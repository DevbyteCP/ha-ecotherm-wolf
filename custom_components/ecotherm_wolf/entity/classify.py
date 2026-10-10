# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Derive HA ``device_class`` / ``state_class`` / ``icon`` for a parameter.

EcoTherm Wolf entities are generated dynamically from the Wolf catalog -- there
are no static ``EntityDescription`` objects to hang metadata on. This module is
the single source of truth that maps a :class:`ParameterDefinition` (its codec's
unit / options / platform and its stable snake_case ``name``) to the presentation
metadata HA needs so every entity shows a meaningful icon instead of the generic
sensor glyph.

**Correctness rule (load-safe):** a ``device_class`` is only ever assigned when
HA's own ``DEVICE_CLASS_UNITS`` table accepts the codec's *exact* unit string for
that class. The mapping in :data:`_UNIT_DEVICE_CLASS` was verified against
homeassistant 2026.5/2026.6 -- both the ``sensor`` and ``number`` validators use
the same accepted-unit sets for these classes. Units HA does **not** map to a
class (``l/min`` lower-case, ``U/min``, ``%``, ``Std``, ``sec``, ``µA`` with the
MICRO SIGN, ...) get an icon only, so HA never refuses to load an entity over a
``device_class`` / unit mismatch. ``state_class`` is set only on *numeric*
sensors.

Entity IDs are **not** affected -- this only sets ``_attr_device_class`` /
``_attr_state_class`` / ``_attr_icon`` on the entity instances.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.number import NumberDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from ..definitions.catalog import ParameterDefinition
from ..definitions.codecs import TimeOfDayCodec

# --------------------------------------------------------------------------- #
# Unit -> device_class. Only units HA's DEVICE_CLASS_UNITS accepts for the
# class are listed (verified against the running HA). Both the sensor and the
# number platform validate these units the same way, so one map drives both;
# the public helpers look up the concrete enum member by value.
#
# Deliberately NOT mapped (icon-only, no device_class):
#   'l/min' (lower-case l; HA only accepts 'L/min' for volume_flow_rate)
#   'U/min', 'rpm'        -- no rotational-speed device_class in HA
#   '%'                   -- battery/humidity/moisture/power_factor: none apt
#   'Std', 'sec', 'Tage', 'Wochen'  -- HA duration wants d/h/min/s/ms
#   'µA' (MICRO SIGN U+00B5; HA's current accepts 'μA' GREEK MU U+03BC)
#   'Cent/kWh', 'pls/kWh', 'l/Impuls', 'ml/Impuls', 'K/%', 'K/K', 'K/(K*h)'
# --------------------------------------------------------------------------- #
_UNIT_DEVICE_CLASS: dict[str, str] = {
    "°C": "temperature",
    "K": "temperature_delta",
    "bar": "pressure",
    "Pa": "pressure",
    "kW": "power",
    "kWh": "energy",
    "Hz": "frequency",
    "V": "voltage",
    "mA": "current",
    "ppm": "carbon_dioxide",
    "m³/h": "volume_flow_rate",
    "L/min": "volume_flow_rate",
    "h": "duration",
    "min": "duration",
    "s": "duration",
}


def _unit(codec: object) -> str | None:
    u = getattr(codec, "unit", None)
    return u if isinstance(u, str) and u else None


def _is_enum(codec: object) -> bool:
    """An enum-valued codec exposes ``.options`` (numeric codecs do not)."""
    return getattr(codec, "options", None) is not None


def _is_time(codec: object) -> bool:
    return isinstance(codec, TimeOfDayCodec)


def _sensor_device_class(unit: str | None) -> SensorDeviceClass | None:
    if unit is None:
        return None
    name = _UNIT_DEVICE_CLASS.get(unit)
    return SensorDeviceClass(name) if name else None


def _number_device_class(unit: str | None) -> NumberDeviceClass | None:
    if unit is None:
        return None
    name = _UNIT_DEVICE_CLASS.get(unit)
    return NumberDeviceClass(name) if name else None


def _sensor_state_class(name: str, unit: str | None) -> SensorStateClass:
    """Numeric sensors only. Cumulative meters -> total_increasing, else measurement.

    ``kWh`` energy counters and ``Std`` operating-hour counters are monotonic
    (with periodic resets that ``total_increasing`` handles), as are the
    compressor-start counters. Everything else (temperatures, pressures,
    powers, flows, percentages, speeds) is an instantaneous measurement.
    """
    n = name.lower()
    if unit in ("kWh", "Std") or "starts" in n or "betriebsstunden" in n:
        return SensorStateClass.TOTAL_INCREASING
    return SensorStateClass.MEASUREMENT


