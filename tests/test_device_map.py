"""Tests for the device-map normalisation helpers.

Wolf modules can listen for write commands on a different bus address than
the one they report reads on, so ``device_map`` entries carry a 3-tuple
``(device_type, read_address, write_address)``. ``_normalise_device_map``
coerces stored config-entry data (which may predate the read/write split)
into that shape; ``_entry_read_address`` is the tolerant read-side accessor
used by the public ``build_bundles`` helper.

These are pure functions -- no Home Assistant import needed.
"""
from __future__ import annotations

from custom_components.ecotherm_wolf.coordinator import (
    _entry_read_address,
    _normalise_device_map,
)


def test_normalise_three_element_used_as_is() -> None:
    """A 3-element ``[type, read, write]`` entry is kept verbatim."""
    out = _normalise_device_map({1: ["CHA", "0x8", "0x3"]})
    assert out == {1: ("CHA", "0x8", "0x3")}


def test_normalise_three_element_tuple_used_as_is() -> None:
    """A 3-tuple entry (not list) is also accepted."""
    out = _normalise_device_map({2: ("BM-2", "0x35", "0x30")})
    assert out == {2: ("BM-2", "0x35", "0x30")}


def test_normalise_two_element_defaults_write_to_read() -> None:
    """A legacy 2-element ``[type, addr]`` entry -> write == read.

    This is the critical backward-compat path: config entries created
    before the read/write split stored a single address. Using it as the
    write address too preserves the previous behaviour exactly for the
    majority of Wolf modules (where write == read).
    """
    out = _normalise_device_map({1: ["CHA-10", "0;0;0;1"]})
    assert out == {1: ("CHA-10", "0;0;0;1", "0;0;0;1")}
    # Tuple form too.
    out = _normalise_device_map({3: ("MM", "0x51")})
    assert out == {3: ("MM", "0x51", "0x51")}


def test_normalise_legacy_string_gives_empty_addresses() -> None:
    """A pre-T21d plain-string entry -> both addresses empty."""
    out = _normalise_device_map({1: "CHA"})
    assert out == {1: ("CHA", "", "")}


def test_normalise_mixed_shapes_in_one_map() -> None:
    """A map mixing all three shapes normalises each entry independently."""
    out = _normalise_device_map({
        1: ["CHA", "0x8", "0x3"],   # new 3-element
        2: ["BM-2", "0x35"],         # legacy 2-element
        3: "MM",                      # legacy string
    })
    assert out == {
        1: ("CHA", "0x8", "0x3"),
        2: ("BM-2", "0x35", "0x35"),
        3: ("MM", "", ""),
    }


def test_normalise_drops_malformed_entry() -> None:
    """A malformed entry (e.g. an int, or a 4-element list) is dropped."""
    out = _normalise_device_map({
        1: ["CHA", "0x8", "0x3"],
        2: 12345,                       # not a tuple/list/str
        3: ["too", "many", "el", "s"],  # wrong arity
    })
    assert out == {1: ("CHA", "0x8", "0x3")}


def test_normalise_coerces_string_keys() -> None:
    """JSON stores device-id keys as strings; they're coerced to int."""
    out = _normalise_device_map({"1": ["CHA", "0x8", "0x3"]})
    assert out == {1: ("CHA", "0x8", "0x3")}


def test_entry_read_address_three_tuple() -> None:
    """``_entry_read_address`` returns (type, read) from a 3-tuple."""
    assert _entry_read_address(("CHA", "0x8", "0x3")) == ("CHA", "0x8")


def test_entry_read_address_two_tuple() -> None:
    """A legacy 2-tuple -> (type, read); the single address is the read addr."""
    assert _entry_read_address(("BM-2", "0x35")) == ("BM-2", "0x35")
    assert _entry_read_address(["MM", "0x51"]) == ("MM", "0x51")


def test_entry_read_address_legacy_string() -> None:
    """A legacy plain-string entry -> (type, "") -- no address known."""
    assert _entry_read_address("CHA") == ("CHA", "")
