# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
# Derived in part from zivillian/ism7mqtt (GPL-3.0-or-later)
"""Loader for Wolf's real XML format (device.xml + parameter.xml + converter.xml).

Source files come from zivillian/ism7mqtt's Resources/ (GPL-3.0). Wolf's format:

- ``DeviceTemplate(DTID, ParameterReferenceList[PTID, ...])``
- ``NumericParameterDescriptor(PTID, Name, UnitName, Min, Max, Decimals)``
- ``ListParameterDescriptor(PTID, Name, KeyValueList="0;Aus;1;Ein")``
- ``OtherParameterDescriptor`` and ``TextParameterDescriptor`` for less-common types

We map this to our internal Catalog model:

- ``DeviceTemplate`` -> ``DeviceDefinition`` (device_type from Name, dtid from DTID)
- Parameter descriptors -> ``ParameterDefinition`` (property_id = info_number = PTID,
  codec from descriptor type + bounds + unit)
- A ListParameterDescriptor with two options "Ein/Aus" or "Aktiviert/Deaktiviert"
  is treated as a boolean (matches zivillian's ``IsBoolean`` heuristic).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from pathlib import Path

from lxml import etree

from .catalog import Catalog, DeviceDefinition, ParameterDefinition
from .codecs import (
    BoolCodec,
    Codec,
    EnumCodec,
    Int16Codec,
    ScaledCodec,
)
from .converters import ConverterTemplate, load_converter_templates

_LOGGER = logging.getLogger(__name__)


def load_wolf_catalog(wolf_dir: Path, *, curated_dir: Path | None = None) -> Catalog:
    """Load Wolf's real device.xml + parameter.xml from the given directory.

    ``curated_dir`` holds our own ``curated_*.json`` files. It defaults to
    ``wolf_dir.parent`` (the bundled layout); pass it explicitly when the
    XMLs live elsewhere, e.g. in ``<config>/ecotherm_wolf/``.

    Also loads ``converter.xml`` if present and attaches each parameter's
    ConverterTemplate (matched by CTID == PTID) so the coordinator can decode
    multi-telegram values. Parameters whose CTID has no supported converter
    are dropped at load time (logged at INFO once).
    """
    parameter_xml = wolf_dir / "parameter.xml"
    device_xml = wolf_dir / "device.xml"
    converter_xml = wolf_dir / "converter.xml"
    if not parameter_xml.exists() or not device_xml.exists():
        raise FileNotFoundError(f"Missing wolf XMLs in {wolf_dir}")

    converters: dict[int, ConverterTemplate] = {}
    if converter_xml.exists():
        converters = load_converter_templates(converter_xml)

    parameters_by_ptid = _load_parameters(parameter_xml, converters)
    devices = _load_devices(device_xml, parameters_by_ptid)
    # The curated "standard" preset lives next to the wolf/ XML dir, in
    # ``data/curated_standard.json``. Missing / broken file -> empty mapping,
    # the ``standard`` preset then falls back to the <App> heuristic.
    if curated_dir is None:
        curated_dir = wolf_dir.parent
    curated_standard = _load_curated_standard(curated_dir / "curated_standard.json")
    # ``data/curated_bus_addresses.json`` maps DTID -> default write bus
    # address for modules whose write address differs from their read
    # address. Missing / broken file -> empty mapping (write == read for all).
    curated_write_addresses = _load_curated_write_addresses(
        curated_dir / "curated_bus_addresses.json",
    )
    _LOGGER.info(
        "Loaded %d Wolf device templates and %d parameters from %s "
        "(%d curated standard presets, %d curated write addresses)",
        len(devices), len(parameters_by_ptid), wolf_dir,
        len(curated_standard), len(curated_write_addresses),
    )
    return Catalog(
        devices=devices,
        curated_standard=curated_standard,
        curated_write_addresses=curated_write_addresses,
    )


def _load_curated_write_addresses(json_path: Path) -> dict[int, str]:
    """Load ``curated_bus_addresses.json`` -> ``{DTID: write_address}``.

    The file maps Wolf DeviceTemplate IDs to the bus address a module
    listens on for write commands -- needed because some Wolf modules
    (BM-2, CHA) accept writes on a different address than the one they
    report reads on, and the SystemConfigResp only carries the read
    address. Robust against every failure mode -- a missing file,
    malformed JSON, or an unexpected shape all return ``{}`` (logged at
    WARNING / DEBUG) so the config flow simply defaults write == read.
    """
    if not json_path.exists():
        _LOGGER.debug(
            "Curated write-address file %s not found -- write address "
            "defaults to the read address for every module", json_path,
        )
        return {}
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _LOGGER.warning("Cannot read curated write addresses %s: %s", json_path, e)
        return {}

    if not isinstance(raw, dict):
        _LOGGER.warning(
            "Curated write addresses %s: top level is not an object", json_path,
        )
        return {}
    offsets = raw.get("write_offsets")
    if not isinstance(offsets, dict):
        _LOGGER.warning(
            "Curated write addresses %s: missing or invalid 'write_offsets' "
            "object", json_path,
        )
        return {}

    out: dict[int, str] = {}
    for dtid_key, write_addr in offsets.items():
        try:
            dtid = int(dtid_key)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Curated write addresses %s: skipping non-integer DTID key %r",
                json_path, dtid_key,
            )
            continue
        if not isinstance(write_addr, str) or not write_addr.strip():
            _LOGGER.warning(
                "Curated write addresses %s: DTID %d value is not a non-empty "
                "string -- skipping", json_path, dtid,
            )
            continue
        out[dtid] = write_addr.strip()
    return out


def _load_curated_standard(json_path: Path) -> dict[int, set[int]]:
    """Load ``curated_standard.json`` -> ``{DTID: {PTID, ...}}``.

    The file maps Wolf DeviceTemplate IDs to a hand-picked PTID selection
    (the author's ism7mqtt parameter.json). Robust against every failure
    mode -- a missing file, malformed JSON, or an unexpected shape all
    return ``{}`` (logged at WARNING) so the ``standard`` preset gracefully
    falls back to Wolf's ``<App>`` heuristic.
    """
    if not json_path.exists():
        _LOGGER.warning(
            "Curated standard preset file %s not found -- 'standard' preset "
            "falls back to the <App> heuristic", json_path,
        )
        return {}
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _LOGGER.warning("Cannot read curated standard preset %s: %s", json_path, e)
        return {}

    if not isinstance(raw, dict):
        _LOGGER.warning(
            "Curated standard preset %s: top level is not an object", json_path,
        )
        return {}
    presets = raw.get("presets")
    if not isinstance(presets, dict):
        _LOGGER.warning(
            "Curated standard preset %s: missing or invalid 'presets' object",
            json_path,
        )
        return {}

    out: dict[int, set[int]] = {}
    for dtid_key, ptids in presets.items():
        try:
            dtid = int(dtid_key)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Curated standard preset %s: skipping non-integer DTID key %r",
                json_path, dtid_key,
            )
            continue
        if not isinstance(ptids, list):
            _LOGGER.warning(
                "Curated standard preset %s: DTID %d value is not a list "
                "-- skipping", json_path, dtid,
            )
            continue
        ptid_set: set[int] = set()
        for ptid in ptids:
            try:
                ptid_set.add(int(ptid))
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Curated standard preset %s: DTID %d has non-integer "
                    "PTID %r -- skipping that entry", json_path, dtid, ptid,
                )
        if ptid_set:
            out[dtid] = ptid_set
    return out


def _load_parameters(
    xml_path: Path,
    converters: dict[int, ConverterTemplate],
) -> dict[int, ParameterDefinition]:
    """Parse parameter.xml -> dict mapping PTID -> ParameterDefinition.

    Only parameters whose PTID has a supported converter (CTID == PTID) are
    kept. Parameters without a converter or with an unsupported converter
    type are silently skipped -- the coordinator can't read their values
    from the wire.
    """
    tree = etree.parse(str(xml_path))
    root = tree.getroot()
    out: dict[int, ParameterDefinition] = {}
    skipped_no_converter = 0

    parameter_list = root.find("ParameterList")
    if parameter_list is None:
        _LOGGER.warning("parameter.xml has no <ParameterList>")
        return out

    seen_names: set[str] = set()
    for elem in parameter_list:
        ptid_attr = elem.get("PTID")
        if ptid_attr is None:
            continue
        try:
            ptid = int(ptid_attr)
        except ValueError:
            continue

        name_text = (elem.findtext("Name") or "").strip()
        if not name_text:
            continue

        snake_name = _to_snake(name_text) or f"ptid_{ptid}"
        # Keep the entity_id base unique by appending the PTID when collisions
        # would otherwise occur (Wolf re-uses the same Name across DTIDs --
        # e.g. PTID 10000 and 20000 are both "Tagtemperatur").
        if snake_name in seen_names:
            snake_name = f"{snake_name}_{ptid}"
        seen_names.add(snake_name)

        codec = _build_codec_from_descriptor(elem)
        if codec is None:
            continue

        # Wolf's ``<App>true</App>`` marks parameters shown to end-users in
        # SmartSet / the Wolf app -- a curated subset (1176 of 5682 in the
        # bundled catalog). The element is either ``true`` or absent.
        app_flag = (elem.findtext("App") or "").strip().lower() == "true"

        # Look up the converter (CTID == PTID for the standard catalog). If
        # we have a converter dict at all (real Wolf catalog), drop the
        # parameter when no converter is found -- the coordinator wouldn't
        # know which info_numbers to read. When the converter dict is empty
        # (legacy / synthetic catalogs without converter.xml) we keep the
        # parameter and rely on info_numbers defaulting to [property_id].
        converter = converters.get(ptid)
        info_numbers: list[int] = []
        if converters:
            if converter is None:
                skipped_no_converter += 1
                continue
            info_numbers = list(converter.info_numbers)
            if not info_numbers:
                # NullConverter: 0 InfoReads, no live value. Skip -- the
                # parameter is display-only.
                skipped_no_converter += 1
                continue

        out[ptid] = ParameterDefinition(
            property_id=ptid,
            name=snake_name,
            display_name=name_text,
            codec=codec,
            preset=None,
            info_numbers=info_numbers,
            converter=converter,
            app=app_flag,
        )
    if skipped_no_converter:
        _LOGGER.info(
            "Skipped %d parameters without supported converters",
            skipped_no_converter,
        )
    return out


def _build_codec_from_descriptor(elem: etree._Element) -> Codec | None:
    """Build a codec based on the ParameterDescriptor's tag + attributes."""
    tag = etree.QName(elem.tag).localname
    unit = (elem.findtext("UnitName") or "").strip() or None
    readonly_text = (elem.findtext("ReadOnlyConditionId") or "").strip()
    # Wolf encodes writability as a string: "False" => writable, anything
    # else (e.g. "True", "C_3019") => read-only. Matches zivillian's
    # ``IsWritable => ReadOnlyConditionId == "False"``.
    writable = readonly_text == "False"

    if tag == "NumericParameterDescriptor":
        min_text = elem.findtext("MinValueCondition")
        max_text = elem.findtext("MaxValueCondition")
        decimals_text = elem.findtext("Decimals")
        try:
            decimals = int(decimals_text) if decimals_text else 0
        except (ValueError, TypeError):
            decimals = 0

        # Read-only numeric fields often have no min/max in the XML. Without
        # bounds, the codec is decode-only -- which matches the writability
        # property anyway.
        min_v = _maybe_float(min_text)
        max_v = _maybe_float(max_text)

        # ScaledCodec validates encode() against ``base.min_value`` /
        # ``base.max_value`` *in display units* (see ScaledCodec docstring), so
        # we keep min/max in display units even when wrapping.
        bounds_set = writable and min_v is not None and max_v is not None

        if decimals > 0:
            scale = float(10 ** decimals)
            if bounds_set:
                base = Int16Codec(
                    min_value=int(round(min_v)),  # type: ignore[arg-type]
                    max_value=int(round(max_v)),  # type: ignore[arg-type]
                    unit=unit,
                )
            else:
                base = Int16Codec(unit=unit)
            return ScaledCodec(base=base, scale=scale)

        # Integer (decimals == 0).
        if bounds_set:
            return Int16Codec(
                min_value=int(min_v),  # type: ignore[arg-type]
                max_value=int(max_v),  # type: ignore[arg-type]
                unit=unit,
            )
        return Int16Codec(unit=unit)

    if tag == "ListParameterDescriptor":
        members = _parse_key_value_list(elem.findtext("KeyValueList") or "")
        if not members:
            return None
        # zivillian's heuristic: a 2-member list with Ein/Aus or
        # Aktiviert/Deaktiviert is really a boolean.
        if _is_boolean_list(members):
            return BoolCodec(writable=writable)
        return EnumCodec(members=members, writable=writable)

    # OtherParameterDescriptor / TextParameterDescriptor and the few unknown
    # variants: skip with a debug log. T22+ may add more.
    _LOGGER.debug("Unsupported parameter descriptor type: %s", tag)
    return None


def _maybe_float(text: str | None) -> float | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except (ValueError, TypeError):
        return None


def _parse_key_value_list(s: str) -> dict[int, str]:
    """Parse Wolf's KeyValueList ``"0;Aus;1;Ein"`` into ``{0: "Aus", 1: "Ein"}``."""
    parts = s.split(";")
    out: dict[int, str] = {}
    i = 0
    while i + 1 < len(parts):
        key, name = parts[i].strip(), parts[i + 1].strip()
        try:
            out[int(key)] = name
        except ValueError:
            pass
        i += 2
    return out


def _is_boolean_list(members: dict[int, str]) -> bool:
    if len(members) != 2:
        return False
    values = set(members.values())
    return values == {"Ein", "Aus"} or values == {"Aktiviert", "Deaktiviert"}


def _dedup_key(param: ParameterDefinition) -> tuple[str, str, tuple[int, ...]]:
    """Identity of the *physical bus value* a parameter reads.

    Wolf's parameter.xml lists the same logical value many times -- one row
    per ``RemoveConditionId`` variant (see C_2167 etc.), all reading the same
    telegram. ``RemoveConditionId`` definitions live in Wolf's proprietary
    LuCon library and aren't in the bundled XMLs, so we can't resolve which
    variant SmartSet would show. Instead we collapse rows that read the
    *same* value: same ``display_name`` (Wolf's untouched ``<Name>``), same
    converter class, same TelegramNr set. Rows with a different TelegramNr
    (e.g. Programmwahl 360051@10100 vs 360135@9026) are genuinely distinct
    and survive.
    """
    converter_type = type(param.converter).__name__ if param.converter else ""
    return (param.display_name, converter_type, tuple(param.info_numbers))


def _dedup_device_parameters(
    device_type: str,
    ordered: list[ParameterDefinition],
) -> dict[int, ParameterDefinition]:
    """Drop redundant catalog rows within one device, keeping the first.

    Two passes:

    1. **Dedup** -- rows sharing a ``_dedup_key`` read the identical bus
       value; only the first (in catalog order) is kept.
    2. **Disambiguate** -- after dedup, two surviving rows may still share a
       ``display_name`` (different TelegramNr). HA needs distinct entity
       names, so the 2nd, 3rd, ... get a ``" (2)"`` / ``" (3)"`` suffix on
       ``display_name`` only. The technical ``name`` is left untouched.
       Suffixed rows are *copies* -- ``ParameterDefinition`` objects are
       shared across devices via ``parameters_by_ptid``, so we must not
       mutate the shared instance.
    """
    seen_keys: set[tuple[str, str, tuple[int, ...]]] = set()
    kept: list[ParameterDefinition] = []
    dropped = 0
    for param in ordered:
        key = _dedup_key(param)
        if key in seen_keys:
            dropped += 1
            continue
        seen_keys.add(key)
        kept.append(param)

    if dropped:
        _LOGGER.debug(
            "Deduplicated %d redundant parameters for device %s",
            dropped, device_type,
        )

    name_counts: dict[str, int] = {}
    out: dict[int, ParameterDefinition] = {}
    for param in kept:
        seen_before = name_counts.get(param.display_name, 0)
        name_counts[param.display_name] = seen_before + 1
        if seen_before:
            param = replace(
                param,
                display_name=f"{param.display_name} ({seen_before + 1})",
            )
        out[param.property_id] = param
    return out


def _load_devices(
    xml_path: Path,
    parameters_by_ptid: dict[int, ParameterDefinition],
) -> dict[str, DeviceDefinition]:
    """Parse device.xml -> dict mapping device_type (string key) -> DeviceDefinition."""
    tree = etree.parse(str(xml_path))
    root = tree.getroot()
    out: dict[str, DeviceDefinition] = {}

    for tmpl in root.iter("DeviceTemplate"):
        dtid_attr = tmpl.get("DTID")
        name_attr = tmpl.get("Name")
        if dtid_attr is None or name_attr is None:
            continue
        try:
            dtid = int(dtid_attr)
        except ValueError:
            continue

        # Use the Name as device_type key, with DTID disambiguation when
        # several device templates share a name (Wolf does this for variants).
        device_type = name_attr.strip() or f"DTID_{dtid}"
        if device_type in out:
            device_type = f"{device_type}_{dtid}"

        ordered: list[ParameterDefinition] = []
        ref_list = tmpl.find("ParameterReferenceList")
        if ref_list is None:
            continue
        for ref in ref_list.iter("ParameterReference"):
            ptid_attr = ref.get("PTID")
            if ptid_attr is None:
                continue
            try:
                ptid = int(ptid_attr)
            except ValueError:
                continue
            param = parameters_by_ptid.get(ptid)
            if param is None:
                continue
            ordered.append(param)

        params = _dedup_device_parameters(device_type, ordered)
        if not params:
            # Devices with no resolvable parameters are useless to us.
            continue

        wrs_text = (tmpl.findtext("WRSDeviceIds") or "").strip()
        sw_text = (tmpl.findtext("SoftwareNumber") or "").strip()
        out[device_type] = DeviceDefinition(
            device_type=device_type,
            display_name=name_attr,
            parameters=params,
            dtid=dtid,
            wrs_device_id=wrs_text,
            software_number=sw_text,
        )
    return out


_TRANSLIT = str.maketrans({
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "Ä": "ae",
    "Ö": "oe",
    "Ü": "ue",
    "ß": "ss",
})

_NON_WS_DASH = re.compile(r"[\s\-/]+")
_NON_ALNUM_UNDERSCORE = re.compile(r"[^a-z0-9_]")


def _to_snake(name: str) -> str:
    """Convert a display name to snake_case (rough; Wolf's names have spaces/Umlauts)."""
    s = name.translate(_TRANSLIT).lower()
    s = _NON_WS_DASH.sub("_", s)
    s = _NON_ALNUM_UNDERSCORE.sub("", s)
    return s.strip("_")