# --------------------------------------------------------------------------- #
# Icon resolution. An ordered waterfall over the stable snake_case name (with a
# few unit-aware refinements) so every entity gets a deliberate glyph; a
# category fallback at the end guarantees coverage for any parameter -- incl.
# ones outside the curated "standard" preset.
# --------------------------------------------------------------------------- #
def _temperature_icon(n: str) -> str:
    if "warmwasser" in n:
        return "mdi:water-thermometer"
    if "aussentemp" in n:
        return "mdi:sun-thermometer"
    if "raumtemp" in n:
        return "mdi:home-thermometer"
    if "pool" in n:
        return "mdi:pool"
    if "ruecklauf" in n:
        return "mdi:thermometer-chevron-down"
    if "vorlauf" in n:
        return "mdi:thermometer-chevron-up"
    if "kuehl" in n:
        return "mdi:snowflake-thermometer"
    if "heizregister" in n:
        return "mdi:radiator"
    if "minimalbegrenzung" in n or "tv_min" in n or "starttemperatur" in n:
        return "mdi:thermometer-chevron-down"
    if "maximalbegrenzung" in n or "tv_max" in n or "endtemperatur" in n:
        return "mdi:thermometer-chevron-up"
    return "mdi:thermometer"


def _icon(param: ParameterDefinition) -> str:
    codec = param.codec
    n = param.name.lower()
    unit = _unit(codec)
    platform = codec.ha_platform

    # --- operating-hour / start counters (before the greedy 'verdichter') -- #
    if "betriebsstunden" in n:  # also matches netzbetriebsstunden
        return "mdi:timer-outline"
    if "verdichterstarts" in n or ("verdichter" in n and "starts" in n):
        return "mdi:counter"

    # --- rotating machinery: pumps, compressor, fans, motors --------------- #
    if "verdichterstatus" in n:
        return "mdi:engine-outline"
    if "verdichter" in n:
        return "mdi:heat-pump"
    if "ventilator" in n:
        return "mdi:fan"
    if "drehzahl_zhp" in n or "pumpenleistung" in n:
        return "mdi:pump"
    if "drehzahl" in n:
        return "mdi:speedometer"
    if "mischermotor" in n:
        return "mdi:valve"
    if "pumpe" in n or "_mkp" in n or n.endswith("mkp") or "kreispumpe" in n:
        return "mdi:pump"

    # --- temperatures (any °C value, or a *temperatur* name) --------------- #
    if unit == "°C" or "temperatur" in n:
        return _temperature_icon(n)

    # --- temperature differences (K) --------------------------------------- #
    if unit == "K":
        if "hysterese" in n:
            return "mdi:thermometer-lines"
        if "ueberhoehung" in n:
            return "mdi:thermometer-plus"
        if "heizkurve" in n or "p_bereich" in n:
            return "mdi:chart-bell-curve-cumulative"
        return "mdi:thermometer-lines"

    # --- pressure ----------------------------------------------------------- #
    if unit in ("bar", "Pa"):
        return "mdi:gauge"

    # --- power / energy ----------------------------------------------------- #
    if unit == "kW":
        return "mdi:heat-wave" if "heizleistung" in n else "mdi:flash"
    if unit == "kWh":
        if ("ww" in n and "waermemenge" not in n) or "energiemenge_ww" in n:
            return "mdi:water-boiler"
        if "waermemenge" in n:
            if "vorjahr" in n:
                return "mdi:calendar-arrow-left"
            if "jahr" in n:
                return "mdi:calendar-blank"
            if "monat" in n:
                return "mdi:calendar-month"
            if "tag" in n:
                return "mdi:calendar-today"
            return "mdi:radiator"
        return "mdi:radiator" if "hz" in n else "mdi:lightning-bolt"

    if unit == "Hz":
        return "mdi:sine-wave"
    if unit == "V":
        return "mdi:flash"
    if unit == "mA" or unit == "µA":
        return "mdi:current-dc"
    if unit == "ppm" or "co2" in n:
        return "mdi:molecule-co2"

    # --- flows -------------------------------------------------------------- #
    if "luftdurchsatz" in n or unit == "m³/h":
        return "mdi:fan"
    if "durchfluss" in n or unit in ("l/min", "L/min"):
        return "mdi:water-pump"

    # --- ventilation / humidity -------------------------------------------- #
    if "intensivlueftung" in n:
        return "mdi:fan-plus"
    if "lueftung" in n or "luft" in n:
        return "mdi:fan"
    if "feuchte" in n or "rh_sensor" in n:
        return "mdi:water-percent"

    # --- valves / mixer / IO ------------------------------------------------ #
    # (mixer-*circuit* status/demand enums fall through to the status section
    # below; only real valve/flap hardware is matched here)
    if "ventil" in n or "klappe" in n or "bypass" in n:
        return "mdi:valve"
    if "ausgang" in n or "eingang" in n:
        return "mdi:electric-switch"
    if "wrg" in n or "heizregister" in n or "e_heizung" in n or "ehz" in n:
        return "mdi:heating-coil"

    # --- modes / programs / one-shots -------------------------------------- #
    if "zeitprogramm" in n:
        return "mdi:calendar-clock"
    if "programmwahl" in n:
        return "mdi:fan" if "lueftung" in n else "mdi:tune"
    if "partymodus" in n:
        return "mdi:party-popper"
    if "urlaubsmodus" in n:
        return "mdi:bag-suitcase"
    if "warmwasser" in n:
        return "mdi:water-boiler"
    if "frost" in n:
        return "mdi:snowflake-alert"
    if "kuehl" in n:
        return "mdi:snowflake"

    # --- counters / timers / settings -------------------------------------- #
    if "starts" in n:
        return "mdi:counter"
    # ("betriebsstunden" is already handled at the very top of the waterfall.)
    if unit in ("Std", "h", "min", "s", "sec"):
        return "mdi:timer-outline"
    if "sparfaktor" in n:
        return "mdi:leaf"
    if "sollwertkorrektur" in n:
        return "mdi:tune"
    if "konfiguration" in n or "konstant" in n:
        return "mdi:cog"

    # --- status enums ------------------------------------------------------- #
    if _is_enum(codec):
        if "betriebsart" in n or "kreisart" in n:
            return "mdi:state-machine"
        if "anforderung" in n or "status" in n or "mischerkreis" in n:
            return "mdi:list-status"
        return "mdi:format-list-bulleted"

    # --- category fallbacks (guarantee no generic sensor glyph) ------------ #
    if _is_time(codec):
        return "mdi:clock-outline"
    if unit == "%":
        return "mdi:percent"
    if platform == "binary_sensor":
        return "mdi:checkbox-marked-circle-outline"
    if platform == "switch":
        return "mdi:toggle-switch-variant"
    if platform == "select":
        return "mdi:tune"
    if platform == "number":
        return "mdi:cog"
    return "mdi:counter"


