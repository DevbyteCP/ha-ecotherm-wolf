# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Switch platform -- writable booleans."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ..coordinator import EcothermWolfCoordinator
from .base import EcothermWolfEntity
from .classify import classify_switch
from .health import health_switches

_LOGGER = logging.getLogger(__name__)


class EcothermWolfSwitch(EcothermWolfEntity, SwitchEntity):
    def _apply_classification(self) -> None:
        self._attr_icon = classify_switch(self._param)

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get(self._key))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(
            device_id=self._device_id,
            property_id=self._param.property_id,
            value=True,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(
            device_id=self._device_id,
            property_id=self._param.property_id,
            value=False,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    coordinator: EcothermWolfCoordinator | None = runtime.coordinator
    if coordinator is None:
        _LOGGER.warning("ecotherm_wolf switch platform: coordinator missing on runtime")
        return
    entities: list[SwitchEntity] = []
    for device_id, property_id in coordinator.selected:
        device_type = coordinator.device_type_for(device_id)
        param = coordinator.catalog.get_parameter(device_type, property_id)
        if param.codec.ha_platform == "switch":
            entities.append(
                EcothermWolfSwitch(
                    coordinator,
                    device_id=device_id,
                    device_type=device_type,
                    param=param,
                )
            )
    entities.extend(health_switches(coordinator))
    add_entities(entities)
