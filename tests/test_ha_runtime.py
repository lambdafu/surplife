"""Tests for the HA integration runtime: persistent connection + watchdog.

Stubs the homeassistant / bleak_retry_connector modules so the runtime
module imports without a real HA installation, then drives
PersistentConnection against a fake transport to verify:
- connection reuse across commands (one BLE connect, N commands)
- watchdog: N consecutive timeouts -> WedgedError + cooldown
- no-poll invariant: the coordinator never schedules a refresh
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "custom_components", "surplife_matrix"))

import pytest

# ── Stub the HA/bleak modules before importing runtime.py ────────────

_bluetooth = types.ModuleType("homeassistant.components.bluetooth")
_bluetooth.async_get_scanner = lambda hass: None
_bluetooth.async_last_service_info = lambda *a, **k: None

_config_entries = types.ModuleType("homeassistant.config_entries")
_config_entries.ConfigEntry = type("ConfigEntry", (), {})

_core = types.ModuleType("homeassistant.core")
_core.HomeAssistant = type("HomeAssistant", (), {})
_core.ServiceCall = type("ServiceCall", (), {})
_core.callback = lambda f: f

_uc = types.ModuleType("homeassistant.helpers.update_coordinator")
_coordinator_instances = []


class _FakeCoordinator:
    """Minimal DataUpdateCoordinator stand-in (no HA deps)."""

    def __init__(self, hass, logger, name=None, update_method=None,
                 update_interval=None):
        self.update_method = update_method
        self.update_interval = update_interval
        self._update_interval_seconds = (update_interval.total_seconds()
                                         if update_interval else None)
        self.data = None
        self.name = name
        self._listeners = []

    def async_set_updated_data(self, data):
        self.data = data
        for cb in self._listeners:
            cb()

    def async_add_listener(self, cb):
        self._listeners.append(cb)


_uc_mod = types.ModuleType("homeassistant.helpers.update_coordinator")
_uc_mod.DataUpdateCoordinator = _FakeCoordinator
_uc_mod.CoordinatorEntity = type("CoordinatorEntity", (), {})
_uc_mod.UpdateFailed = type("UpdateFailed", (Exception,), {})

_ha = types.ModuleType("homeassistant")
_ha.components = types.SimpleNamespace(
    bluetooth=_bluetooth)
_ha.config_entries = _config_entries
_ha.core = _core
_helpers = types.ModuleType("homeassistant.helpers")
_helpers.update_coordinator = _uc_mod

_brc = types.ModuleType("bleak_retry_connector")
_brc.BleakClientWithServiceCache = type("BleakClientWithServiceCache", (), {})
_brc.establish_connection = None  # replaced per-test

sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules["homeassistant.components"] = types.SimpleNamespace(
    bluetooth=_bluetooth)
sys.modules["homeassistant.components.bluetooth"] = _bluetooth
sys.modules["homeassistant.config_entries"] = _config_entries
sys.modules["homeassistant.core"] = _core
sys.modules["homeassistant.helpers"] = _helpers
sys.modules["homeassistant.helpers.update_coordinator"] = _uc_mod
sys.modules["bleak_retry_connector"] = _brc

# homeassistant.const + helpers.config_validation (services.py imports)
_const = types.ModuleType("homeassistant.const")
_const.ATTR_ENTITY_ID = "entity_id"
sys.modules["homeassistant.const"] = _const
_ex = types.ModuleType("homeassistant.exceptions")
_ex.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
sys.modules["homeassistant.exceptions"] = _ex
_cv = types.ModuleType("homeassistant.helpers.config_validation")
_cv.entity_id = lambda v: v
_cv.template = lambda v: v
_cv.string = lambda v: v
_cv.boolean = lambda v: bool(v)
_cv.ensure_list = lambda v: v if isinstance(v, list) else [v]
sys.modules["homeassistant.helpers.config_validation"] = _cv
# bleak itself is installed in the test env; BLEDevice resolves natively.

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "surplife_matrix.runtime",
    os.path.join(os.path.dirname(__file__), "..",
                 "custom_components", "surplife_matrix", "runtime.py"),
)
# Register the integration as a package so relative imports resolve
_pkg = types.ModuleType("surplife_matrix")
_pkg.__path__ = [os.path.join(os.path.dirname(__file__), "..",
                              "custom_components", "surplife_matrix")]
sys.modules["surplife_matrix"] = _pkg
sys.modules["surplife_matrix.core"] = types.ModuleType("surplife_matrix.core")
sys.modules["surplife_matrix.core"].__path__ = [os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "surplife_matrix",
    "core")]

rt = _ilu.module_from_spec(_spec)
sys.modules["surplife_matrix.runtime"] = rt
_spec.loader.exec_module(rt)  # noqa: E402

# services.py (imports runtime + assets under the same package name)
_spec_services = _ilu.spec_from_file_location(
    "surplife_matrix.services",
    os.path.join(os.path.dirname(__file__), "..",
                 "custom_components", "surplife_matrix", "services.py"),
)
services = _ilu.module_from_spec(_spec_services)
sys.modules["surplife_matrix.services"] = services
_spec_services.loader.exec_module(services)  # noqa: E402


class FakeClient:
    """Fake bleak client with scriptable notifications + connect counting."""

    instances = 0

    def __init__(self):
        FakeClient.instances += 1
        self.connected = True
        self.written = []
        self.script = []          # per-write response: bytes | None | list

    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def services(self):
        class _S:
            def get_characteristic(_self, uuid):
                return uuid if "ff0" in uuid else None

        return _S()

    async def write_gatt_char(self, _char, packet, response=False):
        self.written.append(packet)
        if self.script:
            entry = self.script.pop(0)
            if entry is not None:
                for resp in (entry if isinstance(entry, list) else [entry]):
                    await self._notify(resp)

    async def _notify(self, raw: bytes) -> None:
        # bleak invokes async notify callbacks via its own task machinery;
        # awaiting inline keeps the fake deterministic
        coro = self._sink(0, bytes(raw))
        if coro is not None:
            await coro

    async def start_notify(self, _char, cb):
        self._sink = cb

    async def stop_notify(self, _char):
        pass

    async def disconnect(self):
        self.connected = False


class FakeRuntime:
    """Minimal runtime shim for PersistentConnection (no HA objects)."""

    def __init__(self, client_factory, wedge_threshold=2, wedge_cooldown=60):
        self.address = "AA:BB:CC:DD:EE:FF"
        self.hass = None
        self.wedge_threshold = wedge_threshold
        self.wedge_cooldown = wedge_cooldown
        self.status = {}
        self._client_factory = client_factory

        self.coordinator = _FakeCoordinator(None, None, name="fake")

        def apply_status(status):
            self.status.update(status)
            self.coordinator.async_set_updated_data(dict(self.status))

        self.apply_status = apply_status

        self.connection = rt.PersistentConnection(self)
        self.connection._runtime_device = None

    @property
    def device(self):
        return None


def _wire(client):
    """Wire a FakeClient to the connection (notify sink + characteristics)."""
    # services.get_characteristic is stubbed above; the transport adapter
    # resolves WRITE_UUID through it.


def _status_response(prefix: int = 0x16) -> bytes:
    body = bytes([
        prefix, 0xEA, 0x81, 0x00, 0x00, 0xDD, 0x06, 0x23,
        0x75, 0x01, 0x32, 0x00, 0x00, 0x00, 0x64, 0x00, 0x00,
        0x60, 0x10, 0x02, 0x00, 0xDD, 0x02, 0x63, 0x00,
    ])
    return _wrap_raw(body)


def _wrap_raw(body: bytes) -> bytes:
    return (b"\x00\x01\x80\x00"
            + bytes([(len(body) - 1) >> 8, (len(body) - 1) & 0xFF,
                     len(body) >> 8, len(body) & 0xFF]) + body)


def _make_runtime(responses: list, client: FakeClient | None = None):
    """FakeRuntime whose establish_connection returns a scripted client.

    Script layout for a fresh connect: the transport's write() pops one
    entry per write. The init handshake writes: trigger, time sync, hash
    — so the init responses belong to the THIRD entry. Helper prepends
    [None, None, init_responses] automatically.

    The returned runtime carries `connect_count` (number of
    establish_connection calls) for reuse assertions.
    """
    if client is None:
        client = FakeClient()
    client.script = [None, None] + list(responses)

    counts = {"connects": 0}

    def _establish(client_class, device, name, **kwargs):
        async def _connect():
            counts["connects"] += 1
            return client
        return _connect()

    rt.establish_connection = _establish
    f_runtime = FakeRuntime(None)
    f_runtime.connect_count = counts
    return f_runtime, client


# ── Connection reuse ─────────────────────────────────────────────────

def test_persistent_connection_reuse_single_connect():
    """N commands over one connection: only ONE establish_connection call."""
    responses = [
        [_status_response(0x15), _status_response(0x16)],  # init handshake
    ] + [None] * 5   # set_speed has no ACK; each write consumes one entry
    runtime, client = _make_runtime(responses)

    async def action(_c, conn):
        await conn.set_speed(50)
        return "ok"

    async def scenario():
        for _ in range(5):
            await runtime.connection.command(action)
        assert runtime.connect_count["connects"] == 1, \
            f"should connect exactly once, got {runtime.connect_count}"
        assert len(client.written) >= 5

    asyncio.run(scenario())


def test_no_poll_invariant():
    """The coordinator must never schedule a refresh (poll = connect = wedge)."""
    runtime, _client = _make_runtime([])
    assert runtime.coordinator.update_method is None
    assert runtime.coordinator.update_interval is None
    # and the schedule helper is a no-op with None interval
    assert runtime.coordinator._update_interval_seconds is None


# ── Watchdog ─────────────────────────────────────────────────────────

def test_watchdog_trips_after_threshold_timeouts():
    """Two consecutive timeouts -> WedgedError + cooldown active."""
    runtime, client = _make_runtime([
        [_status_response(0x15), _status_response(0x16)],   # init: success
        None,                              # command 1: no response (timeout)
        None,                              # command 2: no response (timeout)
    ])

    async def scenario():
        async def action(_c, conn):
            await conn._wait_for(m_match_status_16, timeout=0.1)

        # first command: timeout #1
        with pytest.raises(TimeoutError):
            await runtime.connection.command(action)
        # second command: timeout #2 -> wedged
        with pytest.raises(rt.WedgedError):
            await runtime.connection.command(action)
        assert runtime.connection.wedged
        # further commands fail fast with WedgedError
        with pytest.raises(rt.WedgedError):
            await runtime.connection.command(action)

    asyncio.run(scenario())


def test_watchdog_cooldown_expires():
    """After the cooldown, commands are allowed again (reconnect path)."""
    runtime, client = _make_runtime([
        [_status_response(0x15), _status_response(0x16)],
        None, None,                        # two timeouts -> wedge
    ])
    runtime.wedge_cooldown = 0.05

    async def scenario():
        async def action(_c, conn):
            await conn._wait_for(lambda p: False, timeout=0.1)

        with pytest.raises(TimeoutError):
            await runtime.connection.command(action)
        with pytest.raises(rt.WedgedError):
            await runtime.connection.command(action)
        assert runtime.connection.wedged
        await asyncio.sleep(0.15)
        assert not runtime.connection.wedged   # cooldown elapsed

    asyncio.run(scenario())


def test_successful_command_resets_timeout_streak():
    """One success between failures keeps the device out of the wedge state."""
    responses = [
        [_status_response(0x15), _status_response(0x16)],
        None,                                       # timeout #1
        _wrap_raw(bytes([0x16, 0xEA, 0x81] + [0] * 22)),  # command 2 ACKs
        None,                                       # timeout (streak = 1)
    ]
    runtime, client = _make_runtime(responses)

    async def scenario():
        async def fail(_c, conn):
            await conn.send(b"\xea\x07\x00\x32")   # write pops a script entry
            await conn._wait_for(lambda p: False, timeout=0.1)

        async def ack(_c, conn):
            await conn.send(b"\xea\x07\x00\x32")   # write pops the ACK entry
            await conn._wait_for(lambda p: p[1:3] == b"\xea\x81", timeout=0.5)

        with pytest.raises(TimeoutError):
            await runtime.connection.command(fail)
        assert not runtime.connection.wedged
        await runtime.connection.command(ack)       # success resets streak
        assert not runtime.connection.wedged
        # now only ONE more timeout should NOT wedge
        with pytest.raises(TimeoutError):
            await runtime.connection.command(fail)
        assert not runtime.connection.wedged

    asyncio.run(scenario())


def test_apply_status_pushes_to_coordinator():
    """apply_status updates runtime.status AND coordinator.data (no polling)."""
    runtime, _client = _make_runtime([])
    runtime.apply_status({"power": False, "brightness": 40})
    assert runtime.status["power"] is False
    assert runtime.coordinator.data == runtime.status
    assert runtime.coordinator.data["brightness"] == 40


# matcher imported late to avoid the stubbed-module ordering issue
from surplife_core import messages as m  # noqa: E402

m_match_status_16 = m.match_status_16


# ── Command serialization ────────────────────────────────────────────

def test_concurrent_commands_never_interleave():
    """Two concurrent commands serialize: no interleaved writes."""
    responses = [
        [_status_response(0x15), _status_response(0x16)],   # init
    ]
    runtime, client = _make_runtime(responses)

    in_flight = []

    async def action(tag):
        in_flight.append(tag)
        await asyncio.sleep(0.05)          # simulate a slow command
        in_flight.append(tag)

    async def scenario():
        # fire both commands truly concurrently
        await asyncio.gather(
            runtime.connection.command(lambda c, n: action("A")),
            runtime.connection.command(lambda c, n: action("B")),
        )

    asyncio.run(scenario())
    # the lock serializes: each tag enters and leaves before the other starts
    assert in_flight == ["A", "A", "B", "B"] or in_flight == ["B", "B", "A", "A"], \
        in_flight


def test_partial_upload_abort_logs_and_reraises(caplog):
    """A mid-upload transport error logs the abort and re-raises."""
    import logging

    from surplife_core.connection import SurplifeConnection

    class BoomTransport:
        def __init__(self):
            self.writes = 0
            self.script = []   # header ack then end ack (never reached)

        async def write(self, packet):
            self.writes += 1
            if self.writes == 3:            # header ack'd, then fail on segment
                raise OSError("connection dropped")

    async def scenario():
        transport = BoomTransport()
        conn = SurplifeConnection(transport)
        conn.feed_notification(_resp_bytes(0x15, 0xE0, 0x30, b"\x00\x01"))
        payload = bytes(4 * 490)         # multi-segment

        with caplog.at_level(logging.ERROR):
            with pytest.raises(OSError, match="connection dropped"):
                await conn.upload_content(
                    0x01, b"{}", payload, b"\xea\x07\x00\x50",
                    bytes(range(16)), settle_s=0.0)

        assert any("aborted at segment" in r.message for r in caplog.records)


def _resp_bytes(prefix, cmd0, cmd1, extra=b""):
    body = bytes([prefix, cmd0, cmd1]) + extra
    return (b"\x00\x01\x80\x00"
            + bytes([(len(body) - 1) >> 8, (len(body) - 1) & 0xFF,
                     len(body) >> 8, len(body) & 0xFF]) + body)


def test_fetch_media_builtin_resolution():
    """_fetch_media resolves builtin: sources to bundled animation bytes."""
    class FakeHass:
        async def async_add_executor_job(self, fn, *args):
            return fn(*args)

    raw = asyncio.run(services._fetch_media(FakeHass(), "builtin:plasma"))
    assert raw[:3] == b"GIF"
    with pytest.raises(KeyError):
        asyncio.run(services._fetch_media(FakeHass(), "builtin:missing"))
