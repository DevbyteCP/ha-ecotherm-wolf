# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Sensor platform -- read-only numeric and enum read-only values."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ..coordinator import EcothermWolfCoordinator
from .base import EcothermWolfEntity
from .classify import classify_sensor
from .health import health_sensors

_LOGGER = logging.getLogger(__name__)


class EcothermWolfSensor(EcothermWolfEntity, SensorEntity):
    def _apply_classification(self) -> None:
        device_class, state_class, icon = classify_sensor(self._param)
        if device_class is not None:
            self._attr_device_class = device_class
        if state_class is not None:
            self._attr_state_class = state_class
        self._attr_icon = icon

    @property
    def native_unit_of_measurement(self) -> str | None:
        unit = getattr(self._param.codec, "unit", None)
        return unit if isinstance(unit, str) else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    coordinator: EcothermWolfCoordinator | None = runtime.coordinator
    if coordinator is None:
        _LOGGER.warning("ecotherm_wolf sensor platform: coordinator missing on runtime")
        return
    entities: list[SensorEntity] = []
    for device_id, property_id in coordinator.selected:
        device_type = coordinator.device_type_for(device_id)
        param = coordinator.catalog.get_parameter(device_type, property_id)
        if param.codec.ha_platform == "sensor":
            entities.append(
                EcothermWolfSensor(
                    coordinator,
                    device_id=device_id,
                    device_type=device_type,
                    param=param,
                )
            )
    entities.extend(health_sensors(coordinator))
    add_entities(entities)
