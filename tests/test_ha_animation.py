"""Tests for the animation catalog, the async upload manager and the
select entity that exposes the bundled animations.

The manager is exercised against a fake runtime whose connection records
the uploaded bytes, so the whole path (catalog -> upload -> catalog
mapping) is verified without a BLE device.
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "custom_components", "surplife_matrix"))

import pytest  # noqa: E402

# ── Stub the HA modules before importing the integration modules ──────

_core = types.ModuleType("homeassistant.core")
_core.HomeAssistant = type("HomeAssistant", (), {})
_core.ServiceCall = type("ServiceCall", (), {})
_core.callback = lambda f: f
_core.SupportsResponse = type("SupportsResponse", (), {
    "NONE": 0, "OPTIONAL": 1, "ONLY": 2})

_config_entries = types.ModuleType("homeassistant.config_entries")
_config_entries.ConfigEntry = type("ConfigEntry", (), {})

_select = types.ModuleType("homeassistant.components.select")


class _SelectEntity:
    """Minimal stand-in for homeassistant.components.select.SelectEntity."""

    _attr_options: list[str] = []
    _attr_current_option = None
    _attr_has_entity_name = True
    _attr_name = None
    _attr_icon = None
    _attr_should_poll = False

    def __init__(self, *args, **kwargs):
        self.entity_id = "select.test"
        self._written = []

    @property
    def options(self) -> list[str]:
        return self._attr_options

    async def async_added_to_hass(self):
        return None

    def async_write_ha_state(self):
        self._written.append(True)

_select.SelectEntity = _SelectEntity

_ep = types.ModuleType("homeassistant.helpers.entity_platform")
_ep.AddConfigEntryEntitiesCallback = type("AddConfigEntryEntitiesCallback", (), {})

sys.modules["homeassistant.core"] = _core
sys.modules["homeassistant.config_entries"] = _config_entries
sys.modules["homeassistant.components"] = types.SimpleNamespace(
    select=_select)
sys.modules["homeassistant.components.select"] = _select
sys.modules["homeassistant.helpers"] = types.SimpleNamespace(
    entity_platform=_ep)
sys.modules["homeassistant.helpers.entity_platform"] = _ep

# Register the integration as a package so relative imports resolve
_pkg = types.ModuleType("surplife_matrix")
_pkg.__path__ = [os.path.join(os.path.dirname(__file__), "..",
                              "custom_components", "surplife_matrix")]
sys.modules["surplife_matrix"] = _pkg
sys.modules["surplife_matrix.core"] = types.ModuleType("surplife_matrix.core")
sys.modules["surplife_matrix.core"].__path__ = [os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "surplife_matrix",
    "core")]

import importlib.util as _ilu  # noqa: E402


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


anim_mod = _load("surplife_matrix.animation", "animation.py")
select_mod = _load("surplife_matrix.select", "select.py")


# ── Fakes ─────────────────────────────────────────────────────────────

class _FakeHass:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class _FakeConnection:
    """Records the upload instead of talking to a device."""

    def __init__(self):
        self.uploads = []
        self.wedged = False

    async def upload_content(self, cache_type, meta, payload, activate,
                             c_hash, **kwargs):
        self.uploads.append({
            "cache_type": cache_type,
            "meta": meta,
            "payload": payload,
            "activate": activate,
            "hash": c_hash,
        })

    async def command(self, action):
        await action(None, self)


class _FakeRuntime:
    def __init__(self, catalog=None):
        self.address = "AA:BB:CC:DD:EE:FF"
        self.title = "IOTBTD98"
        self.hass = _FakeHass()
        self.connection = _FakeConnection()
        self.animations = anim_mod.AnimationManager(self, catalog)


# ── Catalog ───────────────────────────────────────────────────────────

def test_catalog_lists_bundled_animations():
    catalog = anim_mod.AnimationCatalog()
    names = catalog.names
    assert names, "the integration ships animations"
    assert "plasma" in names
    assert names == sorted(names)


def test_catalog_probes_size_and_frames():
    catalog = anim_mod.AnimationCatalog()
    plasma = catalog.resolve("plasma")
    assert plasma.size > 0
    assert plasma.frames > 1


def test_catalog_unknown_name_raises():
    catalog = anim_mod.AnimationCatalog()
    with pytest.raises(KeyError):
        catalog.resolve("does-not-exist")


def test_catalog_records_hash_to_name():
    catalog = anim_mod.AnimationCatalog()
    c_hash = bytes(range(16))
    catalog.record("AA:BB:CC:DD:EE:FF", c_hash, "plasma")
    assert catalog.name_for("AA:BB:CC:DD:EE:FF", c_hash) == "plasma"
    assert catalog.hash_for("AA:BB:CC:DD:EE:FF", "plasma") == c_hash
    # A different device does not see the mapping.
    assert catalog.name_for("11:22:33:44:55:66", c_hash) is None


# ── Upload manager ───────────────────────────────────────────────────

def test_start_uploads_in_background():
    runtime = _FakeRuntime()
    manager = anim_mod.AnimationManager(runtime)

    async def scenario():
        upload = manager.start("plasma", 70)
        assert upload.state == anim_mod.STATE_UPLOADING
        assert upload.task is not None
        await upload.task
        assert upload.state == anim_mod.STATE_ACTIVE
        assert upload.content_hash is not None
        assert manager.current == "plasma"

    asyncio.run(scenario())


def test_upload_records_name_in_catalog():
    runtime = _FakeRuntime()
    catalog = anim_mod.AnimationCatalog()
    manager = anim_mod.AnimationManager(runtime, catalog)

    async def scenario():
        upload = manager.start("fire")
        await upload.task
        assert catalog.name_for(runtime.address, upload.content_hash) == "fire"

    asyncio.run(scenario())


def test_upload_reports_errors():
    runtime = _FakeRuntime()

    async def _failing_command(action):
        raise RuntimeError("ble died")

    runtime.connection.command = _failing_command
    manager = anim_mod.AnimationManager(runtime)

    async def scenario():
        upload = manager.start("plasma")
        await upload.task
        assert upload.state == anim_mod.STATE_ERROR
        assert "ble died" in upload.error

    asyncio.run(scenario())


def test_step_cycles_through_the_catalog():
    runtime = _FakeRuntime()
    manager = anim_mod.AnimationManager(runtime)
    names = manager.names

    async def scenario():
        first = manager.step(1)
        await first.task
        second = manager.step(1)
        await second.task
        assert first.name == names[0]
        assert second.name == names[1]
        # Walking past the end wraps around to the first entry.
        for _ in range(len(names) - 2):
            upload = manager.step(1)
            await upload.task
        wrapped = manager.step(1)
        await wrapped.task
        assert wrapped.name == names[0]

    asyncio.run(scenario())


def test_step_backwards():
    runtime = _FakeRuntime()
    manager = anim_mod.AnimationManager(runtime)
    names = manager.names

    async def scenario():
        upload = manager.step(-1)
        await upload.task
        assert upload.name == names[-1]

    asyncio.run(scenario())


# ── Select entity ────────────────────────────────────────────────────

def test_select_options_come_from_the_catalog():
    runtime = _FakeRuntime()
    entity = select_mod.SurplifeAnimationSelect(runtime)
    assert entity.options == runtime.animations.names
    assert "plasma" in entity.options


def test_select_reports_the_current_animation():
    runtime = _FakeRuntime()
    runtime.animations.set_current("fire")
    entity = select_mod.SurplifeAnimationSelect(runtime)
    entity._attr_current_option = runtime.animations.current
    assert entity.current_option == "fire"


def test_select_starts_the_upload_and_updates_state():
    runtime = _FakeRuntime()
    entity = select_mod.SurplifeAnimationSelect(runtime)

    async def scenario():
        await entity.async_select_option("plasma")
        # The state is reported immediately, before the upload finished.
        assert entity.current_option == "plasma"
        # Wait for the background upload to complete.
        await asyncio.sleep(0)
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                await task
        assert runtime.animations.current == "plasma"

    asyncio.run(scenario())


def test_select_is_unavailable_while_wedged():
    runtime = _FakeRuntime()
    entity = select_mod.SurplifeAnimationSelect(runtime)
    assert entity.available is True
    runtime.connection.wedged = True
    assert entity.available is False
