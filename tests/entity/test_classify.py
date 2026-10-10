# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate entity classification (device_class / state_class / icon).

The decisive guarantee here: a ``device_class`` is only ever assigned with a
unit HA's own ``DEVICE_CLASS_UNITS`` accepts -- so HA can never refuse to load
an EcoTherm Wolf entity over a class/unit mismatch. We assert that against HA's
*real* validation table for **every** parameter of **every** bundled device,
across all presets.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# Needs Wolf's XMLs/certificate (scripts/fetch_wolf_files.py --bundled).
pytestmark = pytest.mark.requires_wolf_files

pytest.importorskip("homeassistant")

from homeassistant.components.number.const import (  # noqa: E402
    DEVICE_CLASS_UNITS as NUMBER_UNITS,
)
from homeassistant.components.sensor import SensorStateClass  # noqa: E402
from homeassistant.components.sensor.const import (  # noqa: E402
    DEVICE_CLASS_UNITS as SENSOR_UNITS,
)

from custom_components.ecotherm_wolf.definitions.wolf_loader import (  # noqa: E402
    load_wolf_catalog,
)
from custom_components.ecotherm_wolf.entity.classify import (  # noqa: E402
    classify_binary_sensor,
    classify_number,
    classify_select,
    classify_sensor,
    classify_switch,
)

# Repo layout: tests/ and custom_components/ are siblings under the project root.
WOLF_DIR = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "ecotherm_wolf"
    / "definitions"
    / "data"
    / "wolf"
)


def _unit(codec) -> str | None:
    u = getattr(codec, "unit", None)
    return u if isinstance(u, str) and u else None


def _is_enum(codec) -> bool:
    return getattr(codec, "options", None) is not None


@pytest.fixture(scope="module")
def all_params():
    if not WOLF_DIR.exists():
        pytest.skip("Wolf XMLs not bundled in this checkout")
    cat = load_wolf_catalog(WOLF_DIR)
    params = []
    for dev in cat.devices.values():
        for p in dev.parameters.values():
            params.append((dev.device_type, p))
    assert params, "catalog produced no parameters"
    return params


def _valid_icon(icon: str) -> bool:
    return isinstance(icon, str) and icon.startswith("mdi:") and len(icon) > 4


def test_every_param_classifies_load_safe(all_params) -> None:
    """For all params: any assigned device_class must accept the codec's unit,
    icons are well-formed, and state_class is never put on a string sensor."""
    for device_type, p in all_params:
        plat = p.codec.ha_platform
        unit = _unit(p.codec)
        label = f"{device_type}/0x{p.property_id:04X} {p.name} unit={unit!r}"

        if plat == "sensor":
            dc, sc, icon = classify_sensor(p)
            assert _valid_icon(icon), label
            if _is_enum(p.codec) or type(p.codec).__name__ == "TimeOfDayCodec":
                # String-valued sensors: never a device_class or state_class.
                assert dc is None, label
                assert sc is None, label
            else:
                assert sc in (
                    SensorStateClass.MEASUREMENT,
                    SensorStateClass.TOTAL_INCREASING,
                ), label
                if dc is not None:
                    assert unit in SENSOR_UNITS[dc], (
                        f"{label}: device_class {dc} rejects unit {unit!r}"
                    )

        elif plat == "number":
            dc, icon = classify_number(p)
            assert _valid_icon(icon), label
            if dc is not None:
                assert unit in NUMBER_UNITS[dc], (
                    f"{label}: number device_class {dc} rejects unit {unit!r}"
                )

        elif plat == "select":
            assert _valid_icon(classify_select(p)), label

        elif plat == "switch":
            assert _valid_icon(classify_switch(p)), label

        elif plat == "binary_sensor":
            dc, icon = classify_binary_sensor(p)
            assert _valid_icon(icon), label
            # binary_sensor device classes are unit-free; nothing to validate.

        # "text" (writable time-of-day) has no platform entity -> not created.


def test_standard_preset_full_icon_coverage(all_params) -> None:
    """Every parameter that becomes an entity gets a non-empty mdi icon."""
    for device_type, p in all_params:
        plat = p.codec.ha_platform
        if plat == "sensor":
            icon = classify_sensor(p)[2]
        elif plat == "number":
            icon = classify_number(p)[1]
        elif plat == "select":
            icon = classify_select(p)
        elif plat == "switch":
            icon = classify_switch(p)
        elif plat == "binary_sensor":
            icon = classify_binary_sensor(p)[1]
        else:
            continue
        assert _valid_icon(icon), f"{device_type}/{p.name} icon={icon!r}"


def _param(cat, device_type, ptid):
    return cat.get_parameter(device_type, ptid)


@pytest.fixture(scope="module")
def catalog():
    if not WOLF_DIR.exists():
        pytest.skip("Wolf XMLs not bundled in this checkout")
    return load_wolf_catalog(WOLF_DIR)


