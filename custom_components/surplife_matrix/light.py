"""Light entity for Surplife Matrix (power + brightness).

Commands run over the runtime's persistent connection — one BLE connection
per device, never re-connected per action (the firmware wedges after ~6-7
connect cycles; see PROTOCOL.md "BLE Connect-Cycle Limitation").
"""

from __future__ import annotations

import logging

from homeassistant.components.light import (
    ColorMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SurplifeCoordinatorEntity
from .runtime import SurplifeRuntime

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


class SurplifeMatrixLight(SurplifeCoordinatorEntity):
    """Power and brightness control of the LED matrix."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_color_modes = {ColorMode.WHITE}
    _attr_assumed_state = True   # state comes from command ACKs, not polling

    @property
    def unique_id(self) -> str:
        return self._runtime.address

    async def async_added_to_hass(self) -> None:
        """Register entity_id → entry for service targeting."""
        from .const import DOMAIN

        self.hass.data[DOMAIN].setdefault("entities", {})
        self.hass.data[DOMAIN]["entities"][self.entity_id] = \
            self._runtime.entry.entry_id

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
        async def _run(_client, conn):
            if "brightness" in kwargs:
                pct = round(int(kwargs["brightness"]) * 100 / 255)
                await conn.set_brightness(pct, self._runtime.apply_status)
            elif "brightness_pct" in kwargs:
                pct = round(int(kwargs["brightness_pct"]))
                await conn.set_brightness(pct, self._runtime.apply_status)
            else:
                await conn.power(True, self._runtime.apply_status)

        await self._runtime.connection.command(_run)

    async def async_turn_off(self, **kwargs) -> None:
        async def _run(_client, conn):
            await conn.power(False, self._runtime.apply_status)

        await self._runtime.connection.command(_run)


def _status(runtime: SurplifeRuntime) -> dict:
    return runtime.coordinator.data or runtime.status
