# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Health pseudo-entities exposing connection / liveness state."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import (
    DEFAULT_HEARTBEAT_MAX_SECONDS,
    DEFAULT_HEARTBEAT_MIN_SECONDS,
    DEFAULT_HEARTBEAT_STEP_SECONDS,
    DOMAIN,
    MANUFACTURER,
    SW_VERSION,
)
from ..coordinator import EcothermWolfCoordinator


class _HealthEntity(CoordinatorEntity[EcothermWolfCoordinator]):
    """Shared init wiring for the health-device pseudo-entities.

    Health entities are diagnostics: they describe the integration's state
    (connection up, last push, reconnect count, ...). HA filters
    ``EntityCategory.DIAGNOSTIC`` entities out of the main dashboard tiles
    and the energy / climate dashboards, which matches user expectations.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: EcothermWolfCoordinator,
        *,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        # ``config_entry`` is typed as Optional but is always set during
        # ``async_setup_entry`` -- the platform iterates the coordinator
        # only after HA has wired the entry.
        assert coordinator.config_entry is not None
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_health_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_health")},
            name="EcoTherm Wolf Health",
            manufacturer=MANUFACTURER,
            model="EcoTherm Wolf",
            sw_version=SW_VERSION,
        )


class EcothermWolfConnectedBinarySensor(_HealthEntity, BinarySensorEntity):
    """True while the underlying ISM7 TCP connection is up."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool:
        return self.coordinator.client_connected


class EcothermWolfConnectFailedBinarySensor(_HealthEntity, BinarySensorEntity):
    """ON when the ISM7 link can't be re-established (sustained outage).

    Unlike ``Connected`` (which flips off on any brief disconnect), this
    only turns ON after the auto-reconnect has kept failing past the repair
    threshold -- a genuine "gateway unreachable" fault worth a push. Wire it
    into a notification.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:lan-disconnect"

    @property
    def is_on(self) -> bool:
        return self.coordinator.connect_failed


class EcothermWolfLastUpdateSensor(_HealthEntity, SensorEntity):
    """Timestamp of the most recent value-update push from the ISM7."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_push_at


class EcothermWolfStaleMinutesSensor(_HealthEntity, SensorEntity):
    """Minutes since the last push exceeded the stale threshold (else 0)."""

    _attr_native_unit_of_measurement = "min"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-alert-outline"

    @property
    def native_value(self) -> int:
        return self.coordinator.stale_minutes


class EcothermWolfReconnectCountSensor(_HealthEntity, SensorEntity):
    """Total reconnects since this entry was set up."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:restart"

    @property
    def native_value(self) -> int:
        return self.coordinator.reconnect_count


class EcothermWolfWriteErrors24hSensor(_HealthEntity, SensorEntity):
    """Rolling 24h count of failed writes (timeout / rejected / no ack)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:alert-circle-outline"

    @property
    def native_value(self) -> int:
        return self.coordinator.write_errors_count_24h


class EcothermWolfHeartbeatNumber(_HealthEntity, NumberEntity):
    """Adjustable push interval (Number entity at the Health device).

    Persists via ``entry.options['heartbeat_seconds']``; the
    ``_async_options_updated`` listener then reloads the entry so the
    gateway receives a fresh subscribe with the new ``Intervall``.
    Calling ``set_heartbeat_seconds`` on the live coordinator is best-
    effort -- the reload below is the authoritative path.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = float(DEFAULT_HEARTBEAT_MIN_SECONDS)
    _attr_native_max_value = float(DEFAULT_HEARTBEAT_MAX_SECONDS)
    _attr_native_step = float(DEFAULT_HEARTBEAT_STEP_SECONDS)
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:timer-cog-outline"

    @property
    def native_value(self) -> float:
        return float(self.coordinator.heartbeat_seconds)

    async def async_set_native_value(self, value: float) -> None:
        new = int(value)
        # Apply to the live coordinator first so a fast user can see the
        # change before the reload completes.
        await self.coordinator.set_heartbeat_seconds(new)
        # Persist for restart. The update listener triggers a reload that
        # rebuilds the coordinator with the new value.
        entry = self.coordinator.config_entry
        assert entry is not None
        self.hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, "heartbeat_seconds": new},
        )


class EcothermWolfActiveSwitch(_HealthEntity, SwitchEntity):
    """Pause / resume the integration's connection.

    OFF stops the KeepAlive + stale-watchdog and disconnects the socket;
    ON reconnects, re-runs the 2-stage init, and restarts the tasks. The
    switch ONLY persists ``entry.options['is_active']`` -- the options
    update listener then reloads the entry, and the rebuilt coordinator /
    client apply the new state. (``coordinator.async_set_active`` exists
    for programmatic use but is deliberately NOT called here: the reload
    is the single authoritative path.)
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:lan-connect"

    @property
    def is_on(self) -> bool:
        return self.coordinator.is_active

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, active: bool) -> None:
        # Persist the new value; the listener reloads the entry so the
        # fresh coordinator picks it up.
        entry = self.coordinator.config_entry
        assert entry is not None
        if entry.options.get("is_active", True) == active:
            return
        self.hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, "is_active": active},
        )