@pytest.mark.parametrize(
    ("device", "ptid", "exp_dc", "exp_sc", "exp_icon"),
    [
        # CHA temperature sensor
        ("CHA", 0x41EB6, "temperature", "measurement", "mdi:thermometer"),
        ("CHA", 0x41EB7, "temperature", "measurement", "mdi:water-thermometer"),
        ("CHA", 0x41EB5, "temperature", "measurement", "mdi:sun-thermometer"),
        ("CHA", 0x41EB9, "temperature", "measurement", "mdi:thermometer-chevron-down"),
        # CHA energy counters -> total_increasing
        ("CHA", 0x41EEB, "energy", "total_increasing", "mdi:radiator"),
        ("CHA", 0x41EEC, "energy", "total_increasing", "mdi:water-boiler"),
        ("CHA", 0x41F51, "energy", "total_increasing", "mdi:calendar-today"),
        ("CHA", 0x41F57, "energy", "total_increasing", "mdi:calendar-arrow-left"),
        # CHA hours / counters (no device_class, total_increasing)
        ("CHA", 0x41ECA, None, "total_increasing", "mdi:timer-outline"),  # netzbetriebsstd
        ("CHA", 0x41ECB, None, "total_increasing", "mdi:timer-outline"),  # betriebsstd verd.
        ("CHA", 0x41EC9, None, "total_increasing", "mdi:counter"),  # verdichterstarts
        # CHA flow l/min lower-case -> NO device_class (would break load)
        ("CHA", 0x41ECC, None, "measurement", "mdi:water-pump"),
        # CHA pressure / power
        ("CHA", 0x41ECE, "pressure", "measurement", "mdi:gauge"),
        ("CHA", 0x41EDE, "power", "measurement", "mdi:heat-wave"),
        # CHA enum status sensors -> no dc/sc, icon only
        ("CHA", 0x41EE2, None, None, "mdi:state-machine"),  # betriebsart
        ("CHA", 0x41EE3, None, None, "mdi:engine-outline"),  # verdichterstatus
        # CWL pressure / flow / co2
        ("CWL", 0x3827C, "pressure", "measurement", "mdi:gauge"),
        ("CWL", 0x3827E, "volume_flow_rate", "measurement", "mdi:fan"),
        ("CWL", 0x382B9, "carbon_dioxide", "measurement", "mdi:molecule-co2"),
        # MK temperature / room / pool-setpoint number
        ("MK_BM-2", 0x57EB4, "temperature", "measurement", "mdi:home-thermometer"),
    ],
)
def test_sensor_spot_checks(catalog, device, ptid, exp_dc, exp_sc, exp_icon) -> None:
    p = _param(catalog, device, ptid)
    assert p.codec.ha_platform == "sensor", f"{device}/{ptid:#x} not a sensor"
    dc, sc, icon = classify_sensor(p)
    assert (dc.value if dc else None) == exp_dc
    assert (sc.value if sc else None) == exp_sc
    assert icon == exp_icon


@pytest.mark.parametrize(
    ("device", "ptid", "exp_dc", "exp_icon"),
    [
        ("MM", 0x9C47, "temperature_delta", "mdi:chart-bell-curve-cumulative"),  # heizkurve K
        ("MM", 0x9C4A, "duration", "mdi:timer-outline"),  # nachlaufzeit min
        ("CHA", 0x41EFC, "temperature_delta", "mdi:thermometer-lines"),  # hysterese K
        ("CHA", 0x41F4B, "temperature", "mdi:pool"),  # vorlauftemp poolbetrieb
        ("CHA", 0x41F00, None, "mdi:pump"),  # pumpenleistung %
        ("MK_BM-2", 0x57E48, "temperature", "mdi:thermometer"),  # eco/abs °C
    ],
)
def test_number_spot_checks(catalog, device, ptid, exp_dc, exp_icon) -> None:
    p = _param(catalog, device, ptid)
    assert p.codec.ha_platform == "number", f"{device}/{ptid:#x} not a number"
    dc, icon = classify_number(p)
    assert (dc.value if dc else None) == exp_dc
    assert icon == exp_icon


@pytest.mark.parametrize(
    ("device", "ptid", "exp_dc", "exp_icon"),
    [
        ("CHA", 0x41EC1, "running", "mdi:heat-pump"),  # verdichter
        ("CHA", 0x41EBB, "running", "mdi:pump"),  # heizkreispumpe
        ("MM", 0x9C86, "running", "mdi:pump"),  # mischerkreispumpe
        ("MM", 0x9C41, "running", "mdi:pump"),  # ausgang_mkp
        ("MM", 0x9C43, "power", "mdi:electric-switch"),  # ausgang_a1
    ],
)
def test_binary_sensor_spot_checks(catalog, device, ptid, exp_dc, exp_icon) -> None:
    p = _param(catalog, device, ptid)
    assert p.codec.ha_platform == "binary_sensor"
    dc, icon = classify_binary_sensor(p)
    assert (dc.value if dc else None) == exp_dc
    assert icon == exp_icon


@pytest.mark.parametrize(
    ("device", "ptid", "exp_icon"),
    [
        ("MK_BM-2", 0x57E50, "mdi:calendar-clock"),  # zeitprogramm select
        ("MK_BM-2", 0x57E73, "mdi:tune"),  # programmwahl select
        ("BM-2", 0x35B84, "mdi:fan"),  # programmwahl lueftung select
        ("BM-2", 0x35B82, "mdi:party-popper"),  # partymodus switch
        ("BM-2", 0x35B83, "mdi:bag-suitcase"),  # urlaubsmodus switch
    ],
)
def test_select_switch_spot_checks(catalog, device, ptid, exp_icon) -> None:
    p = _param(catalog, device, ptid)
    plat = p.codec.ha_platform
    icon = classify_select(p) if plat == "select" else classify_switch(p)
    assert icon == exp_icon
