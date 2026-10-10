"""Tests for the Wolf ConverterTemplate system (T21e).

Each converter type maps a CTID to one or more InfoNumbers (TelegramNrs)
and decodes the bundled telegrams into a typed value. We exercise the
common types here:

- ``NumericConverter16Template``: 1 telegram, scaled int16/uint16
- ``NumericConverter32Template``: 2 telegrams (low + high), uint32
- ``MixerStateConverterTemplate``: 2 telegrams (open + close), int 0/1/2
- ``NullConverterTemplate``: 0 telegrams (skipped at load time)
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from custom_components.ecotherm_wolf.definitions.converters import (
    BinaryReadOnlyConverter,
    MixerStateConverter,
    NullConverter,
    NumericConverter16,
    NumericConverter32,
    load_converter_templates,
)


class TestNumericConverter16:
    def test_info_numbers_single(self) -> None:
        c = NumericConverter16(10000, telegram_nr=5, type_code="SS10")
        assert c.info_numbers == [5]

    def test_decode_ss10_positive(self) -> None:
        """SS10: signed short / 10. Wire (dl=0xF5, dh=0x00) -> 245 / 10 = 24.5."""
        c = NumericConverter16(10000, telegram_nr=5, type_code="SS10")
        assert c.decode({5: (0xF5, 0x00)}) == 24.5

    def test_decode_ss10_negative(self) -> None:
        """Two's-complement signed: 0xFF38 = -200, /10 = -20.0."""
        c = NumericConverter16(10000, telegram_nr=5, type_code="SS10")
        # 0xFF38 -> dl=0x38, dh=0xFF
        assert c.decode({5: (0x38, 0xFF)}) == -20.0

    def test_decode_us_unsigned(self) -> None:
        """US: unsigned short, no scaling. 0x8000 = 32768 (would be -ve as SS)."""
        c = NumericConverter16(10000, telegram_nr=5, type_code="US")
        assert c.decode({5: (0x00, 0x80)}) == 32768

    def test_decode_us100(self) -> None:
        """US100: unsigned short / 100. 0x0064 = 100 / 100 = 1.0."""
        c = NumericConverter16(10000, telegram_nr=5, type_code="US100")
        assert c.decode({5: (0x64, 0x00)}) == 1.0

    def test_decode_unknown_telegram_returns_none(self) -> None:
        """When the required telegram isn't in the dict, decode returns None."""
        c = NumericConverter16(10000, telegram_nr=5, type_code="SS10")
        assert c.decode({999: (0xF5, 0x00)}) is None

    def test_unknown_type_falls_back_to_us(self) -> None:
        """Unknown type code logs a debug + falls back to unsigned no-scale."""
        c = NumericConverter16(10000, telegram_nr=5, type_code="MYSTERY")
        # 0x0014 = 20
        assert c.decode({5: (0x14, 0x00)}) == 20

    def test_encode_ss10_round_trip(self) -> None:
        c = NumericConverter16(10000, telegram_nr=5, type_code="SS10")
        encoded = c.encode(24.5)
        # 24.5 * 10 = 245 = 0x00F5 -> dl=0xF5, dh=0x00
        assert encoded == {5: (0xF5, 0x00)}

    def test_encode_negative_ss10(self) -> None:
        c = NumericConverter16(10000, telegram_nr=5, type_code="SS10")
        encoded = c.encode(-20.0)
        # -200 (signed) -> 0xFF38 -> dl=0x38, dh=0xFF
        assert encoded == {5: (0x38, 0xFF)}

    def test_writable(self) -> None:
        c = NumericConverter16(10000, telegram_nr=5, type_code="SS10")
        assert c.writable is True


class TestNumericConverter32:
    def test_info_numbers_two(self) -> None:
        c = NumericConverter32(
            80021, telegram_nr_low=552, telegram_nr_high=553, type_code="UINT32",
        )
        assert c.info_numbers == [552, 553]

    def test_decode_uint32(self) -> None:
        """0x0001_2345 = 74565. Low word=0x2345, high word=0x0001."""
        c = NumericConverter32(
            80021, telegram_nr_low=552, telegram_nr_high=553, type_code="UINT32",
        )
        # Low word 0x2345: dl=0x45, dh=0x23
        # High word 0x0001: dl=0x01, dh=0x00
        assert c.decode({
            552: (0x45, 0x23),
            553: (0x01, 0x00),
        }) == 0x12345

    def test_decode_returns_none_when_one_half_missing(self) -> None:
        c = NumericConverter32(
            80021, telegram_nr_low=552, telegram_nr_high=553, type_code="UINT32",
        )
        assert c.decode({552: (0x45, 0x23)}) is None

    def test_encode_uint32(self) -> None:
        c = NumericConverter32(
            80021, telegram_nr_low=552, telegram_nr_high=553, type_code="UINT32",
        )
        out = c.encode(0x12345)
        assert out == {
            552: (0x45, 0x23),  # low word 0x2345
            553: (0x01, 0x00),  # high word 0x0001
        }

    def test_encode_rejects_negative_uint(self) -> None:
        c = NumericConverter32(
            80021, telegram_nr_low=552, telegram_nr_high=553, type_code="UINT32",
        )
        with pytest.raises(ValueError):
            c.encode(-1)


class TestMixerStateConverter:
    def test_info_numbers_two(self) -> None:
        c = MixerStateConverter(
            40002, telegram_nr_open=86, telegram_nr_close=87,
        )
        assert c.info_numbers == [86, 87]

    def test_decode_open_active(self) -> None:
        """Non-zero on the Open telegram -> 2 (opening)."""
        c = MixerStateConverter(
            40002, telegram_nr_open=86, telegram_nr_close=87,
        )
        assert c.decode({86: (0x01, 0x00), 87: (0x00, 0x00)}) == 2

    def test_decode_close_active(self) -> None:
        """Non-zero on the Close telegram (Open=0) -> 1 (closing)."""
        c = MixerStateConverter(
            40002, telegram_nr_open=86, telegram_nr_close=87,
        )
        assert c.decode({86: (0x00, 0x00), 87: (0x01, 0x00)}) == 1

    def test_decode_neither(self) -> None:
        """Both zero -> 0 (idle)."""
        c = MixerStateConverter(
            40002, telegram_nr_open=86, telegram_nr_close=87,
        )
        assert c.decode({86: (0x00, 0x00), 87: (0x00, 0x00)}) == 0

    def test_writable_false(self) -> None:
        c = MixerStateConverter(
            40002, telegram_nr_open=86, telegram_nr_close=87,
        )
        assert c.writable is False


class TestNullConverter:
    def test_info_numbers_empty(self) -> None:
        assert NullConverter(99999).info_numbers == []

    def test_decode_returns_none(self) -> None:
        assert NullConverter(99999).decode({}) is None


