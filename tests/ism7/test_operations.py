"""Operations layer tests.

After T21d, ``decode_value_update`` operates on a ``TelegramBundleResp``
containing ``InfonumberReadResp`` entries (info_number + bus_address +
dl/dh hex bytes), translating those into our internal (device_id,
property_id, value) tuples via the parameter catalog.
"""
from __future__ import annotations

import pytest

from custom_components.ecotherm_wolf.definitions.catalog import (
    Catalog,
    DeviceDefinition,
    ParameterDefinition,
)
from custom_components.ecotherm_wolf.definitions.codecs import (
    BoolCodec,
    EnumCodec,
    Int16Codec,
    ScaledCodec,
)
from custom_components.ecotherm_wolf.ism7.operations import (
    DecodedValue,
    decode_value_update,
)
from custom_components.ecotherm_wolf.ism7.protocol import (
    InfonumberReadResp,
    TelegramBundleResp,
)


@pytest.fixture
def catalog() -> Catalog:
    return Catalog(devices={
        "CHA-10": DeviceDefinition(
            device_type="CHA-10",
            display_name="CHA",
            parameters={
                0x0080: ParameterDefinition(
                    property_id=0x0080,
                    name="vorlauftemp",
                    display_name="Vorlauftemperatur",
                    codec=ScaledCodec(base=Int16Codec(unit="°C"), scale=10),
                ),
                0x00A1: ParameterDefinition(
                    property_id=0x00A1,
                    name="betriebsart",
                    display_name="Betriebsart",
                    codec=EnumCodec(
                        members={0: "Aus", 1: "Auto", 2: "Heiz"},
                        writable=True,
                    ),
                ),
                0x00B0: ParameterDefinition(
                    property_id=0x00B0,
                    name="pumpe",
                    display_name="Pumpe",
                    codec=BoolCodec(writable=False),
                ),
            },
            dtid=12345,
        ),
    })


def _resp(reads: list[InfonumberReadResp]) -> TelegramBundleResp:
    return TelegramBundleResp(
        bundle_id="b1", state="ok", read_telegrams=reads,
    )


def test_decode_int16_scaled(catalog: Catalog) -> None:
    """ScaledCodec(Int16) reads dh+dl as big-endian: 00F5 = 245 / 10 = 24.5."""
    resp = _resp([
        InfonumberReadResp(
            info_number=0x0080, bus_address="0;0;0;1",
            state="ok", db_low="F5", db_high="00",
        ),
    ])
    out = decode_value_update(
        resp,
        device_map={1: ("CHA-10", "0;0;0;1")},
        catalog=catalog,
    )
    assert out == [DecodedValue(
        device_id=1, device_type="CHA-10",
        property_id=0x0080, name="vorlauftemp", value=24.5,
    )]


def test_decode_enum_uses_low_byte_only(catalog: Catalog) -> None:
    """EnumCodec.decode reads ``raw[0]``; with dl="01", dh="00" we want 1."""
    resp = _resp([
        InfonumberReadResp(
            info_number=0x00A1, bus_address="0;0;0;1",
            state="ok", db_low="01", db_high="00",
        ),
    ])
    out = decode_value_update(
        resp,
        device_map={1: ("CHA-10", "0;0;0;1")},
        catalog=catalog,
    )
    assert out[0].value == "Auto"


def test_decode_bool_uses_low_byte_only(catalog: Catalog) -> None:
    """BoolCodec is true when the low byte is non-zero."""
    resp = _resp([
        InfonumberReadResp(
            info_number=0x00B0, bus_address="0;0;0;1",
            state="ok", db_low="01", db_high="00",
        ),
        InfonumberReadResp(
            info_number=0x00B0, bus_address="0;0;0;1",
            state="ok", db_low="00", db_high="00",
        ),
    ])
    out = decode_value_update(
        resp,
        device_map={1: ("CHA-10", "0;0;0;1")},
        catalog=catalog,
    )
    assert [d.value for d in out] == [True, False]


def test_decode_skips_records_with_error_state(catalog: Catalog) -> None:
    resp = _resp([
        InfonumberReadResp(
            info_number=0x0080, bus_address="0;0;0;1",
            state="error", db_low="F5", db_high="00",
        ),
    ])
    out = decode_value_update(
        resp,
        device_map={1: ("CHA-10", "0;0;0;1")},
        catalog=catalog,
    )
    assert out == []


def test_decode_skips_unknown_bus_address(catalog: Catalog) -> None:
    resp = _resp([
        InfonumberReadResp(
            info_number=0x0080, bus_address="9;9;9;9",
            state="ok", db_low="F5", db_high="00",
        ),
    ])
    out = decode_value_update(
        resp,
        device_map={1: ("CHA-10", "0;0;0;1")},
        catalog=catalog,
    )
    assert out == []


def test_decode_skips_unknown_info_number(catalog: Catalog) -> None:
    resp = _resp([
        InfonumberReadResp(
            info_number=0xDEAD, bus_address="0;0;0;1",
            state="ok", db_low="F5", db_high="00",
        ),
    ])
    out = decode_value_update(
        resp,
        device_map={1: ("CHA-10", "0;0;0;1")},
        catalog=catalog,
    )
    assert out == []


def test_decode_handles_legacy_string_device_map(catalog: Catalog) -> None:
    """A legacy device_map storing just the device_type string skips the read.

    With no bus_address we cannot match the wire-side bus_address -- the
    record gets logged-and-dropped. Verifies we don't crash on legacy data.
    """
    resp = _resp([
        InfonumberReadResp(
            info_number=0x0080, bus_address="0;0;0;1",
            state="ok", db_low="F5", db_high="00",
        ),
    ])
    out = decode_value_update(
        resp,
        device_map={1: "CHA-10"},  # type: ignore[dict-item]
        catalog=catalog,
    )
    assert out == []


def test_decode_matches_on_read_address_of_three_tuple(catalog: Catalog) -> None:
    """A 3-tuple device_map entry is matched on its READ address.

    Incoming telegrams are read responses, so ``decode_value_update`` must
    match them against the device's read address -- the second tuple
    element -- and ignore the (divergent) write address entirely. Here the
    read address ``0;0;0;1`` matches; the write address ``0;0;9;9`` is a
    decoy that must NOT be consulted.
    """
    resp = _resp([
        InfonumberReadResp(
            info_number=0x0080, bus_address="0;0;0;1",
            state="ok", db_low="F5", db_high="00",
        ),
    ])
    out = decode_value_update(
        resp,
        device_map={1: ("CHA-10", "0;0;0;1", "0;0;9;9")},
        catalog=catalog,
    )
    assert out == [DecodedValue(
        device_id=1, device_type="CHA-10",
        property_id=0x0080, name="vorlauftemp", value=24.5,
    )]


def test_decode_does_not_match_on_write_address(catalog: Catalog) -> None:
    """A telegram arriving on a device's WRITE address is not matched.

    Reads never come back on the write address, so a telegram whose
    bus_address equals only the write address (not the read address) must
    be logged-and-dropped, not silently mis-attributed.
    """
    resp = _resp([
        InfonumberReadResp(
            info_number=0x0080, bus_address="0;0;9;9",  # the write address
            state="ok", db_low="F5", db_high="00",
        ),
    ])
    out = decode_value_update(
        resp,
        device_map={1: ("CHA-10", "0;0;0;1", "0;0;9;9")},
        catalog=catalog,
    )
    assert out == []
