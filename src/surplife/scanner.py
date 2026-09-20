"""BLE device discovery for Surplife displays."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .protocol import BLE_MANUFACTURER_ID, BLE_NAME_PREFIX

log = logging.getLogger(__name__)


@dataclass
class SurplifeDevice:
    """A discovered Surplife display."""

    address: str
    name: str
    rssi: int
    ble_device: BLEDevice | None = None
    manufacturer_data: bytes | None = None

    @property
    def device_id(self) -> str:
        """The device ID suffix from the BLE name.

        BLE names follow the pattern IOTBT<id> where <id> is derived from
        the device's MAC address. The full ID is the last 4 hex chars of
        the MAC (e.g. MAC xx:xx:xx:xx:79:96 -> ID 7996), but the BLE
        local name is capped at 8 bytes, so only the last 3 hex chars
        fit after the IOTBT prefix (e.g. IOTBT996, IOTBTD98).
        """
        return self.name.removeprefix(BLE_NAME_PREFIX)

    def __str__(self) -> str:
        return f"{self.name} ({self.address}, RSSI={self.rssi})"


def _is_surplife(name: str | None, adv: AdvertisementData) -> bool:
    """Check whether an advertisement looks like a Surplife display."""
    if name and name.upper().startswith(BLE_NAME_PREFIX):
        return True
    if BLE_MANUFACTURER_ID in adv.manufacturer_data:
        return True
    return False


def _make_device(device: BLEDevice, adv: AdvertisementData) -> SurplifeDevice:
    """Create a SurplifeDevice from a BLE discovery result."""
    return SurplifeDevice(
        address=device.address,
        name=device.name or adv.local_name or "",
        rssi=adv.rssi,
        ble_device=device,
        manufacturer_data=adv.manufacturer_data.get(BLE_MANUFACTURER_ID),
    )


async def discover(timeout: float = 10.0) -> list[SurplifeDevice]:
    """Scan for nearby Surplife displays.

    Waits the full timeout, then returns all found devices.
    For live progress updates, use discover_live() instead.
    """
    return await discover_live(timeout=timeout)


async def discover_live(
    timeout: float = 10.0,
    on_update: Callable[[list[SurplifeDevice], int], None] | None = None,
) -> list[SurplifeDevice]:
    """Scan for Surplife displays with live progress callbacks.

    Args:
        timeout: Scan duration in seconds.
        on_update: Called each time a new BLE device is seen, with
                   (surplife_devices_so_far, total_ble_devices_seen).

    Returns:
        List of discovered devices, sorted by signal strength (strongest first).
    """
    log.debug("Scanning for Surplife displays (%.1fs)...", timeout)
    found: dict[str, SurplifeDevice] = {}
    total_seen: set[str] = set()

    def _on_detect(device: BLEDevice, adv: AdvertisementData) -> None:
        addr = device.address
        is_new = addr not in total_seen
        total_seen.add(addr)

        name = device.name or adv.local_name or ""
        if _is_surplife(name, adv) and addr not in found:
            found[addr] = _make_device(device, adv)

        if is_new and on_update is not None:
            on_update(list(found.values()), len(total_seen))

    scanner = BleakScanner(detection_callback=_on_detect)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()

    result = list(found.values())
    result.sort(key=lambda d: d.rssi, reverse=True)
    log.debug("Found %d Surplife device(s)", len(result))
    return result


async def _find_first(
    match: Callable[[str, AdvertisementData], bool],
    timeout: float,
    description: str,
) -> SurplifeDevice:
    """Scan until the first matching device is found, then stop early.

    Uses a callback-based scanner for fast early return (~1s) instead of
    waiting for the full scan timeout.
    """
    log.debug("Scanning for %s (up to %.1fs)...", description, timeout)
    found: asyncio.Future[SurplifeDevice] = asyncio.get_event_loop().create_future()

    def _on_detect(device: BLEDevice, adv: AdvertisementData) -> None:
        if found.done():
            return
        name = device.name or adv.local_name or ""
        if not match(name, adv):
            return
        found.set_result(_make_device(device, adv))

    scanner = BleakScanner(detection_callback=_on_detect)
    await scanner.start()
    try:
        return await asyncio.wait_for(found, timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"{description} not found (scanned {timeout:.0f}s)")
    finally:
        await scanner.stop()


async def discover_one(timeout: float = 10.0) -> SurplifeDevice:
    """Scan and return the first Surplife display found.

    Raises:
        RuntimeError: If no Surplife display is found.
    """
    return await _find_first(
        match=lambda name, adv: _is_surplife(name, adv),
        timeout=timeout,
        description="Surplife display",
    )


async def find_by_name(name: str, timeout: float = 10.0) -> SurplifeDevice:
    """Find a specific Surplife display by its BLE name (e.g. IOTBTD98).

    Matches case-insensitively. Accepts with or without the IOTBT prefix.

    Raises:
        RuntimeError: If the named device is not found.
    """
    if not name.upper().startswith(BLE_NAME_PREFIX):
        name = BLE_NAME_PREFIX + name

    return await _find_first(
        match=lambda n, adv: n.upper() == name.upper(),
        timeout=timeout,
        description=name,
    )
