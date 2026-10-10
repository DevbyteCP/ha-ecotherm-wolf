# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Pure unit tests for the options-flow helpers (no HA fixture needed).

The "Werte anpassen" picker hides the machine-readable
``<device_id>/<property_id_hex>`` pair in each option's ``value`` and shows
a human-readable ``label``. ``_parse_param_value`` turns the value back
into ``(device_id, property_id)`` on submit -- a malformed value must never
crash the flow. ``_param_option_label`` builds the visible row label,
device-type-prefixed for visual grouping and suffixed with the current
live value when one is available.
"""
from __future__ import annotations

from custom_components.ecotherm_wolf.options_flow import (
    _param_option_label,
    _parse_param_value,
)


class TestParseParamValue:
    def test_round_trip(self) -> None:
        # The picker emits "<device_id>/<property_id:04X>".
        assert _parse_param_value("1/0080") == (1, 0x0080)
        assert _parse_param_value("12/41EE") == (12, 0x41EE)

    def test_malformed_returns_none(self) -> None:
        # A stray / malformed value must not raise -- the flow drops it.
        assert _parse_param_value("") is None
        assert _parse_param_value("nope") is None
        assert _parse_param_value("1/") is None
        assert _parse_param_value("/0080") is None
        assert _parse_param_value("1/ZZ") is None
        assert _parse_param_value("1/0080/extra") is None


class TestParamOptionLabel:
    def test_with_value(self) -> None:
        # The device-type prefix groups the rows (the list is sorted by
        # device); the trailing value is the parameter's current reading.
        assert (
            _param_option_label("BM-2", "Betriebsart Heizgerät", "Heizen")
            == "BM-2 · Betriebsart Heizgerät — Heizen"
        )
        assert (
            _param_option_label("CWL", "Abluftdruck", 255.0)
            == "CWL · Abluftdruck — 255.0"
        )

    def test_without_value(self) -> None:
        # No value yet (not subscribed, or not pushed since setup) -> just
        # the device-prefixed name, no trailing separator.
        assert (
            _param_option_label("CHA", "Vorlauftemperatur", None)
            == "CHA · Vorlauftemperatur"
        )