class TestBinaryReadOnlyConverter:
    """BinaryReadOnly extracts one bit from a 16-bit telegram and compares to OnValue.

    Pattern from zivillian's BinaryReadOnlyConverterTemplate:
        value = (dh << 8) | dl
        bit   = (value >> Bitnumber) & 0x1
        result = bit == OnValue
    """

    def test_info_numbers_single(self) -> None:
        c = BinaryReadOnlyConverter(270017, telegram_nr=12409, bit_number=6)
        assert c.info_numbers == [12409]

    def test_decode_bit_set_returns_true(self) -> None:
        """Wolf PTID 270017 (Verdichter): bit 6 of telegram 12409.

        0x40 = 0b0100_0000 -> bit 6 is set -> True.
        """
        c = BinaryReadOnlyConverter(
            270017, telegram_nr=12409, bit_number=6, on_value=1,
        )
        assert c.decode({12409: (0x40, 0x00)}) is True

    def test_decode_bit_unset_returns_false(self) -> None:
        """0x00 -> bit 6 is 0 -> False (since on_value=1)."""
        c = BinaryReadOnlyConverter(
            270017, telegram_nr=12409, bit_number=6, on_value=1,
        )
        assert c.decode({12409: (0x00, 0x00)}) is False

    def test_decode_high_byte_bit(self) -> None:
        """Bit 11 lives in the high byte: 0x0800 = bit 11 set."""
        c = BinaryReadOnlyConverter(
            500, telegram_nr=370, bit_number=11, on_value=1,
        )
        # 0x0800: dh=0x08, dl=0x00
        assert c.decode({370: (0x00, 0x08)}) is True
        # Other bits set, bit 11 clear:
        assert c.decode({370: (0xFF, 0xF7)}) is False  # 0xF7FF

    def test_decode_missing_telegram_returns_none(self) -> None:
        c = BinaryReadOnlyConverter(270017, telegram_nr=12409, bit_number=6)
        assert c.decode({999: (0x40, 0x00)}) is None

    def test_inverted_on_value(self) -> None:
        """If OnValue=0, bit=0 -> True and bit=1 -> False."""
        c = BinaryReadOnlyConverter(
            999, telegram_nr=1, bit_number=0, on_value=0,
        )
        assert c.decode({1: (0x00, 0x00)}) is True
        assert c.decode({1: (0x01, 0x00)}) is False

    def test_writable_false(self) -> None:
        c = BinaryReadOnlyConverter(270017, telegram_nr=12409, bit_number=6)
        assert c.writable is False

    def test_loaded_from_xml(self, tmp_path: Path) -> None:
        """The XML dispatch in _build_converter wires up BinaryReadOnlyConverterTemplate."""
        xml = tmp_path / "converter.xml"
        xml.write_text(dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <ConverterTemplateConfig>
              <ConverterTemplates>
                <BinaryReadOnlyConverterTemplate CTID="270017">
                  <TelegramNr>12409</TelegramNr>
                  <Type>US</Type>
                  <OnValue>1</OnValue>
                  <Bitnumber>6</Bitnumber>
                </BinaryReadOnlyConverterTemplate>
              </ConverterTemplates>
            </ConverterTemplateConfig>
        """))
        out = load_converter_templates(xml)
        assert 270017 in out
        c = out[270017]
        assert isinstance(c, BinaryReadOnlyConverter)
        assert c.info_numbers == [12409]
        assert c.decode({12409: (0x40, 0x00)}) is True
        assert c.decode({12409: (0x00, 0x00)}) is False


class TestLoadConverterTemplates:
    def test_loads_bundled_wolf_converter_xml(self) -> None:
        """The bundled converter.xml has 1000+ entries; make sure parsing
        produces a sensible result."""
        wolf_dir = (
            Path(__file__).parent.parent.parent
            / "custom_components" / "ecotherm_wolf"
            / "definitions" / "data" / "wolf"
        )
        if not (wolf_dir / "converter.xml").exists():
            pytest.skip("Wolf converter.xml not bundled")
        out = load_converter_templates(wolf_dir / "converter.xml")
        # CTID 10000 is "Tagtemperatur" -> NumericConverter16 with TelegramNr 5.
        assert 10000 in out
        c = out[10000]
        assert isinstance(c, NumericConverter16)
        assert c.info_numbers == [5]
        # CTID 80021 is a UINT32 with TelegramNrHigh=553, TelegramNrLow=552.
        assert 80021 in out
        c32 = out[80021]
        assert isinstance(c32, NumericConverter32)
        assert set(c32.info_numbers) == {552, 553}
        # CTID 40002 is a MixerState (TelegramNrOpen=86, TelegramNrClose=87).
        assert 40002 in out
        cm = out[40002]
        assert isinstance(cm, MixerStateConverter)
        assert set(cm.info_numbers) == {86, 87}

    def test_skips_unknown_template_types(self, tmp_path: Path) -> None:
        """Unsupported template types are silently dropped."""
        xml = tmp_path / "converter.xml"
        xml.write_text(dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <ConverterTemplateConfig>
              <ConverterTemplates>
                <NumericConverter16Template CTID="100">
                  <TelegramNr>1</TelegramNr>
                  <Type>SS</Type>
                </NumericConverter16Template>
                <DateTimeConverterTemplate CTID="200">
                </DateTimeConverterTemplate>
                <NullConverterTemplate CTID="300">
                </NullConverterTemplate>
              </ConverterTemplates>
            </ConverterTemplateConfig>
        """))
        out = load_converter_templates(xml)
        # 100 (Numeric16) and 300 (Null) load. 200 (DateTime) is dropped.
        assert 100 in out
        assert 200 not in out
        assert 300 in out


