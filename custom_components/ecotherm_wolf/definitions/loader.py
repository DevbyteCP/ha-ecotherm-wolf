# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
# Derived in part from zivillian/ism7mqtt (GPL-3.0-or-later)
"""Load device definitions from XML files."""
from __future__ import annotations

import logging
from pathlib import Path

from lxml import etree

from .catalog import Catalog, DeviceDefinition, ParameterDefinition
from .codecs import (
    BoolCodec,
    Codec,
    EnumCodec,
    Int16Codec,
    ScaledCodec,
    TimeOfDayCodec,
    Uint8Codec,
    Uint16Codec,
)

_LOGGER = logging.getLogger(__name__)


class DefinitionLoadError(Exception):
    pass


def load_catalog_from_dir(directory: Path) -> Catalog:
    if not directory.exists() or not directory.is_dir():
        raise DefinitionLoadError(f"Definition directory not found: {directory}")

    devices: dict[str, DeviceDefinition] = {}
    errors: list[str] = []
    for xml_path in sorted(directory.glob("*.xml")):
        try:
            dev = _load_device(xml_path)
        except DefinitionLoadError as e:
            # Structural problem (duplicate PTID, etc.) -- collect and re-raise
            # at the end so the caller can surface a Repair Issue with all
            # broken files listed.
            errors.append(str(e))
            continue
        except (etree.XMLSyntaxError, ValueError, KeyError) as e:
            _LOGGER.warning("Skipping invalid definition %s: %s", xml_path.name, e)
            continue
        devices[dev.device_type] = dev

    if errors:
        raise DefinitionLoadError("; ".join(errors))

    return Catalog(devices=devices)


def _load_device(xml_path: Path) -> DeviceDefinition:
    tree = etree.parse(str(xml_path))
    root = tree.getroot()
    device_type = root.attrib["type"]
    display_name = root.attrib.get("displayName", device_type)

    params: dict[int, ParameterDefinition] = {}
    for p in root.findall("parameter"):
        id_str = p.attrib["id"]
        property_id = int(id_str, 16) if id_str.startswith("0x") else int(id_str)
        if property_id in params:
            raise DefinitionLoadError(
                f"{xml_path.name}: duplicate property_id 0x{property_id:04X} "
                f"for device '{device_type}'",
            )
        name = p.attrib["name"]
        param_display = p.attrib.get("displayName", name)
        preset = p.attrib.get("preset")
        codec = _build_codec(p.find("codec"))
        params[property_id] = ParameterDefinition(
            property_id=property_id,
            name=name,
            display_name=param_display,
            codec=codec,
            preset=preset,
        )

    return DeviceDefinition(
        device_type=device_type,
        display_name=display_name,
        parameters=params,
    )


def _build_codec(node: etree._Element | None) -> Codec:
    if node is None:
        raise ValueError("missing <codec> element")
    codec_type = node.attrib["type"]
    writable = node.attrib.get("writable", "false").lower() == "true"

    if codec_type == "int16":
        return _int_codec(Int16Codec, node)
    if codec_type == "uint8":
        return _int_codec(Uint8Codec, node)
    if codec_type == "uint16":
        return _int_codec(Uint16Codec, node)
    if codec_type == "scaled":
        base_node = node.find("base")
        if base_node is None:
            raise ValueError("scaled codec missing <base>")
        base = _build_codec(_wrap_as_codec(base_node))
        scale = float(node.findtext("scale", "1"))
        return ScaledCodec(base=base, scale=scale)  # type: ignore[arg-type]
    if codec_type == "enum":
        members = {
            int(m.attrib["value"]): (m.text or "").strip()
            for m in node.findall("member")
        }
        return EnumCodec(members=members, writable=writable)
    if codec_type == "bool":
        return BoolCodec(writable=writable)
    if codec_type == "time_of_day":
        return TimeOfDayCodec(writable=writable)
    raise ValueError(f"unknown codec type: {codec_type}")


def _int_codec(
    cls: type[Int16Codec] | type[Uint8Codec] | type[Uint16Codec],
    node: etree._Element,
) -> Int16Codec | Uint8Codec | Uint16Codec:
    unit = node.attrib.get("unit")
    min_text = node.findtext("min")
    max_text = node.findtext("max")
    return cls(
        min_value=int(min_text) if min_text else None,
        max_value=int(max_text) if max_text else None,
        unit=unit,
    )


def _wrap_as_codec(base_node: etree._Element) -> etree._Element:
    """`<base type="int16" unit="°C"/>` is structurally a codec node."""
    new = etree.Element("codec", attrib=dict(base_node.attrib))
    for child in base_node:
        new.append(child)
    return new
