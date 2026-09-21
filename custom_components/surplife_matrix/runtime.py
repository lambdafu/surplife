"""Runtime data for one Surplife Matrix device.

Owns a **persistent, always-connected BLE session** using the HA Bluetooth
integration's shared scanner and bleak-retry-connector.

Why persistent: the device firmware leaks a resource per BLE connection and
wedges after ~6-7 connect cycles per boot (see PROTOCOL.md "BLE Connect-
Cycle Limitation" — no self-recovery, manual power cycle required). One
long-lived session with all commands batched through it is exactly the
official app's pattern (trace 60: 15 commands over one connection) and is
verified safe. Periodic polling is forbidden.

A watchdog tracks consecutive write/wait timeouts: after
`WEDGE_THRESHOLD` failures the device is presumed firmware-wedged, the
entity goes unavailable, the user is warned to power-cycle, and reconnect
attempts back off for `WEDGE_COOLDOWN_S` (hammering a wedged device
extends the wedge).
"""

from __future__ import annotations

import asyncio
import logging
import time

from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)
from homeassistant.components.bluetooth import async_get_scanner
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    RECONNECT_MAX_BACKOFF_S,
    RECONNECT_MIN_BACKOFF_S,
    WEDGE_COOLDOWN_S,
    WEDGE_THRESHOLD,
)
from .core.connection import SurplifeConnection
from .core.protocol import (
    NOTIFY_UUID,
    WRITE_UUID,
)

_LOGGER = logging.getLogger(__name__)


class TransportAdapter:
    """Adapts a bleak client to the SurplifeConnection transport interface."""

    def __init__(self, client):
        self._client = client
        self._write_char = client.services.get_characteristic(WRITE_UUID)
        if self._write_char is None:
            raise RuntimeError("Write characteristic ff01 not found")

    async def write(self, packet: bytes) -> None:
        _LOGGER.debug(">> %s", packet.hex())
        await self._client.write_gatt_char(self._write_char, packet,
                                           response=False)


class WedgedError(Exception):
    """The device is presumed firmware-wedged (needs manual power cycle)."""


class PersistentConnection:
    """Long-lived BLE session with auto-reconnect and a wedge watchdog.

    All commands run through `command()` which:

    1. Ensures the connection is up (lazy connect on first use).
    2. Runs the async action `f(client, conn)`.
    3. On timeout: counts toward the wedge threshold; on non-timeout
       transport errors: schedules a reconnect with backoff.

    After `WEDGE_THRESHOLD` consecutive timeouts the connection is marked
    wedged: all commands raise `WedgedError` until the cooldown elapses
    and a reconnect succeeds.
    """

    def __init__(self, runtime: SurplifeRuntime):
        self._runtime = runtime
        self._client = None
        self._conn: SurplifeConnection | None = None
        self._connect_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()   # serialize all traffic
        self._consecutive_timeouts = 0
        self._wedged_until = 0.0
        self._backoff = RECONNECT_MIN_BACKOFF_S
        self._closed = False

    # ── Lifecycle ────────────────────────────────────────────────────

    async def ensure_connected(self):
        """Return (client, conn), connecting if necessary.

        Raises WedgedError during the cooldown window.
        """
        if self._closed:
            raise RuntimeError("Connection closed")
        if time.monotonic() < self._wedged_until:
            remaining = self._wedged_until - time.monotonic()
            raise WedgedError(
                f"Device presumed wedged; retry in {remaining:.0f}s "
                "(power-cycle the display)")

        if self._conn is not None and self._client.is_connected:
            return self._client, self._conn

        async with self._connect_lock:
            if self._conn is not None and self._client.is_connected:
                return self._client, self._conn

            scanner = async_get_scanner(self._runtime.hass)
            address = self._runtime.address
            _LOGGER.debug("Connecting to %s", address)
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    self._runtime.device,
                    address,
                    max_attempts=3,
                    scanner=scanner,
                )
            except Exception as err:
                if time.monotonic() < self._wedged_until:
                    raise WedgedError(
                        f"Device presumed wedged: {err}") from err
                raise

            write_char = client.services.get_characteristic(WRITE_UUID)
            notify_char = client.services.get_characteristic(NOTIFY_UUID)
            if write_char is None or notify_char is None:
                raise RuntimeError("GATT characteristics ff01/ff02 not found")

            conn = SurplifeConnection(TransportAdapter(client))

            async def _cb(_sender: int, data: bytearray) -> None:
                conn.feed_notification(bytes(data))

            await client.start_notify(notify_char, _cb)
            try:
                await conn.init(self._runtime.apply_status)
            except TimeoutError:
                # init timeout on a fresh connect: count toward the wedge
                # watchdog and drop the half-open client
                self._consecutive_timeouts += 1
                await self._disconnect_client()
                raise
            self._client = client
            self._conn = conn
            self._consecutive_timeouts = 0
            self._backoff = RECONNECT_MIN_BACKOFF_S
            return client, conn

    async def _disconnect_client(self) -> None:
        if self._client is not None and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None
        self._conn = None

    async def close(self) -> None:
        self._closed = True
        await self._disconnect_client()

    # ── Command execution ────────────────────────────────────────────

    async def command(self, action):
        """Run `async f(conn)` on the persistent connection, serialized.

        A command lock serializes all traffic: two interleaved uploads
        would corrupt the device's segment reassembly (its `e0 32` chunks
        are reassembled by index into ONE blob), and any other interleaved
        command pair races the request/response matching.

        Timeouts count toward the wedge watchdog; after the threshold the
        connection enters the wedge cooldown.
        """
        async with self._command_lock:
            return await self._command_locked(action)

    async def _command_locked(self, action):
        try:
            client, conn = await self.ensure_connected()
        except WedgedError:
            raise
        except Exception as err:
            # reconnect path failed; back off before the next attempt
            self._consecutive_timeouts += 1
            if self._consecutive_timeouts >= self._runtime.wedge_threshold:
                self._mark_wedged()
                raise WedgedError(str(err)) from err
            await self._schedule_backoff()
            raise
        try:
            result = await action(client, conn)
            self._consecutive_timeouts = 0   # success resets the streak
            return result
        except TimeoutError as err:
            self._consecutive_timeouts += 1
            if self._consecutive_timeouts >= self._runtime.wedge_threshold:
                self._mark_wedged()
                raise WedgedError(
                    "Device presumed wedged (repeated timeouts) — "
                    "power-cycle the display") from err
            raise
        except Exception:
            # transport-level break: reset for reconnect
            await self._disconnect_client()
            raise

    def _mark_wedged(self) -> None:
        self._wedged_until = time.monotonic() + self._runtime.wedge_cooldown
        self._consecutive_timeouts = 0
        _LOGGER.warning(
            "Device %s presumed firmware-wedged (panel frozen). "
            "Power-cycle the display; retrying in %ds.",
            self._runtime.address, self._runtime.wedge_cooldown)
        # drop the client so the next attempt reconnects after cooldown.
        # Works on the running asyncio loop (HA or test): schedule as a task.
        self._drop_task = asyncio.get_event_loop().create_task(
            self._disconnect_client())

    async def _schedule_backoff(self) -> None:
        await asyncio.sleep(self._backoff)
        self._backoff = min(self._backoff * 2, RECONNECT_MAX_BACKOFF_S)

    @property
    def wedged(self) -> bool:
        return time.monotonic() < self._wedged_until


