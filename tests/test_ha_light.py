"""Tests for the HA light entity: color modes and state mapping.

The device is single-colour but dimmable. Home Assistant rejects
`ColorMode.WHITE` as the *only* supported mode
(homeassistant/components/light/helper.py: valid_supported_color_modes),
and the entity registry then refuses to add the entity at all — which
shows up in HA as:

    Not adding entity None, entity registry error: ... sets invalid
    supported color modes {<ColorMode.WHITE: 'white'>}

This module stubs the HA light module (including a faithful copy of
HA's validation rule) so the entity's declared modes are checked
without a real Home Assistant installation.
"""
import asyncio
import importlib.util as _ilu
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "custom_components", "surplife_matrix"))

import pytest  # noqa: E402

# ── Stub the HA modules before importing light.py ─────────────────────

# Mirrors homeassistant/components/light/const.py
class ColorMode(str):
    """Stand-in for HA's ColorMode StrEnum."""

    UNKNOWN = "unknown"
    ONOFF = "onoff"
    BRIGHTNESS = "brightness"
    COLOR_TEMP = "color_temp"
    HS = "hs"
    XY = "xy"
    RGB = "rgb"
    RGBW = "rgbw"
    RGBWW = "rgbww"
    WHITE = "white"


VALID_COLOR_MODES = {
    ColorMode.ONOFF, ColorMode.BRIGHTNESS, ColorMode.COLOR_TEMP,
    ColorMode.HS, ColorMode.XY, ColorMode.RGB, ColorMode.RGBW,
    ColorMode.RGBWW, ColorMode.WHITE,
}
COLOR_MODES_BRIGHTNESS = VALID_COLOR_MODES - {ColorMode.ONOFF}
COLOR_MODES_COLOR = {
    ColorMode.HS, ColorMode.RGB, ColorMode.RGBW, ColorMode.RGBWW,
    ColorMode.XY,
}


def _color_supported(color_modes) -> bool:
    return not COLOR_MODES_COLOR.isdisjoint(set(color_modes))


def valid_supported_color_modes(color_modes):
    """Faithful copy of HA's valid_supported_color_modes (raises ValueError).

    homeassistant/components/light/helper.py
    """
    color_modes = set(color_modes)
    if (
        not color_modes
        or ColorMode.UNKNOWN in color_modes
        or (ColorMode.BRIGHTNESS in color_modes and len(color_modes) > 1)
        or (ColorMode.ONOFF in color_modes and len(color_modes) > 1)
        or (ColorMode.WHITE in color_modes and not _color_supported(color_modes))
    ):
        raise ValueError(f"Invalid supported_color_modes {sorted(color_modes)}")
    return color_modes


class _LightEntity:
    """Minimal stand-in for homeassistant.components.light.LightEntity."""

    _attr_supported_color_modes = None
    _attr_color_mode = None
    _attr_brightness = None


class _LightEntityFeature:
    """Stand-in for HA's LightEntityFeature IntFlag."""

    EFFECT = 4
    FLASH = 8
    TRANSITION = 32


_light_mod = types.ModuleType("homeassistant.components.light")
_light_mod.ColorMode = ColorMode
_light_mod.LightEntity = _LightEntity
_light_mod.LightEntityFeature = _LightEntityFeature
_light_mod.valid_supported_color_modes = valid_supported_color_modes

_bluetooth = types.ModuleType("homeassistant.components.bluetooth")
_bluetooth.async_get_scanner = lambda hass: None
_bluetooth.async_ble_device_from_address = lambda *a, **k: object()

_config_entries = types.ModuleType("homeassistant.config_entries")
_config_entries.ConfigEntry = type("ConfigEntry", (), {})

_core = types.ModuleType("homeassistant.core")
_core.HomeAssistant = type("HomeAssistant", (), {})
_core.ServiceCall = type("ServiceCall", (), {})
_core.callback = lambda f: f
_core.SupportsResponse = type("SupportsResponse", (), {
    "NONE": 0, "OPTIONAL": 1, "ONLY": 2})

