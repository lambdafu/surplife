"""Light entity for Surplife Matrix (power + brightness).

Commands run over the runtime's persistent connection — one BLE connection
per device, never re-connected per action (the firmware wedges after ~6-7
connect cycles; see PROTOCOL.md "BLE Connect-Cycle Limitation").
"""

from __future__ import annotations

import logging

from homeassistant.components.light import (
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, EFFECTS
from .coordinator import SurplifeCoordinatorEntity
from .runtime import DeviceNotFoundError, SurplifeRuntime, WedgedError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one light entity per device."""
    from .const import DOMAIN

    runtime: SurplifeRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SurplifeMatrixLight(runtime)])


class SurplifeMatrixLight(SurplifeCoordinatorEntity, LightEntity):
    """Power and brightness control of the LED matrix."""

    _attr_has_entity_name = True
    _attr_name = None
    # The panel is single-colour but dimmable, so BRIGHTNESS is the only
    # valid mode here. HA rejects ColorMode.WHITE as a sole mode
    # (see homeassistant/components/light/helper.py
    # valid_supported_color_modes), which would make it refuse the
    # entity and leave the device without a light entity.
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_assumed_state = True   # state comes from command ACKs, not polling

    def __init__(self, runtime: SurplifeRuntime) -> None:
        super().__init__(runtime)
        self._attr_effect_list = list(EFFECTS) + runtime.animations.names
        self._attr_effect = runtime.animations.current

    @property
    def unique_id(self) -> str:
        return self._runtime.address

    async def async_added_to_hass(self) -> None:
        """Register entity_id → entry for service targeting."""
        await super().async_added_to_hass()
        self.hass.data[DOMAIN].setdefault("entities", {})
        self.hass.data[DOMAIN]["entities"][self.entity_id] = \
            self._runtime.entry.entry_id
        # Refresh the entity when a background upload finishes.
        self._runtime.animations.set_update_callback(self._handle_upload_done)

    @callback
    def _handle_upload_done(self) -> None:
        """Push the new state after a background upload completed."""
        self._attr_effect = self._runtime.animations.current
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # unavailable while the wedge watchdog has the device in cooldown
        return not self._runtime.connection.wedged

    @property
    def is_on(self) -> bool | None:
        return _status(self._runtime).get("power")

    @property
    def brightness(self) -> int | None:
        """HA brightness 0-255 from device percent 0-100."""
        pct = _status(self._runtime).get("brightness", 100)
        return round(pct * 255 / 100)

    async def async_turn_on(self, **kwargs) -> None:
        effect = kwargs.get("effect")

        if effect and effect not in EFFECTS:
            # An animation name: start the upload in the background and
            # power the panel on. The upload takes a few seconds, so it
            # must not block the service call.
            if effect not in self._runtime.animations.names:
                raise HomeAssistantError(
                    f"Unknown effect or animation '{effect}'. Available "
                    f"animations: {', '.join(self._runtime.animations.names)}")
            self._runtime.animations.start(effect)
            self._attr_effect = effect
            kwargs.pop("effect", None)
        elif effect in EFFECTS:
            # A device effect applies to the image that is already
            # cached on the device (ea 06 with a new effect id).
            self._attr_effect = effect
            kwargs.pop("effect", None)

        async def _run(_client, conn):
            if effect in EFFECTS:
                # Re-activate the cached image with the new effect.
                await self._run_effect(conn, effect)
            elif "brightness" in kwargs:
                pct = round(int(kwargs["brightness"]) * 100 / 255)
                await conn.set_brightness(pct, self._runtime.apply_status)
            elif "brightness_pct" in kwargs:
                pct = round(int(kwargs["brightness_pct"]))
                await conn.set_brightness(pct, self._runtime.apply_status)
            else:
                await conn.power(True, self._runtime.apply_status)

        await self._async_command(_run)

    async def _run_effect(self, conn, effect: str) -> None:
        """Apply a device effect to the image cached on the device."""
        await conn.send(bytes([0xEA, 0x06, 0x01, 50, EFFECTS[effect]]))

    async def async_turn_off(self, **kwargs) -> None:
        async def _run(_client, conn):
            await conn.power(False, self._runtime.apply_status)

        await self._async_command(_run)

    async def _async_command(self, action) -> None:
        """Run a command, surfacing BLE failures as clean HA errors."""
        try:
            await self._runtime.connection.command(action)
        except (DeviceNotFoundError, WedgedError) as err:
            raise HomeAssistantError(str(err)) from err


def _status(runtime: SurplifeRuntime) -> dict:
    return runtime.coordinator.data or runtime.status
