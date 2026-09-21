"""Coordinator entity base for Surplife Matrix.

The coordinator is a passive state holder: no periodic polling (the device
firmware wedges after ~6-7 BLE connect cycles per boot). State updates are
pushed from command ACKs via `SurplifeRuntime.apply_status`.
"""

from __future__ import annotations

import logging

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .runtime import SurplifeRuntime

_LOGGER = logging.getLogger(__name__)


class SurplifeCoordinatorEntity(CoordinatorEntity):
    """Base entity wired to the runtime's status coordinator."""

    def __init__(self, runtime: SurplifeRuntime):
        super().__init__(runtime.coordinator)
        self._runtime = runtime

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._runtime.address)},
            "connections": {("bluetooth", self._runtime.address)},
            "name": self._runtime.title,
            "manufacturer": "Surplife",
            "model": "LED Matrix 96x16",
        }