_uc = types.ModuleType("homeassistant.helpers.update_coordinator")


class _FakeCoordinator:
    def __init__(self, *args, **kwargs):
        self.data = None
        self._listeners = []

    def async_set_updated_data(self, data):
        self.data = data
        for cb in self._listeners:
            cb()

    def async_add_listener(self, cb):
        self._listeners.append(cb)


class _FakeCoordinatorEntity:
    """Stand-in for CoordinatorEntity: stores the coordinator, no HA deps."""

    def __init__(self, coordinator, context=None):
        self.coordinator = coordinator
        self.coordinator_context = context

    async def async_added_to_hass(self):
        return None


_uc.DataUpdateCoordinator = _FakeCoordinator
_uc.CoordinatorEntity = _FakeCoordinatorEntity
_uc.UpdateFailed = type("UpdateFailed", (Exception,), {})

_ex = types.ModuleType("homeassistant.exceptions")
_ex.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
sys.modules["homeassistant.exceptions"] = _ex

_ep = types.ModuleType("homeassistant.helpers.entity_platform")
_ep.AddConfigEntryEntitiesCallback = type("AddConfigEntryEntitiesCallback", (), {})

_brc = types.ModuleType("bleak_retry_connector")
_brc.BleakClientWithServiceCache = type("BleakClientWithServiceCache", (), {})
_brc.establish_connection = None

sys.modules["homeassistant.components"] = types.SimpleNamespace(
    light=_light_mod, bluetooth=_bluetooth)
sys.modules["homeassistant.components.light"] = _light_mod
sys.modules["homeassistant.components.bluetooth"] = _bluetooth
sys.modules["homeassistant.config_entries"] = _config_entries
sys.modules["homeassistant.core"] = _core
sys.modules["homeassistant.exceptions"] = _ex
sys.modules["homeassistant.helpers"] = types.SimpleNamespace(
    update_coordinator=_uc, entity_platform=_ep)
sys.modules["homeassistant.helpers.update_coordinator"] = _uc
sys.modules["homeassistant.helpers.entity_platform"] = _ep
sys.modules["bleak_retry_connector"] = _brc

# Register the integration as a package so relative imports resolve
_pkg = types.ModuleType("surplife_matrix")
_pkg.__path__ = [os.path.join(os.path.dirname(__file__), "..",
                              "custom_components", "surplife_matrix")]
sys.modules["surplife_matrix"] = _pkg
sys.modules["surplife_matrix.core"] = types.ModuleType("surplife_matrix.core")
sys.modules["surplife_matrix.core"].__path__ = [os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "surplife_matrix",
    "core")]


def _load(module_name: str, filename: str):
    spec = _ilu.spec_from_file_location(
        module_name,
        os.path.join(os.path.dirname(__file__), "..",
                     "custom_components", "surplife_matrix", filename),
    )
    mod = _ilu.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("surplife_matrix.runtime", "runtime.py")
light_mod = _load("surplife_matrix.light", "light.py")

# The real HomeAssistantError from the stubbed module.
HomeAssistantError = _ex.HomeAssistantError


class _FakeAnimations:
    """Animation manager shim."""

    def __init__(self, names=("plasma", "fire", "ocean")):
        self._names = list(names)
        self.current = None
        self.started = []

    @property
    def names(self):
        return list(self._names)

    def start(self, name, speed=None):
        self.started.append((name, speed))
        self.current = name
        return types.SimpleNamespace(name=name, state="uploading")


