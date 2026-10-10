# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
# Derived in part from zivillian/ism7mqtt (GPL-3.0-or-later)
"""High-level operations on top of the protocol layer.

.. warning:: LEGACY -- not used by the live integration anymore.
   The coordinator owns its own decode pipeline
   (``_process_telegrams`` / ``_decode_param``), which routes telegrams by
   ``(info_number, service_number)`` and delegates to the parameter's
   ``ConverterTemplate``. This module's ``decode_value_update`` predates
   that: it keys the catalog lookup on ``info_number == property_id`` and
   ignores eBus service numbers entirely, which is only correct for the
   synthetic test catalogs. Kept because the protocol-level test suite
   exercises it; do NOT wire it into production paths.

Decoding a ``TelegramBundleResp`` into our internal (device_id, property_id,
value) tuples requires:
- Mapping the ``bus_address`` field back to the ``device_id`` we know about
- Looking up the parameter in the catalog by ``info_number``
- Reassembling the ``dl/dh`` hex pair into the codec-appropriate raw bytes
- Running the codec's ``decode``
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from custom_components.ecotherm_wolf.definitions.catalog import (
    Catalog,
    UnknownDeviceError,
    UnknownParameterError,
)
from custom_components.ecotherm_wolf.definitions.codecs import (
    Int16Codec,
    ScaledCodec,
    TimeOfDayCodec,
    Uint16Codec,
)

from .protocol import TelegramBundleResp

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class DecodedValue:
    device_id: int
    device_type: str
    property_id: int
    name: str
    value: Any


def _bytes_for_codec(dl: str, dh: str, codec: Any) -> bytes:
    """Reassemble dl/dh into the byte form the codec expects.

    16-bit codecs (Int16, Uint16, TimeOfDay) read big-endian 2-byte; 8-bit
    codecs (Uint8, Enum, Bool) read just the low byte. ScaledCodec wraps a
    base codec, so we recurse through that.
    """
    if isinstance(codec, ScaledCodec):
        return _bytes_for_codec(dl, dh, codec.base)
    if isinstance(codec, Int16Codec | Uint16Codec | TimeOfDayCodec):
        return bytes.fromhex(dh) + bytes.fromhex(dl)
    return bytes.fromhex(dl)


def decode_value_update(
    update: TelegramBundleResp,
    *,
    device_map: dict[int, tuple[str, ...]],
    catalog: Catalog,
) -> list[DecodedValue]:
    """Decode a TelegramBundleResp's read_telegrams into DecodedValues.

    Incoming telegrams are read responses, so they are matched back to a
    device by its *read* bus address. ``device_map`` values may be the
    current 3-tuple ``(device_type, read_address, write_address)``, a legacy
    2-tuple ``(device_type, bus_address)`` or a legacy plain string -- all
    three are accepted; only the read address is consulted here.
    """
    out: list[DecodedValue] = []
    for record in update.read_telegrams:
        if record.state != "ok":
            continue
        # Find device_id by matching the READ bus_address.
        device_id = None
        device_type = None
        for d_id, value in device_map.items():
            if isinstance(value, tuple | list) and len(value) >= 2:
                # 3-tuple (type, read, write) or legacy 2-tuple (type, read).
                d_type, read_bus = str(value[0]), str(value[1])
            else:
                # Legacy plain-string entry: device_type only, no address.
                d_type, read_bus = str(value), ""
            if read_bus and read_bus == record.bus_address:
                device_id = d_id
                device_type = d_type
                break
        if device_id is None or device_type is None:
            _LOGGER.warning(
                "Unknown bus_address %r in telegram", record.bus_address,
            )
            continue
        # Find parameter by info_number (== property_id for standard Wolf).
        try:
            param = catalog.get_parameter(device_type, record.info_number)
        except (UnknownDeviceError, UnknownParameterError):
            _LOGGER.warning(
                "Unknown info_number %s for device %s",
                record.info_number, device_type,
            )
            continue
        try:
            raw = _bytes_for_codec(record.db_low, record.db_high, param.codec)
            value = param.codec.decode(raw)
        except Exception:
            _LOGGER.exception(
                "Codec failed for %s/info_number=%s (dl=%s dh=%s)",
                device_type, record.info_number,
                record.db_low, record.db_high,
            )
            continue
        out.append(DecodedValue(
            device_id=device_id,
            device_type=device_type,
            property_id=record.info_number,
            name=param.name,
            value=value,
        ))
    return out
