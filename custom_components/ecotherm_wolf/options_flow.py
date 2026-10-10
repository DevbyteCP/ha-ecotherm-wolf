# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Options flow -- adjust parameters / connection / advanced settings live."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_REBOOT_AFTER_FAILURES,
    CONF_REBOOT_HOST,
    CONF_REBOOT_NOTIFY_SERVICE,
    CONF_REBOOT_PASSWORD,
    CONF_REBOOT_USERNAME,
    CONF_SELECTED_PARAMETERS,
    DEFAULT_ACTIVE_RESET_ON_STALE,
    DEFAULT_DEBUG_LOGGING,
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_REBOOT_AFTER_FAILURES,
    DEFAULT_REBOOT_USERNAME,
    DEFAULT_STALE_THRESHOLD_SECONDS,
)


class EcothermWolfOptionsFlow(OptionsFlow):
    """Three-tab options flow: parameters, connection, advanced."""

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._new_options: dict[str, Any] = dict(entry.options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the menu with the three tabs."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["parameters", "connection", "advanced", "reboot"],
        )

    async def async_step_parameters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Tab 1: change which parameters are subscribed; applies live diff.

        The picker groups parameters per device -- the list is sorted by
        device id, then property id, so a device's rows stay contiguous and
        read like a block under its device-type prefix -- and shows each
        parameter's current live value next to its name. The cryptic
        ``device_id/property_id`` pair only lives in the option *value*
        (hidden from the user); the visible *label* stays human-readable.
        """
        coordinator = self.entry.runtime_data.coordinator
        assert coordinator is not None, "options flow requires running coordinator"
        catalog = coordinator.catalog
        device_map = coordinator.device_map

        if user_input is not None:
            selection: list[tuple[int, int]] = []
            for value in user_input.get("selected", []):
                parsed = _parse_param_value(value)
                if parsed is not None:
                    selection.append(parsed)
            await coordinator.apply_selection(selection)
            self._new_options[CONF_SELECTED_PARAMETERS] = [
                [str(d), f"0x{p:04X}"] for d, p in selection
            ]
            return self.async_create_entry(title="", data=self._new_options)

        # Build the option list grouped per device (sorted by device id),
        # each row carrying the parameter's current live value.
        options: list[SelectOptionDict] = []
        for dev_id in sorted(device_map):
            dev_type = device_map[dev_id][0]
            try:
                device = catalog.get_device(dev_type)
            except KeyError:
                continue
            for p in sorted(
                device.parameters.values(), key=lambda x: x.property_id,
            ):
                current = coordinator.data.get((dev_id, p.property_id))
                options.append(SelectOptionDict(
                    value=f"{dev_id}/{p.property_id:04X}",
                    label=_param_option_label(dev_type, p.display_name, current),
                ))

        # Default = the currently-subscribed values, but only those that
        # still resolve to a real option (a selected parameter whose device
        # vanished from the catalog would otherwise be an invalid default).
        valid_values = {opt["value"] for opt in options}
        current_values = [
            value
            for d, p in coordinator.selected
            if (value := f"{d}/{p:04X}") in valid_values
        ]

        return self.async_show_form(
            step_id="parameters",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "selected", default=current_values,
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Tab 2: change host/port/password (rare); writes to entry.data."""
        if user_input is not None:
            data = dict(self.entry.data)
            data[CONF_HOST] = user_input[CONF_HOST]
            data[CONF_PORT] = user_input[CONF_PORT]
            data[CONF_PASSWORD] = user_input[CONF_PASSWORD]
            self.hass.config_entries.async_update_entry(self.entry, data=data)
            return self.async_create_entry(title="", data=self._new_options)

        return self.async_show_form(
            step_id="connection",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST, default=self.entry.data[CONF_HOST]
                    ): str,
                    vol.Required(
                        CONF_PORT, default=self.entry.data[CONF_PORT]
                    ): int,
                    vol.Required(
                        CONF_PASSWORD, default=self.entry.data[CONF_PASSWORD]
                    ): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Tab 3: tuning settings (heartbeat, stale threshold, active reset, debug)."""
        if user_input is not None:
            self._new_options.update(user_input)
            return self.async_create_entry(title="", data=self._new_options)

        opts = self.entry.options
        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "heartbeat_seconds",
                        default=opts.get(
                            "heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=10, max=600, step=10, unit_of_measurement="s"
                        )
                    ),
                    vol.Required(
                        "stale_threshold_seconds",
                        default=opts.get(
                            "stale_threshold_seconds",
                            DEFAULT_STALE_THRESHOLD_SECONDS,
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=60, max=3600, step=30, unit_of_measurement="s"
                        )
                    ),
                    vol.Required(
                        "active_reset_on_stale",
                        default=opts.get(
                            "active_reset_on_stale", DEFAULT_ACTIVE_RESET_ON_STALE
                        ),
                    ): BooleanSelector(),
                    vol.Required(
                        "debug_logging",
                        default=opts.get("debug_logging", DEFAULT_DEBUG_LOGGING),
                    ): BooleanSelector(),
                }
            ),
        )

    async def async_step_reboot(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Tab 4: ISM7 reboot watchdog.

        When the auto-reconnect can't re-establish the link after N
        consecutive attempts, the integration power-cycles the gateway over
        its HTTP firmware endpoint (``/protect/reboot.htm``). Inert until a
        reboot password is entered. Threshold 0 disables it outright. The
        host defaults to the gateway host; the username defaults to ``admin``.
        """
        if user_input is not None:
            self._new_options.update(user_input)
            return self.async_create_entry(title="", data=self._new_options)

        opts = self.entry.options
        return self.async_show_form(
            step_id="reboot",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_REBOOT_AFTER_FAILURES,
                        default=opts.get(
                            CONF_REBOOT_AFTER_FAILURES,
                            DEFAULT_REBOOT_AFTER_FAILURES,
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(min=0, max=50, step=1)
                    ),
                    vol.Optional(
                        CONF_REBOOT_HOST,
                        default=opts.get(CONF_REBOOT_HOST)
                        or self.entry.data.get(CONF_HOST, ""),
                    ): str,
                    vol.Optional(
                        CONF_REBOOT_USERNAME,
                        default=opts.get(
                            CONF_REBOOT_USERNAME, DEFAULT_REBOOT_USERNAME
                        ),
                    ): str,
                    vol.Optional(
                        CONF_REBOOT_PASSWORD,
                        default=opts.get(CONF_REBOOT_PASSWORD, ""),
                    ): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Optional(
                        CONF_REBOOT_NOTIFY_SERVICE,
                        default=opts.get(CONF_REBOOT_NOTIFY_SERVICE, ""),
                    ): str,
                }
            ),
        )


def _param_option_label(
    dev_type: str, display_name: str, value: Any,
) -> str:
    """Human-readable label for one parameter row in the options picker.

    ``"BM-2 · Betriebsart Heizgerät — Heizen"``. The device-type prefix
    groups the rows visually (the list is sorted by device); the trailing
    value is the parameter's current live reading, omitted when the
    parameter has no value yet (not subscribed, or not pushed since setup).
    """
    head = f"{dev_type} · {display_name}"
    if value is None:
        return head
    return f"{head} — {value}"


def _parse_param_value(value: str) -> tuple[int, int] | None:
    """Parse a ``"<device_id>/<property_id_hex>"`` option value.

    Returns ``None`` for anything malformed so a stray value can never
    crash the options flow on submit.
    """
    try:
        dev_str, prop_str = value.split("/", 1)
        return int(dev_str), int(prop_str, 16)
    except (ValueError, AttributeError):
        return None
