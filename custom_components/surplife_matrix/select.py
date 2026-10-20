"""Select entity for choosing a bundled animation.

One `select` entity per display:

    select.<device>_animation
        options: [fire, ocean, plasma, rainbow_wave, ...]
        state:   the animation that is currently displayed

The options come from the bundled `animations/` folder, so an animation
added to the integration shows up here automatically. Selecting an
option starts an asynchronous upload (see `animation.py`): the entity
reports the option immediately and the upload runs in the background,
because pushing a ~40 KB GIF over BLE takes several seconds.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN

if TYPE_CHECKING:
    from .runtime import SurplifeRuntime

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one animation select entity per device."""
    runtime: SurplifeRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SurplifeAnimationSelect(runtime)])


class SurplifeAnimationSelect(SelectEntity):
    """Pick which bundled animation the display shows."""

    _attr_has_entity_name = True
    _attr_name = "Animation"
    _attr_icon = "mdi:animation"
    _attr_should_poll = False

    def __init__(self, runtime: SurplifeRuntime) -> None:
        super().__init__()
        self._runtime = runtime
        self._attr_options = runtime.animations.names

    @property
    def unique_id(self) -> str:
        return f"{self._runtime.address}_animation"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._runtime.address)},
            "connections": {("bluetooth", self._runtime.address)},
            "name": self._runtime.title,
            "manufacturer": "Surplife",
            "model": "LED Matrix 96x16",
        }

    @property
    def current_option(self) -> str | None:
        """The animation that is currently displayed."""
        return self._attr_current_option

    @property
    def available(self) -> bool:
        # unavailable while the wedge watchdog has the device in cooldown
        return not self._runtime.connection.wedged

    async def async_select_option(self, option: str) -> None:
        """Start uploading and showing the selected animation."""
        upload = self._runtime.animations.start(option)
        # Report the selection right away; the upload continues in the
        # background and updates the entity when it completes.
        self._attr_current_option = upload.name
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Refresh the option list once the entity is registered."""
        await super().async_added_to_hass()
        self._attr_options = self._runtime.animations.names
        self._attr_current_option = self._runtime.animations.current
        # Refresh the entity when a background upload finishes.
        self._runtime.animations.set_update_callback(self._handle_upload_done)

    @callback
    def _handle_upload_done(self) -> None:
        """Push the new state after a background upload completed."""
        self._attr_current_option = self._runtime.animations.current
        self.async_write_ha_state()
