"""The Surplife Matrix integration.

Controls Surplife 96x16 LED matrix displays (IOTBT*) over the Bluetooth
integration's shared BLE stack: a light entity for power/brightness and
services for content (images, GIFs, text, clock, direct draw, playlist).
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .runtime import SurplifeRuntime

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Surplife Matrix from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("entities", {})
    runtime = SurplifeRuntime(hass, entry)
    runtime.async_setup()
    hass.data[DOMAIN][entry.entry_id] = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime: SurplifeRuntime = hass.data[DOMAIN].pop(entry.entry_id)
        await runtime.async_close()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup(hass: HomeAssistant, config) -> None:
    """Register content services once (domain-level, not per entry)."""
    from .services import async_setup_services

    await async_setup_services(hass)
