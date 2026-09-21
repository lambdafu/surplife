"""Config flow for the Surplife Matrix integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import DOMAIN


async def _async_discovered_devices(hass) -> list[BluetoothServiceInfoBleak]:
    """In-range IOTBT* devices, newest advertisement per address."""
    found: dict[str, BluetoothServiceInfoBleak] = {}
    for info in async_discovered_service_info(hass, connectable=True):
        if info.name.startswith("IOTBT"):
            found[info.address] = info
    return list(found.values())


class SurplifeMatrixConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Surplife Matrix devices."""

    VERSION = 1

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a device discovered via the bluetooth integration."""
        await self._async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {
            "name": discovery_info.name,
            "address": discovery_info.address,
        }
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": discovery_info.name,
                "address": discovery_info.address,
            },
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm discovery."""
        if user_input is not None:
            placeholders = self.context.get("title_placeholders", {})
            title = placeholders.get("name") or placeholders.get("address", "Surplife Matrix")
            return self.async_create_entry(
                title=title,
                data={CONF_ADDRESS: placeholders["address"]},
            )
        return self.async_show_form(
            step_id="confirm",
            description_placeholders=self.context.get("title_placeholders", {}),
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step: pick a discovered device or enter an address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address: str = user_input[CONF_ADDRESS].strip().upper()
            infos = await _async_discovered_devices(self.hass)
            match = next((i for i in infos
                          if i.address.upper() == address), None)
            if match is not None:
                return self.async_create_entry(
                    title=match.name,
                    data={CONF_ADDRESS: match.address},
                )
            # Manual entry: accept any address the user provides
            if len(address) == 17 and address.count(":") == 5:
                await self._async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=address,
                    data={CONF_ADDRESS: address},
                )
            errors[CONF_ADDRESS] = "invalid_address"

        discovered = await _async_discovered_devices(self.hass)
        choices = {
            info.address: f"{info.name} ({info.address})" for info in discovered
        }
        address_schema: Any = vol.Coerce(str) if not choices else vol.In(choices)
        schema = vol.Schema({
            vol.Required(CONF_ADDRESS): address_schema,
        })
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )
