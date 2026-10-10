# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Coordinator's codec-coercion (Task 2 of the live-test fixes).

Wolf catalog quirk: ``ListParameterDescriptor`` maps to an ``EnumCodec``
*plus* a ``NumericConverter16``. The converter returns a raw int; the
Select platform expects the mapped option string. The coordinator's
``_coerce_to_codec`` helper bridges that gap. Pure unit tests below --
no HA fixture needed.
"""
from __future__ import annotations

import pytest

from custom_components.ecotherm_wolf.coordinator import (
    _coerce_from_codec,
    _coerce_to_codec,
)
from custom_components.ecotherm_wolf.definitions.codecs import (
    BoolCodec,
    EnumCodec,
    Int16Codec,
    ScaledCodec,
    TimeOfDayCodec,
)
from custom_components.ecotherm_wolf.ism7.exceptions import WriteRejected


class TestEnumCoercion:
    def test_int_in_range_maps_to_member_name(self) -> None:
        codec = EnumCodec(
            members={0: "Aus", 1: "Auto", 2: "Heizen", 3: "Feuchteschutz"},
            writable=True,
        )
        assert _coerce_to_codec(3, codec) == "Feuchteschutz"

    def test_int_out_of_range_returns_none(self) -> None:
        codec = EnumCodec(members={0: "Aus", 1: "Auto"}, writable=True)
        # No member 99 -> None so the Select entity surfaces "unknown"
        # instead of caching a fake string.
        assert _coerce_to_codec(99, codec) is None

    def test_string_passthrough_for_synthetic_codec_path(self) -> None:
        """Synthetic catalogs decode directly to string via the codec; we
        must not re-map a string back through ``members.get``."""
        codec = EnumCodec(members={0: "Aus", 1: "Auto"}, writable=True)
        assert _coerce_to_codec("Auto", codec) == "Auto"

    def test_none_passthrough(self) -> None:
        codec = EnumCodec(members={0: "Aus", 1: "Auto"})
        assert _coerce_to_codec(None, codec) is None


class TestBoolCoercion:
    def test_int_zero_maps_to_false(self) -> None:
        assert _coerce_to_codec(0, BoolCodec(writable=True)) is False

    def test_int_nonzero_maps_to_true(self) -> None:
        assert _coerce_to_codec(7, BoolCodec(writable=True)) is True

    def test_bool_passthrough(self) -> None:
        assert _coerce_to_codec(True, BoolCodec()) is True
        assert _coerce_to_codec(False, BoolCodec()) is False


class TestNumericPassthrough:
    def test_int16(self) -> None:
        # No coercion for plain numeric codecs.
        assert _coerce_to_codec(42, Int16Codec()) == 42

    def test_scaled(self) -> None:
        codec = ScaledCodec(base=Int16Codec(), scale=10)
        assert _coerce_to_codec(24.5, codec) == 24.5

    def test_time_of_day(self) -> None:
        assert _coerce_to_codec("08:30", TimeOfDayCodec()) == "08:30"


class TestCoerceFromCodec:
    """Encode side: HA's typed value -> the int the converter expects.

    Inverse of ``_coerce_to_codec``. Regression for the live bug where
    ``select_option`` raised ``invalid literal for int()`` because the
    option string reached ``NumericConverter16.encode`` unmapped.
    """

    def test_enum_option_string_maps_to_member_id(self) -> None:
        codec = EnumCodec(
            members={0: "Auto", 1: "Reduzierte Lüftung", 2: "Nennlüftung"},
            writable=True,
        )
        assert _coerce_from_codec("Reduzierte Lüftung", codec) == 1
        assert _coerce_from_codec("Auto", codec) == 0

    def test_enum_int_passthrough(self) -> None:
        codec = EnumCodec(members={0: "Auto", 1: "Heizen"}, writable=True)
        assert _coerce_from_codec(2, codec) == 2

    def test_enum_unknown_option_rejected(self) -> None:
        codec = EnumCodec(members={0: "Auto"}, writable=True)
        with pytest.raises(WriteRejected):
            _coerce_from_codec("Nonexistent", codec)

    def test_bool_true_false_to_int(self) -> None:
        assert _coerce_from_codec(True, BoolCodec(writable=True)) == 1
        assert _coerce_from_codec(False, BoolCodec(writable=True)) == 0

    def test_bool_string_forms(self) -> None:
        codec = BoolCodec(writable=True)
        assert _coerce_from_codec("on", codec) == 1
        assert _coerce_from_codec("off", codec) == 0

    def test_numeric_passthrough(self) -> None:
        assert _coerce_from_codec(20, Int16Codec()) == 20
        scaled = ScaledCodec(base=Int16Codec(), scale=10)
        assert _coerce_from_codec(24.5, scaled) == 24.5
