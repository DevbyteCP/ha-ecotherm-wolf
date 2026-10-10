# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Number platform -- writable numeric values."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ..coordinator import EcothermWolfCoordinator
from .base import EcothermWolfEntity
from .classify import classify_number
from .health import health_numbers

_LOGGER = logging.getLogger(__name__)


def _resolve_native_step(codec: Any) -> float | None:
    """UI step derived from the codec's resolution, or ``None`` for HA's default.

    A ``ScaledCodec`` stores ``scale = 10**decimals`` -- its values move in
    ``1/scale`` increments (0.1 for one decimal). Without an explicit step
    HA defaults to 1.0 whenever ``max - min > 1``, which would make
    half-degree setpoints unreachable from the UI slider/box.
    """
    scale = getattr(codec, "scale", None)
    if isinstance(scale, int | float) and scale > 1:
        return 1.0 / float(scale)
    return None


def _resolve_range_attr(codec: Any, attr_name: str) -> float | None:
    """Look up ``attr_name`` on the codec, falling back to ``codec.base``.

    Plain numeric codecs (Int16, Uint8, Uint16) expose ``min_value`` /
    ``max_value`` directly. ScaledCodec wraps a base codec, so the bounds
    live one level deeper at ``codec.base.min_value``.
    """
    direct = getattr(codec, attr_name, None)
    if direct is not None:
        return float(direct)
    base = getattr(codec, "base", None)
    if base is None:
        return None
    nested = getattr(base, attr_name, None)
    if nested is None:
        return None
    return float(nested)


class EcothermWolfNumber(EcothermWolfEntity, NumberEntity):
    def _apply_classification(self) -> None:
        device_class, icon = classify_number(self._param)
        if device_class is not None:
            self._attr_device_class = device_class
        self._attr_icon = icon
        # Step from the codec's resolution (0.1 for one decimal). Left unset
        # for plain integer codecs so HA's default (1.0) applies.
        step = _resolve_native_step(self._param.codec)
        if step is not None:
            self._attr_native_step = step

    @property
    def native_min_value(self) -> float:
        v = _resolve_range_attr(self._param.codec, "min_value")
        # NumberEntity's superclass exposes a class-level default if we
        # cannot determine one (DEFAULT_MIN_VALUE = 0.0).
        return v if v is not None else super().native_min_value

    @property
    def native_max_value(self) -> float:
        v = _resolve_range_attr(self._param.codec, "max_value")
        return v if v is not None else super().native_max_value

    @property
    def native_unit_of_measurement(self) -> str | None:
        unit = getattr(self._param.codec, "unit", None)
        return unit if isinstance(unit, str) else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write(
            device_id=self._device_id,
            property_id=self._param.property_id,
            value=value,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    coordinator: EcothermWolfCoordinator | None = runtime.coordinator
    if coordinator is None:
        _LOGGER.warning("ecotherm_wolf number platform: coordinator missing on runtime")
        return
    entities: list[NumberEntity] = []
    for device_id, property_id in coordinator.selected:
        device_type = coordinator.device_type_for(device_id)
        param = coordinator.catalog.get_parameter(device_type, property_id)
        if param.codec.ha_platform == "number":
            entities.append(
                EcothermWolfNumber(
                    coordinator,
                    device_id=device_id,
                    device_type=device_type,
                    param=param,
                )
            )
    entities.extend(health_numbers(coordinator))
    add_entities(entities)