# --------------------------------------------------------------------------- #
# Public classifiers -- one per platform.
# --------------------------------------------------------------------------- #
def classify_sensor(
    param: ParameterDefinition,
) -> tuple[SensorDeviceClass | None, SensorStateClass | None, str]:
    """Return ``(device_class, state_class, icon)`` for a read-only sensor.

    Enum / time-of-day sensors are string-valued: no ``device_class`` (HA's
    ``enum`` class would warn on the codec's ``unknown_N`` fallbacks) and no
    ``state_class`` (numeric-only). Numeric sensors get a unit-matched class
    where valid plus the appropriate ``state_class``.
    """
    codec = param.codec
    icon = _icon(param)
    if _is_enum(codec) or _is_time(codec):
        return None, None, icon
    unit = _unit(codec)
    return _sensor_device_class(unit), _sensor_state_class(param.name, unit), icon


def classify_number(
    param: ParameterDefinition,
) -> tuple[NumberDeviceClass | None, str]:
    """Return ``(device_class, icon)`` for a writable number.

    ``NumberEntity`` has no ``state_class``; a unit-matched ``device_class`` is
    assigned where HA accepts the unit, otherwise icon only.
    """
    return _number_device_class(_unit(param.codec)), _icon(param)


def classify_binary_sensor(
    param: ParameterDefinition,
) -> tuple[BinarySensorDeviceClass | None, str]:
    """Return ``(device_class, icon)`` for a read-only boolean.

    Pumps and the compressor are ``running``; generic relay outputs are
    ``power``; anything else gets an icon only.
    """
    n = param.name.lower()
    icon = _icon(param)
    if "verdichter" in n or "pumpe" in n or "_mkp" in n or n.endswith("mkp"):
        return BinarySensorDeviceClass.RUNNING, icon
    if "ausgang" in n:
        return BinarySensorDeviceClass.POWER, icon
    return None, icon


def classify_select(param: ParameterDefinition) -> str:
    """Return the ``icon`` for a writable enum (selects have no device_class)."""
    return _icon(param)


def classify_switch(param: ParameterDefinition) -> str:
    """Return the ``icon`` for a writable boolean.

    ``SwitchDeviceClass`` only offers switch/outlet (neither apt for these
    heating controls), so switches get an icon only.
    """
    return _icon(param)