class _FakeRuntime:
    """Runtime shim with just what the light entity touches."""

    def __init__(self, address="AA:BB:CC:DD:EE:FF", status=None):
        self.address = address
        self.title = "IOTBTD98"
        self.entry = types.SimpleNamespace(entry_id="entry-id")
        self.status = status if status is not None else {
            "power": True, "brightness": 100, "speed": 50,
        }
        self.coordinator = _FakeCoordinator()
        self.coordinator.data = dict(self.status)
        self.connection = types.SimpleNamespace(
            wedged=False, command=self._command)
        self.animations = _FakeAnimations()
        self.sent = []

    async def _command(self, action):
        """Fake connection: runs the action against a stub conn."""
        conn = _StubConn(self.sent)
        await action(None, conn)

    def apply_status(self, status):
        self.status.update(status)
        self.coordinator.async_set_updated_data(dict(self.status))


class _StubConn:
    """Connection stub that records the commands it receives."""

    def __init__(self, sent):
        self.sent = sent

    async def send(self, packet):
        self.sent.append(packet)

    async def power(self, on, cb):
        self.sent.append(("power", on))

    async def set_brightness(self, value, cb):
        self.sent.append(("brightness", value))


def _entity(status=None) -> "light_mod.SurplifeMatrixLight":
    return light_mod.SurplifeMatrixLight(_FakeRuntime(status=status))


# ── Color mode validation ─────────────────────────────────────────────

def test_supported_color_modes_pass_ha_validation():
    """HA must accept the declared modes, else the entity is never added."""
    entity = _entity()
    modes = entity._attr_supported_color_modes
    assert valid_supported_color_modes(modes) == modes


def test_white_alone_is_rejected_by_ha():
    """Documents why WHITE cannot be used as the sole mode."""
    with pytest.raises(ValueError):
        valid_supported_color_modes({ColorMode.WHITE})


def test_reported_color_mode_is_supported():
    """The reported color mode must be one of the supported modes."""
    entity = _entity()
    assert entity._attr_color_mode in entity._attr_supported_color_modes


def test_brightness_mode_is_the_only_supported_mode():
    """Single-colour dimmable panel: exactly one BRIGHTNESS mode."""
    entity = _entity()
    assert entity._attr_supported_color_modes == {ColorMode.BRIGHTNESS}


# ── State mapping ────────────────────────────────────────────────────

def test_is_on_reflects_status():
    entity = _entity(status={"power": True, "brightness": 100})
    assert entity.is_on is True
    entity = _entity(status={"power": False, "brightness": 100})
    assert entity.is_on is False


def test_brightness_converts_percent_to_0_255():
    entity = _entity(status={"power": True, "brightness": 100})
    assert entity.brightness == 255
    entity = _entity(status={"power": True, "brightness": 50})
    assert entity.brightness == 128  # round(50 * 255 / 100)
    entity = _entity(status={"power": True, "brightness": 0})
    assert entity.brightness == 0


def test_available_false_while_wedged():
    entity = _entity()
    assert entity.available is True
    entity._runtime.connection.wedged = True
    assert entity.available is False


# ── Effects / animations ─────────────────────────────────────────────

def test_effect_list_contains_device_effects_and_animations():
    entity = _entity()
    effects = entity._attr_effect_list
    for name in ("static", "breathe", "wipe"):
        assert name in effects
    for name in ("plasma", "fire", "ocean"):
        assert name in effects


def test_turn_on_with_animation_starts_the_upload():
    entity = _entity()
    runtime = entity._runtime

    async def scenario():
        await entity.async_turn_on(effect="plasma")
        assert runtime.animations.started == [("plasma", None)]
        assert entity._attr_effect == "plasma"

    asyncio.run(scenario())


def test_turn_on_with_unknown_effect_raises():
    entity = _entity()

    async def scenario():
        with pytest.raises(HomeAssistantError):
            await entity.async_turn_on(effect="not-a-thing")

    asyncio.run(scenario())


def test_turn_on_with_device_effect_sends_ea06():
    """A device effect re-activates the cached image with a new effect id."""
    entity = _entity()
    runtime = entity._runtime

    async def scenario():
        await entity._async_command(
            lambda _c, conn: entity._run_effect(conn, "breathe"))
        assert runtime.sent == [bytes([0xEA, 0x06, 0x01, 50, 5])]

    asyncio.run(scenario())
