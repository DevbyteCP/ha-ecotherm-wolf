# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
# Derived in part from zivillian/ism7mqtt (GPL-3.0-or-later)
"""In-memory catalog of ISM7 devices and parameters."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .converters import ConverterTemplate

_LOGGER = logging.getLogger(__name__)


class UnknownDeviceError(KeyError):
    pass


class UnknownParameterError(KeyError):
    pass


@dataclass(slots=True)
class ParameterDefinition:
    property_id: int
    name: str  # snake_case identifier, stable
    display_name: str
    codec: Any  # Codec protocol
    preset: str | None = None  # "minimal", "standard", "extended", or None
    # Wire-level identifier(s) sent on the bus. For most parameters this is a
    # single value (== ``property_id``). Multi-telegram parameters (32-bit
    # counters, mixer state) need 2+ TelegramNrs read together. Default empty
    # list is resolved in ``__post_init__`` to ``[property_id]`` for back-compat.
    info_numbers: list[int] = field(default_factory=list)
    # ConverterTemplate for the parameter (CTID == PTID in the standard Wolf
    # catalog). ``None`` for synthetic / non-Wolf test fixtures -- the
    # coordinator falls back to the ``codec`` for those.
    converter: ConverterTemplate | None = None
    # Wolf's ``<App>true</App>`` flag from parameter.xml -- marks the curated
    # subset Wolf shows end-users in SmartSet/the app. ``False`` when the
    # element is absent. Drives the "standard" / "minimal" presets.
    app: bool = False

    def __post_init__(self) -> None:
        if not self.info_numbers:
            self.info_numbers = [self.property_id]

    @property
    def info_number(self) -> int:
        """Back-compat scalar accessor.

        Returns the first info_number (for parameters bundled into one
        telegram) or 0 if nothing's configured (defensive). New code should
        iterate ``info_numbers`` instead.
        """
        return self.info_numbers[0] if self.info_numbers else 0


@dataclass(slots=True)
class DeviceDefinition:
    device_type: str
    display_name: str
    parameters: dict[int, ParameterDefinition] = field(default_factory=dict)
    # DeviceTemplate ID from Wolf's device.xml. 0 = synthetic / non-Wolf
    # device (e.g. a test fixture).
    dtid: int = 0
    # WRSDeviceIds (hex string like ``"0x29"``) from device.xml. This is the
    # byte the ISM7 reports as ``did`` for each discovered bus device. Empty
    # for synthetic catalogs that don't carry the field.
    wrs_device_id: str = ""
    # SoftwareNumber from device.xml (hex string ``"0xF001"``). Used to
    # disambiguate when several templates share a wrs_device_id.
    software_number: str = ""

    def _standard_params(
        self, curated_ptids: set[int] | None
    ) -> list[ParameterDefinition]:
        """Resolve the ``standard`` preset set for this device.

        When ``curated_ptids`` is provided (the device's DTID is in
        ``Catalog.curated_standard``), ``standard`` is exactly the author's
        hand-picked selection -- intersected with the parameters actually
        present in the (deduplicated) catalog. PTIDs from the curated list
        that no longer exist (dropped by dedup, or never had a converter)
        are skipped with a debug log instead of crashing.

        When ``curated_ptids`` is ``None`` (DTID not curated -- e.g. a Wolf
        gas boiler the author doesn't own), fall back to Wolf's
        ``<App>true</App>`` heuristic so ``standard`` stays defined for all
        device templates.
        """
        if curated_ptids is None:
            return [p for p in self.parameters.values() if p.app]
        result: list[ParameterDefinition] = []
        for ptid in curated_ptids:
            param = self.parameters.get(ptid)
            if param is None:
                _LOGGER.debug(
                    "Curated 'standard' PTID %d not in catalog for device "
                    "%s -- skipping (dropped by dedup or no converter)",
                    ptid, self.device_type,
                )
                continue
            result.append(param)
        return result

    def parameters_for_preset(
        self, preset: str, curated_ptids: set[int] | None = None
    ) -> list[ParameterDefinition]:
        """Resolve a config-flow preset to a concrete parameter list.

        Returns a deterministic, ``property_id``-sorted list. The four
        non-manual presets form a nesting chain
        ``minimal ⊆ standard ⊆ extended ⊆ all``:

        - ``all``      -- every parameter of the device.
        - ``extended`` -- ``standard`` plus every ``<App>true</App>``
          parameter plus every writable parameter (numbers / selects /
          switches). Guaranteed superset of ``standard``.
        - ``standard`` -- the author's curated selection for the device's
          DTID (passed in as ``curated_ptids``); when the DTID isn't
          curated, Wolf's ``<App>true</App>`` flag is used instead.
        - ``minimal``  -- the read-only display sensors within ``standard``
          (temperatures, status, counters -- no settings).

        ``curated_ptids`` is the set of PTIDs from
        ``Catalog.curated_standard`` for this device's DTID, or ``None`` when
        the DTID isn't curated. ``Catalog.parameters_for_preset`` wires this
        up automatically; callers holding a bare ``DeviceDefinition`` may
        pass it explicitly.

        An unknown preset still yields ``[]`` (legacy ``preset`` attribute
        fallback for hand-written override XMLs that set it explicitly).
        """
        params = self.parameters.values()
        if preset == "all":
            result = list(params)
        elif preset == "standard":
            result = self._standard_params(curated_ptids)
        elif preset == "extended":
            standard_ids = {
                p.property_id for p in self._standard_params(curated_ptids)
            }
            result = [
                p for p in params
                if p.property_id in standard_ids or p.app or p.codec.writable
            ]
        elif preset == "minimal":
            result = [
                p for p in self._standard_params(curated_ptids)
                if p.codec.ha_platform == "sensor" and not p.codec.writable
            ]
        else:
            # Override-XML / synthetic catalogs may still tag parameters with
            # an explicit ``preset`` string; honour that as a fallback.
            result = [p for p in params if p.preset == preset]
        return sorted(result, key=lambda p: p.property_id)


@dataclass(slots=True)
class CatalogDiff:
    added: list[tuple[str, int, str]]  # (device_type, property_id, name)
    removed: list[tuple[str, int, str]]
    changed: list[tuple[str, int, str]]  # codec or display_name changed


@dataclass(slots=True)
class Catalog:
    devices: dict[str, DeviceDefinition]
    # Curated "standard" preset, keyed by DeviceTemplate ID -> set of PTIDs.
    # Loaded from ``data/curated_standard.json`` (the author's hand-picked
    # selection). Empty dict when the file is missing / unreadable -- the
    # ``standard`` preset then falls back to Wolf's ``<App>`` heuristic for
    # every device. A DTID absent from this mapping always uses the fallback.
    curated_standard: dict[int, set[int]] = field(default_factory=dict)
    # Default write-bus-address per DeviceTemplate ID. Loaded from
    # ``data/curated_bus_addresses.json``. Wolf modules can listen for write
    # commands on a different bus address than the one they report reads on
    # (BM-2 / CHA do); the real write address lives in Wolf's proprietary
    # LuCon library and is NOT carried by the SystemConfigResp. This mapping
    # lets the config flow pre-fill a sane write address for known DTIDs.
    # A DTID absent from this mapping means "write address == read address".
    # Empty dict when the file is missing / unreadable.
    curated_write_addresses: dict[int, str] = field(default_factory=dict)

    def get_device(self, device_type: str) -> DeviceDefinition:
        try:
            return self.devices[device_type]
        except KeyError as e:
            raise UnknownDeviceError(device_type) from e

    def get_parameter(self, device_type: str, property_id: int) -> ParameterDefinition:
        dev = self.get_device(device_type)
        try:
            return dev.parameters[property_id]
        except KeyError as e:
            raise UnknownParameterError(f"{device_type}/0x{property_id:04X}") from e

    def parameters_for_preset(
        self, device_type: str, preset: str
    ) -> list[ParameterDefinition]:
        dev = self.get_device(device_type)
        curated_ptids = self.curated_standard.get(dev.dtid)
        return dev.parameters_for_preset(preset, curated_ptids)

    def diff(self, other: Catalog) -> CatalogDiff:
        added: list[tuple[str, int, str]] = []
        removed: list[tuple[str, int, str]] = []
        changed: list[tuple[str, int, str]] = []

        old_keys = {(d, p) for d, dev in self.devices.items() for p in dev.parameters}
        new_keys = {(d, p) for d, dev in other.devices.items() for p in dev.parameters}

        for d, p in new_keys - old_keys:
            added.append((d, p, other.devices[d].parameters[p].name))
        for d, p in old_keys - new_keys:
            removed.append((d, p, self.devices[d].parameters[p].name))
        for d, p in old_keys & new_keys:
            old_param = self.devices[d].parameters[p]
            new_param = other.devices[d].parameters[p]
            if (
                old_param.display_name != new_param.display_name
                or type(old_param.codec) is not type(new_param.codec)
            ):
                changed.append((d, p, new_param.name))

        return CatalogDiff(added=added, removed=removed, changed=changed)
