# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Binary sensor platform -- read-only booleans."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ..coordinator import EcothermWolfCoordinator
from .base import EcothermWolfEntity
from .classify import classify_binary_sensor
from .health import health_binary_sensors

_LOGGER = logging.getLogger(__name__)


class EcothermWolfBinarySensor(EcothermWolfEntity, BinarySensorEntity):
    def _apply_classification(self) -> None:
        device_class, icon = classify_binary_sensor(self._param)
        if device_class is not None:
            self._attr_device_class = device_class
        self._attr_icon = icon

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get(self._key))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    coordinator: EcothermWolfCoordinator | None = runtime.coordinator
    if coordinator is None:
        _LOGGER.warning(
            "ecotherm_wolf binary_sensor platform: coordinator missing on runtime"
        )
        return
    entities: list[BinarySensorEntity] = []
    for device_id, property_id in coordinator.selected:
        device_type = coordinator.device_type_for(device_id)
        param = coordinator.catalog.get_parameter(device_type, property_id)
        if param.codec.ha_platform == "binary_sensor":
            entities.append(
                EcothermWolfBinarySensor(
                    coordinator,
                    device_id=device_id,
                    device_type=device_type,
                    param=param,
                )
            )
    entities.extend(health_binary_sensors(coordinator))
    add_entities(entities)
