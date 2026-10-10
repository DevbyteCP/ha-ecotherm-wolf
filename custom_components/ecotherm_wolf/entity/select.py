# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Select platform -- writable enums."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ..coordinator import EcothermWolfCoordinator
from .base import EcothermWolfEntity
from .classify import classify_select

_LOGGER = logging.getLogger(__name__)


class EcothermWolfSelect(EcothermWolfEntity, SelectEntity):
    def _apply_classification(self) -> None:
        self._attr_icon = classify_select(self._param)

    @property
    def options(self) -> list[str]:
        opts = self._param.codec.options
        return list(opts)

    @property
    def current_option(self) -> str | None:
        v = self.coordinator.data.get(self._key)
        return v if isinstance(v, str) else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_write(
            device_id=self._device_id,
            property_id=self._param.property_id,
            value=option,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    coordinator: EcothermWolfCoordinator | None = runtime.coordinator
    if coordinator is None:
        _LOGGER.warning("ecotherm_wolf select platform: coordinator missing on runtime")
        return
    entities: list[EcothermWolfSelect] = []
    for device_id, property_id in coordinator.selected:
        device_type = coordinator.device_type_for(device_id)
        param = coordinator.catalog.get_parameter(device_type, property_id)
        if param.codec.ha_platform == "select":
            entities.append(
                EcothermWolfSelect(
                    coordinator,
                    device_id=device_id,
                    device_type=device_type,
                    param=param,
                )
            )
    add_entities(entities)
