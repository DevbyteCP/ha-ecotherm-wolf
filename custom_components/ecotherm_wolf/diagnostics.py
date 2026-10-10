# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Diagnostics export for the EcoTherm Wolf integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PASSWORD, CONF_REBOOT_PASSWORD

REDACT_DATA_KEYS = {CONF_PASSWORD}
# ``entry.options`` carries the reboot watchdog's credentials (the ISM7
# web-UI admin password) since 1.0.5 -- diagnostics downloads get attached
# to GitHub issues, so this MUST be redacted just like the ISM7 password.
REDACT_OPTIONS_KEYS = {CONF_REBOOT_PASSWORD}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostic data for the config entry. Redacts all passwords."""
    diagnostics: dict[str, Any] = {
        "entry_data": async_redact_data(dict(entry.data), REDACT_DATA_KEYS),
        "entry_options": async_redact_data(
            dict(entry.options), REDACT_OPTIONS_KEYS,
        ),
    }

    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return diagnostics

    diagnostics["connected"] = runtime.client.connected
    diagnostics["catalog_devices"] = list(runtime.catalog.devices.keys())

    coord = runtime.coordinator
    if coord is not None:
        diagnostics.update(
            {
                "device_map": {
                    str(k): {
                        "device_type": v[0],
                        "read_address": v[1],
                        "write_address": v[2],
                    }
                    for k, v in coord.device_map.items()
                },
                "selected": [(d, f"0x{p:04X}") for d, p in coord.selected],
                "values_cached": len(coord.data),
                "last_push_at": (
                    coord.last_push_at.isoformat() if coord.last_push_at else None
                ),
                "stale_minutes": coord.stale_minutes,
                "reconnect_count": coord.reconnect_count,
                "write_errors_24h": coord.write_errors_count_24h,
            }
        )

    return diagnostics
