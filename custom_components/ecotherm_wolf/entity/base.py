# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Common base for EcoTherm Wolf entities."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN, MANUFACTURER, SW_VERSION
from ..coordinator import EcothermWolfCoordinator
from ..definitions.catalog import ParameterDefinition


class EcothermWolfEntity(CoordinatorEntity[EcothermWolfCoordinator]):
    """Base class for all EcoTherm Wolf entities.

    Provides shared init wiring (unique_id, name, device_info) and the
    cross-cutting ``native_value`` / ``available`` lookups against the
    coordinator's value cache.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EcothermWolfCoordinator,
        *,
        device_id: int,
        device_type: str,
        param: ParameterDefinition,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._device_type = device_type
        self._param = param
        # ``config_entry`` is typed as Optional but is always set during
        # ``async_setup_entry`` -- the platform iterates the coordinator
        # only after HA has wired the entry.
        assert coordinator.config_entry is not None
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = (
            f"{entry_id}_{device_id}_{param.property_id:04x}"
        )
        self._attr_name = param.display_name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{device_id}")},
            name=coordinator.catalog.get_device(device_type).display_name,
            manufacturer=MANUFACTURER,
            model=device_type,
            sw_version=SW_VERSION,
        )
        # Platform subclasses set _attr_device_class / _attr_state_class /
        # _attr_icon from ``classify`` so every entity shows a meaningful icon
        # (and proper class/stats) instead of the generic sensor glyph. The
        # entity_id / name / unique_id above are deliberately left untouched.
        self._apply_classification()

    def _apply_classification(self) -> None:
        """Hook: platform subclasses assign presentation metadata. No-op by default."""

    @property
    def _key(self) -> tuple[int, int]:
        return (self._device_id, self._param.property_id)

    @property
    def native_value(self) -> Any:
        return self.coordinator.data.get(self._key)

    @property
    def available(self) -> bool:
        # Deliberately holds the last value while the ISM7 link is down:
        # ecotherm_core keeps running its logic on the (frozen) readings
        # rather than going blind -- it can't command the Wolf during an
        # outage anyway, and a stable last value keeps core's view
        # consistent. Link recovery is handled by the client supervisor
        # (auto-reconnect after a peer reset); see ism7/client._supervise.
        return (
            self.coordinator.last_update_success
            and self._key in self.coordinator.data
        )
