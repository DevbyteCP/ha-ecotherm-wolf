from __future__ import annotations

import pytest

from custom_components.ecotherm_wolf.definitions.codecs import (
    BoolCodec,
    EnumCodec,
    Int16Codec,
    ScaledCodec,
    TimeOfDayCodec,
    Uint8Codec,
    Uint16Codec,
    ValidationError,
)


class TestInt16Codec:
    def test_decodes_positive(self):
        codec = Int16Codec()
        assert codec.decode(b"\x01\xF4") == 500

    def test_decodes_negative(self):
        codec = Int16Codec()
        assert codec.decode(b"\xFF\x9C") == -100

    def test_encodes_round_trip(self):
        codec = Int16Codec()
        assert codec.decode(codec.encode(-12345)) == -12345

    def test_writable_when_range_provided(self):
        codec = Int16Codec(min_value=0, max_value=100)
        assert codec.writable is True

    def test_read_only_when_no_range(self):
        codec = Int16Codec()
        assert codec.writable is False

    def test_rejects_out_of_range(self):
        codec = Int16Codec(min_value=0, max_value=100)
        with pytest.raises(ValidationError):
            codec.encode(150)


class TestScaledCodec:
    def test_decode_divides_by_scale(self):
        # raw int16 = 245, scale = 10 -> 24.5
        codec = ScaledCodec(base=Int16Codec(), scale=10)
        assert codec.decode(b"\x00\xF5") == pytest.approx(24.5)

    def test_encode_multiplies_by_scale(self):
        codec = ScaledCodec(base=Int16Codec(min_value=-50, max_value=50), scale=10)
        assert codec.encode(24.5) == b"\x00\xF5"

    def test_round_trip(self):
        codec = ScaledCodec(base=Int16Codec(min_value=-100, max_value=100), scale=10)
        assert codec.decode(codec.encode(42.7)) == pytest.approx(42.7)

    def test_encode_requires_bounds(self):
        codec = ScaledCodec(base=Int16Codec(), scale=10)  # no bounds
        with pytest.raises(ValidationError):
            codec.encode(24.5)


class TestEnumCodec:
    def test_decode_returns_member_name(self):
        codec = EnumCodec(members={0: "Aus", 1: "Auto", 2: "Heizen"})
        assert codec.decode(b"\x01") == "Auto"

    def test_decode_unknown_returns_raw_string(self):
        codec = EnumCodec(members={0: "Aus", 1: "Auto"})
        assert codec.decode(b"\x09") == "unknown_9"

    def test_encode_member_name(self):
        codec = EnumCodec(members={0: "Aus", 1: "Auto"}, writable=True)
        assert codec.encode("Auto") == b"\x01"

    def test_encode_rejects_unknown_member(self):
        codec = EnumCodec(members={0: "Aus", 1: "Auto"}, writable=True)
        with pytest.raises(ValidationError):
            codec.encode("Unsinn")

    def test_round_trip(self):
        codec = EnumCodec(members={0: "Aus", 1: "Auto", 2: "Heizen"}, writable=True)
        assert codec.decode(codec.encode("Auto")) == "Auto"


class TestBoolCodec:
    def test_decode_true(self):
        assert BoolCodec().decode(b"\x01") is True

    def test_decode_false(self):
        assert BoolCodec().decode(b"\x00") is False

    def test_encode_true(self):
        assert BoolCodec(writable=True).encode(True) == b"\x01"

    def test_encode_false(self):
        assert BoolCodec(writable=True).encode(False) == b"\x00"


class TestUint8Codec:
    def test_decode(self):
        assert Uint8Codec().decode(b"\xFF") == 255

    def test_encode_rejects_negative(self):
        codec = Uint8Codec(min_value=0, max_value=100)
        with pytest.raises(ValidationError):
            codec.encode(-1)


class TestUint16Codec:
    def test_decodes(self):
        assert Uint16Codec().decode(b"\xFF\xFF") == 65535

    def test_decodes_zero(self):
        assert Uint16Codec().decode(b"\x00\x00") == 0

    def test_rejects_negative(self):
        codec = Uint16Codec(min_value=0, max_value=1000)
        with pytest.raises(ValidationError):
            codec.encode(-5)

    def test_rejects_above_max(self):
        codec = Uint16Codec(min_value=0, max_value=1000)
        with pytest.raises(ValidationError):
            codec.encode(2000)


class TestTimeOfDayCodec:
    def test_decodes_hour_minute(self):
        # raw uint16 minutes-since-midnight = 8*60+30 = 510 -> "08:30"
        assert TimeOfDayCodec().decode(b"\x01\xFE") == "08:30"

    def test_round_trip(self):
        codec = TimeOfDayCodec(writable=True)
        assert codec.decode(codec.encode("17:45")) == "17:45"