class SurplifeRuntime:
    """Per-config-entry runtime: persistent connection + status holder.

    No periodic polling — the coordinator exists as a passive state holder
    so entities can subscribe to state changes driven by command ACKs.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data["address"]
        self.title: str = entry.title
        self._device = None
        self.status = {"power": True, "speed": 0, "brightness": 100,
                       "cols": 96, "rows": 16}
        self.wedge_threshold = WEDGE_THRESHOLD
        self.wedge_cooldown = WEDGE_COOLDOWN_S

        self.connection = PersistentConnection(self)

        self.coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{address_suffix(self.address)}",
            update_method=None,          # no polling — see module docstring
            update_interval=None,
        )

    def apply_status(self, status: dict) -> None:
        self.status.update(status)
        # push to entities without reconnecting
        self.coordinator.async_set_updated_data(dict(self.status))

    def async_setup(self) -> None:
        """Resolve the BLEDevice from the HA bluetooth integration."""
        from homeassistant.components.bluetooth import async_last_service_info

        service_info = async_last_service_info(self.hass, self.address,
                                               connectable=True)
        if service_info is None:
            raise RuntimeError(f"Device {self.address} not in BLE range")
        self._device = service_info.device

    async def async_close(self) -> None:
        await self.connection.close()

    @property
    def device(self):
        return self._device

    # ── Session API (legacy shape, now backed by the persistent conn) ─

    def session(self):
        """Context-manager shim for existing call sites.

        Keeps the `async with runtime.session() as (client, conn)` shape
        working without opening a new BLE connection per use.
        """
        return _PersistentSessionProxy(self)


class _PersistentSessionProxy:
    """Mimics SurplifeSession's async-context API over the persistent conn."""

    def __init__(self, runtime: SurplifeRuntime):
        self._runtime = runtime

    async def __aenter__(self):
        return await self._runtime.connection.ensure_connected()

    async def __aexit__(self, *exc) -> None:
        # nothing to close: the connection persists
        return None


def address_suffix(address: str) -> str:
    """Short device label from a MAC address (last two octets)."""
    return address.replace(":", "")[-4:].upper()


__all__ = [
    "PersistentConnection",
    "SurplifeRuntime",
    "TransportAdapter",
    "WedgedError",
    "address_suffix",
]