class EcothermWolfRebootWatchdogSwitch(_HealthEntity, SwitchEntity, RestoreEntity):
    """Runtime on/off for the automatic ISM7 gateway reboot.

    The reboot watchdog (configured under Options -> ISM7-Reboot-Watchdog)
    power-cycles the gateway after repeated reconnect failures. This switch
    is a quick override: turn it off to suppress the auto-reboot without
    touching the options -- the normal reconnect keeps running either way.

    Persisted via ``RestoreEntity`` (not ``entry.options``) so toggling it
    does NOT reload the entry / drop the live ISM7 connection. The choice is
    restored after a HA restart and pushed back onto the coordinator.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:restart"

    @property
    def is_on(self) -> bool:
        return self.coordinator.reboot_watchdog_enabled

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self.coordinator.set_reboot_watchdog_enabled(last.state == "on")

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_reboot_watchdog_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_reboot_watchdog_enabled(False)
        self.async_write_ha_state()


def health_sensors(
    coordinator: EcothermWolfCoordinator,
) -> list[SensorEntity]:
    """Factory: build the four health sensor entities for this coordinator."""
    sensors: list[Any] = [
        EcothermWolfLastUpdateSensor(
            coordinator, key="last_update", name="Last Update"
        ),
        EcothermWolfStaleMinutesSensor(
            coordinator, key="stale_minutes", name="Stale Minutes"
        ),
        EcothermWolfReconnectCountSensor(
            coordinator, key="reconnect_count", name="Reconnect Count"
        ),
        EcothermWolfWriteErrors24hSensor(
            coordinator, key="write_errors_24h", name="Write Errors 24h"
        ),
    ]
    return sensors


def health_binary_sensors(
    coordinator: EcothermWolfCoordinator,
) -> list[BinarySensorEntity]:
    """Factory: build the connectivity binary-sensor entity for this coordinator."""
    sensors: list[Any] = [
        EcothermWolfConnectedBinarySensor(
            coordinator, key="connected", name="Connected"
        ),
        EcothermWolfConnectFailedBinarySensor(
            coordinator,
            key="connect_failed",
            name="Kritischer Fehler (Connect nicht möglich)",
        ),
    ]
    return sensors


def health_numbers(
    coordinator: EcothermWolfCoordinator,
) -> list[NumberEntity]:
    """Factory: build the heartbeat Number entity for this coordinator."""
    return [
        EcothermWolfHeartbeatNumber(
            coordinator, key="heartbeat_seconds", name="Update-Intervall",
        ),
    ]


def health_switches(
    coordinator: EcothermWolfCoordinator,
) -> list[SwitchEntity]:
    """Factory: build the Health-device switch entities for this coordinator."""
    return [
        EcothermWolfActiveSwitch(
            coordinator, key="active", name="Verbindung aktiv",
        ),
        EcothermWolfRebootWatchdogSwitch(
            coordinator, key="reboot_watchdog", name="ISM7 Auto-Reboot",
        ),
    ]