class TestServiceNumbers:
    """``<ServiceReadNumber>`` / ``<ServiceWriteNumber>`` parsing.

    Wolf reuses the same TelegramNr under several eBus "services" -- the
    CWL exposes telegram 1 under both service 4022 and 4050. The ISM7
    answers 0xFFFF for a service-gated telegram requested without its
    ``snr``, so the converter has to carry the service number through to
    the InfoRead. A converter.xml entry without these elements means "no
    service gate" (``service_read_number is None`` -> ``snr`` omitted).
    """

    def test_fresh_converter_defaults_to_none(self) -> None:
        c = NumericConverter16(10000, telegram_nr=5, type_code="SS10")
        assert c.service_read_number is None
        assert c.service_write_number is None

    def test_parsed_from_xml(self, tmp_path: Path) -> None:
        xml = tmp_path / "converter.xml"
        xml.write_text(dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <ConverterTemplateConfig>
              <ConverterTemplates>
                <NumericConverter16Template CTID="230019">
                  <TelegramNr>33</TelegramNr>
                  <ServiceReadNumber>4050</ServiceReadNumber>
                  <Type>SS</Type>
                </NumericConverter16Template>
                <NumericConverter16Template CTID="100">
                  <TelegramNr>1</TelegramNr>
                  <Type>SS</Type>
                </NumericConverter16Template>
              </ConverterTemplates>
            </ConverterTemplateConfig>
        """))
        out = load_converter_templates(xml)
        # Service-gated converter carries the number.
        assert out[230019].service_read_number == 4050
        # Plain converter (no element) stays None -> snr omitted on the wire.
        assert out[100].service_read_number is None

    def test_service_write_number_parsed(self, tmp_path: Path) -> None:
        xml = tmp_path / "converter.xml"
        xml.write_text(dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <ConverterTemplateConfig>
              <ConverterTemplates>
                <NumericConverter16Template CTID="500">
                  <TelegramNr>9</TelegramNr>
                  <ServiceReadNumber>4022</ServiceReadNumber>
                  <ServiceWriteNumber>4023</ServiceWriteNumber>
                  <Type>SS</Type>
                </NumericConverter16Template>
              </ConverterTemplates>
            </ConverterTemplateConfig>
        """))
        out = load_converter_templates(xml)
        assert out[500].service_read_number == 4022
        assert out[500].service_write_number == 4023

    def test_bundled_cwl_converters_carry_service_numbers(self) -> None:
        """The bundled Wolf converter.xml gives the CWL its 4022/4050 gates.

        Regression for the CWL 0xFFFF bug: the integration used to drop
        ``<ServiceReadNumber>`` entirely, so every CWL telegram went out
        without ``snr`` and the ISM7 answered 0xFFFF for the 4050-service
        half.
        """
        wolf_dir = (
            Path(__file__).parent.parent.parent
            / "custom_components" / "ecotherm_wolf"
            / "definitions" / "data" / "wolf"
        )
        if not (wolf_dir / "converter.xml").exists():
            pytest.skip("Wolf converter.xml not bundled")
        out = load_converter_templates(wolf_dir / "converter.xml")
        # CTID 230010 -> CWL TelegramNr 14, service 4022.
        assert out[230010].service_read_number == 4022
        # CTID 230019 -> CWL TelegramNr 33, service 4050.
        assert out[230019].service_read_number == 4050
        # CTID 10000 (Tagtemperatur) has no service gate.
        assert out[10000].service_read_number is None
