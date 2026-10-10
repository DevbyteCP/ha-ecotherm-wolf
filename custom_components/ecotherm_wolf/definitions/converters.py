# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
# Derived in part from zivillian/ism7mqtt (GPL-3.0-or-later)
"""Wolf ConverterTemplate system.

A ConverterTemplate maps PTID -> list of InfoNumbers (TelegramNrs) and
provides decode/encode for the wire bytes. CTID == PTID for the standard
Wolf catalog (we only use that mapping today).

Source semantics (GPL-3.0, zivillian/ism7mqtt):

- A 16-bit telegram value is built as ``(dh << 8) | dl`` -- where ``dl`` is
  the low byte and ``dh`` is the high byte returned in the wire frame.
- A 32-bit value joins two such telegrams: ``(high_word << 16) | low_word``
  where ``low_word`` comes from ``TelegramNrLow`` and ``high_word`` from
  ``TelegramNrHigh``. Each *_word* is itself a 16-bit ``(dh<<8)|dl``.
- The ``Type`` element holds a string code like ``SS10`` (signed short / 10)
  -- see ``_NUMERIC16_TYPE_CODES`` for the full table we support today.
- A ``MixerStateConverterTemplate`` reports an enum value: 0 = neither, 1 =
  closing-active, 2 = opening-active.

Out of scope for T21e (skipped at load time, logged at INFO once):
DateTime, Bit-extraction (Bit0to3 / Bit4to7), SolarStatistic,
Modbus*, Timeprog, Timespan, BM2Date/BM2Time, UnicodeText.

BinaryReadOnlyConverter (added in T21f) is supported.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from lxml import etree

_LOGGER = logging.getLogger(__name__)


# Type code -> (signed flag, scale). Scale 1 means no division.
# Mirrors zivillian/NumericConverter16Template.cs cases that are realistic for
# the bundled Wolf catalog. Unknown codes default to ``US`` (uint, no scale).
_NUMERIC16_TYPE_CODES: dict[str, tuple[bool, float]] = {
    "US": (False, 1.0),
    "SS": (True, 1.0),
    "SS10": (True, 10.0),
    "US10": (False, 10.0),
    "SS100": (True, 100.0),
    "US100": (False, 100.0),
    "SS1000": (True, 1000.0),
    "US1000": (False, 1000.0),
    "US4": (False, 4.0),
    "IntDiv60": (False, 60.0),
    "SSPR": (True, 255.0),  # signed short * (1/255) -- approx
}


def _parse_byte(text: str | None) -> int:
    """Parse a single hex byte from the ``dl`` / ``dh`` wire form.

    Wolf returns these as bare 2-char hex (e.g. ``"F5"``, ``"00"``); some test
    payloads use the C# ``"0x{XX}"`` form. Accept both, return 0 on empty.
    """
    if text is None:
        return 0
    s = text.strip()
    if not s:
        return 0
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    return int(s, 16)


def _maybe_int(text: str | None) -> int | None:
    """Parse an optional integer XML element body.

    Returns ``None`` when the element is absent, blank, or non-numeric --
    used for the optional ``<ServiceReadNumber>`` / ``<ServiceWriteNumber>``
    converter fields.
    """
    if text is None:
        return None
    s = text.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


class ConverterTemplate(ABC):
    """Base class for Wolf's converter templates.

    A converter knows which ``info_numbers`` (= ``TelegramNr`` values from
    converter.xml) make up its parameter, and how to assemble those
    telegrams' ``dl/dh`` bytes back into a typed value (and vice versa for
    writes). The catalog stores one converter per parameter.
    """

    def __init__(self, ctid: int) -> None:
        self.ctid = ctid
        # Wolf's <ServiceReadNumber> / <ServiceWriteNumber> from converter.xml.
        # Several converters reuse the same TelegramNr under *different* eBus
        # "services" -- the CWL, for instance, exposes telegram 1 under both
        # service 4022 and 4050. The ISM7 needs the ``snr`` attribute on the
        # read request to pick the right service; omitting it makes the
        # gateway answer 0xFFFF for every non-default-service telegram and
        # poisons its poll cache (so even the Wolf app then reads -1).
        # zivillian carries this on every InfoRead its converters build
        # (Single/MultiTelegramConverterTemplateBase.InfoReads); we mirror it.
        # ``load_converter_templates`` fills these in from the XML; ``None``
        # means "no service gate -- omit ``snr``".
        self.service_read_number: int | None = None
        self.service_write_number: int | None = None

    @property
    @abstractmethod
    def info_numbers(self) -> list[int]:
        """List of InfoNumbers (TelegramNrs) needed to read this value."""

    @abstractmethod
    def decode(self, telegrams: dict[int, tuple[int, int]]) -> Any:
        """Decode ``{info_number: (dl_byte, dh_byte)}`` to a typed value.

        Returns ``None`` if any required telegram is missing.
        """

    def encode(self, value: Any) -> dict[int, tuple[int, int]]:
        """Encode a typed value to ``{info_number: (dl_byte, dh_byte)}``.

        Default raises -- writable converters override.
        """
        raise NotImplementedError(f"{type(self).__name__} is read-only")

    @property
    def writable(self) -> bool:
        return False


class NumericConverter16(ConverterTemplate):
    """Single 16-bit telegram with optional scaling.

    Decode: ``(dh << 8) | dl`` then sign-extend per type code, then divide
    by the scale.
    """

    __slots__ = ("_telegram_nr", "_type_code", "_signed", "_scale")

    def __init__(self, ctid: int, telegram_nr: int, type_code: str) -> None:
        super().__init__(ctid)
        self._telegram_nr = telegram_nr
        code = (type_code or "US").strip()
        if code not in _NUMERIC16_TYPE_CODES:
            _LOGGER.debug(
                "CTID %s: unknown numeric type code %r, defaulting to US",
                ctid, code,
            )
            code = "US"
        self._type_code = code
        self._signed, self._scale = _NUMERIC16_TYPE_CODES[code]

    @property
    def info_numbers(self) -> list[int]:
        return [self._telegram_nr]

    def decode(self, telegrams: dict[int, tuple[int, int]]) -> Any:
        pair = telegrams.get(self._telegram_nr)
        if pair is None:
            return None
        dl, dh = pair
        raw = ((dh & 0xFF) << 8) | (dl & 0xFF)
        if self._signed and raw & 0x8000:
            raw -= 0x10000
        if self._scale != 1.0:
            return raw / self._scale
        return raw

    def encode(self, value: Any) -> dict[int, tuple[int, int]]:
        if self._scale != 1.0:
            scaled = round(float(value) * self._scale)
        else:
            scaled = int(value)
        # ushort domain on the wire (matches zivillian's `(ushort)` cast).
        word = scaled & 0xFFFF
        dl = word & 0xFF
        dh = (word >> 8) & 0xFF
        return {self._telegram_nr: (dl, dh)}

    @property
    def writable(self) -> bool:
        return True


class NumericConverter32(ConverterTemplate):
    """Two 16-bit telegrams forming a 32-bit value (TelegramNrLow + High).

    Wolf XML uses ``<TelegramNrLow>`` and ``<TelegramNrHigh>``. Decode joins
    both halves: ``(high_word << 16) | low_word``. Today only ``UINT32`` is
    used in the bundled converter.xml -- we treat unknown codes as unsigned.
    """

    __slots__ = ("_low_nr", "_high_nr", "_type_code", "_signed")

    def __init__(
        self,
        ctid: int,
        *,
        telegram_nr_low: int,
        telegram_nr_high: int,
        type_code: str,
    ) -> None:
        super().__init__(ctid)
        self._low_nr = telegram_nr_low
        self._high_nr = telegram_nr_high
        self._type_code = (type_code or "UINT32").strip()
        # The bundled Wolf converter.xml only ever uses UINT32; SINT32 is a
        # defensive fallback if Wolf ever ships one.
        self._signed = self._type_code.startswith("S")

    @property
    def info_numbers(self) -> list[int]:
        return [self._low_nr, self._high_nr]

    def decode(self, telegrams: dict[int, tuple[int, int]]) -> Any:
        low_pair = telegrams.get(self._low_nr)
        high_pair = telegrams.get(self._high_nr)
        if low_pair is None or high_pair is None:
            return None
        # Each *word* is built as (dh<<8)|dl from the wire bytes.
        # The 32-bit value joins the words: (high_word<<16) | low_word, where
        # low_word comes from TelegramNrLow.
        low_word = ((low_pair[1] & 0xFF) << 8) | (low_pair[0] & 0xFF)
        high_word = ((high_pair[1] & 0xFF) << 8) | (high_pair[0] & 0xFF)
        value = ((high_word & 0xFFFF) << 16) | (low_word & 0xFFFF)
        if self._signed and value & 0x80000000:
            value -= 0x100000000
        return value

    def encode(self, value: Any) -> dict[int, tuple[int, int]]:
        v = int(value)
        if self._signed:
            if v < 0:
                v += 0x100000000
        else:
            if v < 0:
                raise ValueError(f"unsigned 32-bit cannot be negative: {value}")
        v &= 0xFFFFFFFF
        low_word = v & 0xFFFF
        high_word = (v >> 16) & 0xFFFF
        return {
            self._low_nr: (low_word & 0xFF, (low_word >> 8) & 0xFF),
            self._high_nr: (high_word & 0xFF, (high_word >> 8) & 0xFF),
        }

    @property
    def writable(self) -> bool:
        return True


class MixerStateConverter(ConverterTemplate):
    """Mixer-position converter (TelegramNrOpen + TelegramNrClose).

    Reports 0 (neither active), 1 (closing-active), 2 (opening-active) --
    matching zivillian's GetValue(). Read-only.
    """

    __slots__ = ("_open_nr", "_close_nr")

    def __init__(
        self,
        ctid: int,
        *,
        telegram_nr_open: int,
        telegram_nr_close: int,
    ) -> None:
        super().__init__(ctid)
        self._open_nr = telegram_nr_open
        self._close_nr = telegram_nr_close

    @property
    def info_numbers(self) -> list[int]:
        return [self._open_nr, self._close_nr]

    def decode(self, telegrams: dict[int, tuple[int, int]]) -> Any:
        open_pair = telegrams.get(self._open_nr)
        close_pair = telegrams.get(self._close_nr)
        if open_pair is None and close_pair is None:
            return None
        # Each pair is a 16-bit; non-zero means "this direction active".
        open_word = (
            ((open_pair[1] & 0xFF) << 8) | (open_pair[0] & 0xFF)
            if open_pair is not None
            else 0
        )
        close_word = (
            ((close_pair[1] & 0xFF) << 8) | (close_pair[0] & 0xFF)
            if close_pair is not None
            else 0
        )
        if open_word != 0:
            return 2
        if close_word != 0:
            return 1
        return 0


class NullConverter(ConverterTemplate):
    """No-op: 0 InfoReads, no live value (display-only / computed parameter)."""

    @property
    def info_numbers(self) -> list[int]:
        return []

    def decode(self, telegrams: dict[int, tuple[int, int]]) -> Any:
        return None


class BinaryReadOnlyConverter(ConverterTemplate):
    """Read-only boolean: extract one bit from a 16-bit telegram, compare to OnValue.

    Mirrors zivillian's ``BinaryReadOnlyConverterTemplate``:

        var value = (ushort) ((high << 8) | low);
        _bit = (value >> Bitnumber) & 0x1;
        // result = _bit == OnValue

    Wolf uses this for bool indicators like "Verdichter on/off" (PTID 270017,
    bit 6 of telegram 12409). Read-only -- write is not implemented in the C#
    reference either.
    """

    __slots__ = ("_telegram_nr", "_bit_number", "_on_value")

    def __init__(
        self,
        ctid: int,
        telegram_nr: int,
        *,
        bit_number: int = 0,
        on_value: int = 1,
    ) -> None:
        super().__init__(ctid)
        self._telegram_nr = telegram_nr
        self._bit_number = bit_number
        self._on_value = on_value

    @property
    def info_numbers(self) -> list[int]:
        return [self._telegram_nr]

    def decode(self, telegrams: dict[int, tuple[int, int]]) -> bool | None:
        pair = telegrams.get(self._telegram_nr)
        if pair is None:
            return None
        dl, dh = pair
        value = ((dh & 0xFF) << 8) | (dl & 0xFF)
        bit = (value >> self._bit_number) & 0x1
        return bit == self._on_value

    @property
    def writable(self) -> bool:
        return False


def load_converter_templates(xml_path: Path) -> dict[int, ConverterTemplate]:
    """Parse ``converter.xml``, return ``CTID -> ConverterTemplate``.

    Unsupported types are silently dropped (the wolf_loader will then skip
    those parameters). A summary of skipped types is logged at INFO once per
    load.
    """
    if not xml_path.exists():
        raise FileNotFoundError(xml_path)
    tree = etree.parse(str(xml_path))
    root = tree.getroot()
    out: dict[int, ConverterTemplate] = {}
    skipped: dict[str, int] = {}

    container = root.find("ConverterTemplates")
    if container is None:
        return out

    for elem in container:
        tag_obj = elem.tag
        if not isinstance(tag_obj, str):
            # Comments and PIs surface as callables in lxml; skip them.
            continue
        tag = etree.QName(tag_obj).localname
        ctid_attr = elem.get("CTID")
        if ctid_attr is None:
            continue
        try:
            ctid = int(ctid_attr)
        except ValueError:
            continue

        try:
            converter = _build_converter(tag, ctid, elem)
        except Exception as e:
            _LOGGER.debug("Skipping CTID %s (%s): %s", ctid, tag, e)
            skipped[tag] = skipped.get(tag, 0) + 1
            continue

        if converter is not None:
            # ServiceReadNumber / ServiceWriteNumber gate which eBus
            # "service" a telegram is read from / written to. Without the
            # read service number on the ``ird`` the ISM7 answers 0xFFFF
            # for non-default-service telegrams (see ConverterTemplate).
            converter.service_read_number = _maybe_int(
                elem.findtext("ServiceReadNumber"),
            )
            converter.service_write_number = _maybe_int(
                elem.findtext("ServiceWriteNumber"),
            )
            out[ctid] = converter
        else:
            skipped[tag] = skipped.get(tag, 0) + 1

    if skipped:
        _LOGGER.info(
            "Loaded %d converter templates; skipped types: %s",
            len(out), skipped,
        )
    else:
        _LOGGER.debug("Loaded %d converter templates", len(out))
    return out


def _build_converter(
    tag: str, ctid: int, elem: etree._Element,
) -> ConverterTemplate | None:
    """Dispatch on tag name. Returns None for unsupported types."""
    if tag == "NumericConverter16Template":
        tnr = elem.findtext("TelegramNr")
        type_code = elem.findtext("Type") or "US"
        if tnr is None or not tnr.strip().isdigit():
            return None
        return NumericConverter16(ctid, int(tnr), type_code)

    if tag == "NumericConverter32Template":
        low_text = elem.findtext("TelegramNrLow")
        high_text = elem.findtext("TelegramNrHigh")
        if (
            low_text is None or high_text is None
            or not low_text.strip().isdigit()
            or not high_text.strip().isdigit()
        ):
            return None
        type_code = elem.findtext("Type") or "UINT32"
        return NumericConverter32(
            ctid,
            telegram_nr_low=int(low_text),
            telegram_nr_high=int(high_text),
            type_code=type_code,
        )

    if tag == "MixerStateConverterTemplate":
        open_text = elem.findtext("TelegramNrOpen")
        close_text = elem.findtext("TelegramNrClose")
        if (
            open_text is None or close_text is None
            or not open_text.strip().isdigit()
            or not close_text.strip().isdigit()
        ):
            return None
        return MixerStateConverter(
            ctid,
            telegram_nr_open=int(open_text),
            telegram_nr_close=int(close_text),
        )

    if tag == "NullConverterTemplate":
        return NullConverter(ctid)

    if tag == "BinaryReadOnlyConverterTemplate":
        tnr = elem.findtext("TelegramNr")
        if tnr is None or not tnr.strip().isdigit():
            return None
        bit_text = elem.findtext("Bitnumber") or "0"
        on_text = elem.findtext("OnValue") or "1"
        try:
            bit_number = int(bit_text)
            on_value = int(on_text)
        except ValueError:
            return None
        return BinaryReadOnlyConverter(
            ctid,
            int(tnr),
            bit_number=bit_number,
            on_value=on_value,
        )

    # Unsupported types: DateTime, Bit0to3 / Bit4to7, SolarStatistic, Modbus*,
    # Timeprog, BM2Date, BM2Time, Timespan, UnicodeText.
    return None
