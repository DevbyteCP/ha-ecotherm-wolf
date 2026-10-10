"""Tests for the bundle-builder helper (T21e).

The bundle-builder groups selected (device_id, property_id) pairs into
``InfoRead`` bundles respecting the ISM7 limits:

- Max 20 InfoReads per bundle (configurable for tests).
- All info_reads of one parameter (multi-telegram converters) stay in the
  same bundle.
- Bundles are returned in declaration order; bundle_ids are assigned by
  the caller (sequential strings starting at "1").
"""
from __future__ import annotations

from custom_components.ecotherm_wolf.coordinator import build_bundles
from custom_components.ecotherm_wolf.definitions.catalog import (
    Catalog,
    DeviceDefinition,
    ParameterDefinition,
)
from custom_components.ecotherm_wolf.definitions.codecs import Int16Codec
from custom_components.ecotherm_wolf.definitions.converters import NumericConverter16


def _make_param(
    property_id: int,
    *,
    info_numbers: list[int] | None = None,
    service_read_number: int | None = None,
) -> ParameterDefinition:
    """A test ParameterDefinition.

    ``service_read_number`` attaches a bare ``NumericConverter16`` carrying
    that service number -- ``build_bundles`` reads the service gate off the
    parameter's converter (it never calls the converter's own
    ``info_numbers``, so the stub's TelegramNr is irrelevant).
    """
    converter = None
    if service_read_number is not None:
        converter = NumericConverter16(
            property_id, telegram_nr=property_id, type_code="US",
        )
        converter.service_read_number = service_read_number
    return ParameterDefinition(
        property_id=property_id,
        name=f"p{property_id}",
        display_name=f"Param {property_id}",
        codec=Int16Codec(),
        info_numbers=info_numbers or [property_id],
        converter=converter,
    )


def _make_catalog(params: list[ParameterDefinition]) -> Catalog:
    return Catalog(devices={
        "DEV": DeviceDefinition(
            device_type="DEV",
            display_name="Device",
            parameters={p.property_id: p for p in params},
            dtid=1,
        ),
    })


def test_empty_selection_returns_empty() -> None:
    catalog = _make_catalog([_make_param(1)])
    assert build_bundles(
        [], catalog, {1: ("DEV", "0;0;0;1")},
    ) == []


def test_skips_devices_without_bus_address() -> None:
    """Selections referring to devices with empty bus_address are dropped."""
    catalog = _make_catalog([_make_param(1)])
    bundles = build_bundles(
        [(1, 1)], catalog, {1: ("DEV", "")},
    )
    assert bundles == []


def test_skips_unknown_devices() -> None:
    catalog = _make_catalog([_make_param(1)])
    bundles = build_bundles(
        [(99, 1)], catalog, {1: ("DEV", "0;0;0;1")},
    )
    assert bundles == []


def test_groups_singletons_into_one_bundle_under_cap() -> None:
    """5 single-telegram params with cap=20 fit in a single bundle."""
    catalog = _make_catalog([_make_param(i) for i in range(1, 6)])
    bundles = build_bundles(
        [(1, i) for i in range(1, 6)],
        catalog,
        {1: ("DEV", "0;0;0;1")},
    )
    assert len(bundles) == 1
    assert len(bundles[0]) == 5


def test_splits_into_multiple_bundles_at_cap() -> None:
    """30 single-telegram params with cap=20 -> 2 bundles."""
    catalog = _make_catalog([_make_param(i) for i in range(1, 31)])
    bundles = build_bundles(
        [(1, i) for i in range(1, 31)],
        catalog,
        {1: ("DEV", "0;0;0;1")},
    )
    assert len(bundles) == 2
    total = sum(len(b) for b in bundles)
    assert total == 30
    for bundle in bundles:
        assert len(bundle) <= 20


def test_keeps_multi_telegram_param_in_one_bundle() -> None:
    """A 2-telegram parameter MUST stay together in one bundle."""
    catalog = _make_catalog([
        # Multi-telegram param with telegram nrs that don't collide with
        # the single-telegram params below (those use property_id == nr).
        _make_param(100, info_numbers=[100, 101]),
        *[_make_param(i) for i in range(1, 20)],
    ])
    bundles = build_bundles(
        [(1, 100)] + [(1, i) for i in range(1, 20)],
        catalog,
        {1: ("DEV", "0;0;0;1")},
        max_per_bundle=10,
    )
    # 2-telegram param + 19 single-telegram params = 21 reads, cap 10:
    # need >= 3 bundles.
    assert len(bundles) >= 3
    # The 2-telegram pair (100 and 101) must appear in the same bundle.
    for bundle in bundles:
        nums = {ir.info_number for ir in bundle}
        if 100 in nums or 101 in nums:
            assert {100, 101} <= nums


def test_deduplicates_shared_telegram_numbers() -> None:
    """Params sharing a telegram nr are read only once (CWL regression).

    Wolf's catalog (and the author's curated selection) pairs several
    parameters with *different* names onto the *same* telegram nr. The
    ISM7 tolerates a duplicate (ba, in) on a one-shot pull but breaks its
    eBus poller when the same nr is push-subscribed twice -- the CWL then
    reports 0xFFFF for everything. build_bundles must emit each (ba, in)
    only once; the decode side feeds every sharing parameter from it.
    """
    catalog = _make_catalog([
        _make_param(1, info_numbers=[7]),
        _make_param(2, info_numbers=[7]),  # same telegram nr as param 1
        _make_param(3, info_numbers=[8]),
    ])
    bundles = build_bundles(
        [(1, 1), (1, 2), (1, 3)],
        catalog,
        {1: ("DEV", "0;0;0;1")},
    )
    all_reads = [ir for b in bundles for ir in b]
    keys = [(ir.bus_address, ir.info_number) for ir in all_reads]
    assert keys.count(("0;0;0;1", 7)) == 1  # shared nr read once
    assert keys.count(("0;0;0;1", 8)) == 1
    assert len(all_reads) == 2


def test_propagates_service_read_number_to_inforead() -> None:
    """A parameter whose converter has a ServiceReadNumber sends ``snr``.

    Wolf gates several telegrams behind an eBus "service" (the CWL exposes
    telegram 1 under both 4022 and 4050). The ISM7 answers 0xFFFF when the
    ``ird`` arrives without the matching ``snr`` -- so build_bundles must
    copy the converter's service number onto every InfoRead it builds.
    """
    catalog = _make_catalog([_make_param(1, service_read_number=4050)])
    bundles = build_bundles(
        [(1, 1)], catalog, {1: ("DEV", "0;0;0;1")},
    )
    assert len(bundles) == 1
    assert bundles[0][0].service_number == 4050


def test_param_without_converter_omits_service_number() -> None:
    """No converter / no ServiceReadNumber -> service_number stays -1.

    ``-1`` makes the protocol encoder omit the ``snr`` attribute, which is
    correct for the default-service telegrams the bulk of Wolf modules use.
    """
    catalog = _make_catalog([_make_param(1)])
    bundles = build_bundles(
        [(1, 1)], catalog, {1: ("DEV", "0;0;0;1")},
    )
    assert bundles[0][0].service_number == -1


def test_shared_telegram_different_service_both_kept() -> None:
    """Same TelegramNr under two services are distinct reads -- both survive.

    The CWL exposes telegram 1 under service 4022 AND 4050. An earlier
    build of this integration deduped on ``(ba, in)`` alone and silently
    dropped one half, which is exactly what made the CWL read 0xFFFF. The
    dedup key now carries the service number, so the two reads stay apart.
    """
    catalog = _make_catalog([
        _make_param(1, service_read_number=4022),
        # Distinct parameter, same telegram nr, different eBus service.
        _make_param(2, info_numbers=[1], service_read_number=4050),
    ])
    bundles = build_bundles(
        [(1, 1), (1, 2)], catalog, {1: ("DEV", "0;0;0;1")},
    )
    all_reads = [ir for b in bundles for ir in b]
    keys = {(ir.info_number, ir.service_number) for ir in all_reads}
    assert keys == {(1, 4022), (1, 4050)}
    assert len(all_reads) == 2


def test_shared_telegram_same_service_deduped() -> None:
    """Two parameters reading the identical (telegram, service) collapse to one.

    Genuinely the same physical read -- the decode side feeds both
    parameters from the single InfoRead.
    """
    catalog = _make_catalog([
        _make_param(1, service_read_number=4022),
        _make_param(2, info_numbers=[1], service_read_number=4022),
    ])
    bundles = build_bundles(
        [(1, 1), (1, 2)], catalog, {1: ("DEV", "0;0;0;1")},
    )
    all_reads = [ir for b in bundles for ir in b]
    assert len(all_reads) == 1
    assert (all_reads[0].info_number, all_reads[0].service_number) == (1, 4022)


def test_multi_telegram_claims_shared_nr_over_single() -> None:
    """A multi-telegram param keeps its grouped reads; a single-telegram
    param sharing one of those nrs is dropped (decodes from the shared
    telegram anyway via the per-bus dict)."""
    catalog = _make_catalog([
        _make_param(100, info_numbers=[5, 6]),  # multi-telegram
        _make_param(5, info_numbers=[5]),       # shares nr 5
    ])
    bundles = build_bundles(
        [(1, 100), (1, 5)],
        catalog,
        {1: ("DEV", "0;0;0;1")},
    )
    all_reads = [ir for b in bundles for ir in b]
    keys = [(ir.bus_address, ir.info_number) for ir in all_reads]
    assert keys.count(("0;0;0;1", 5)) == 1  # not read twice
    assert ("0;0;0;1", 6) in keys
    assert len(all_reads) == 2


def test_skips_param_too_large_for_any_bundle() -> None:
    """If a parameter's info_count exceeds the cap, it's logged and skipped."""
    catalog = _make_catalog([
        _make_param(100, info_numbers=[1, 2, 3, 4, 5]),  # 5 telegrams
    ])
    bundles = build_bundles(
        [(1, 100)], catalog, {1: ("DEV", "0;0;0;1")},
        max_per_bundle=2,  # less than parameter needs
    )
    assert bundles == []


def test_bus_address_propagated_to_each_inforead() -> None:
    """Every InfoRead in a bundle carries the device's bus_address."""
    catalog = _make_catalog([_make_param(1)])
    bundles = build_bundles(
        [(7, 1)], catalog, {7: ("DEV", "0;0;0;7")},
    )
    assert bundles[0][0].bus_address == "0;0;0;7"


def test_no_bundle_mixes_bus_addresses() -> None:
    """Selections spanning 2+ bus addresses never share a bundle.

    Mixing bus addresses in one bundle forces the ISM7 to poll several
    eBus devices for a single request -- that overloads its internal
    poller and makes slow modules (the CWL on 0x7C) return 0xFFFF. zivillian
    builds bundles per bus address (``GetBundlesForDevice``); build_bundles
    must do the same. Each device here gets a handful of single-telegram
    params, comfortably under the cap, so the *only* thing that can split
    them is the per-bus rule.
    """
    catalog = _make_catalog([_make_param(i) for i in range(1, 13)])
    # 3 devices on 3 distinct bus addresses, 4 params each.
    device_map = {
        10: ("DEV", "0x35"),
        20: ("DEV", "0x51"),
        30: ("DEV", "0x7C"),  # the CWL
    }
    selected = (
        [(10, i) for i in range(1, 5)]
        + [(20, i) for i in range(5, 9)]
        + [(30, i) for i in range(9, 13)]
    )
    bundles = build_bundles(selected, catalog, device_map)
    # Three devices, each fits in one bundle -> 3 bundles, none mixed.
    assert len(bundles) == 3
    for bundle in bundles:
        assert len({ir.bus_address for ir in bundle}) == 1
    # Every selected telegram landed somewhere.
    assert sum(len(b) for b in bundles) == 12
    assert {b[0].bus_address for b in bundles} == {"0x35", "0x51", "0x7C"}


def test_cap_applied_per_bus_address() -> None:
    """The 20-cap is enforced *within* each bus-address group.

    25 single-telegram params on bus A + 25 on bus B, cap 20: a naive
    global bin-packer would emit 3 bundles (20+20+10) mixing A and B.
    The per-bus builder must emit >= 4 (>= 2 per bus), each single-bus and
    each <= cap.
    """
    # 50 distinct property ids: 1..25 on DEV_A, 101..125 on DEV_B. The two
    # devices share a device_type so one catalog covers both -- the split
    # is driven purely by the bus address.
    catalog = _make_catalog(
        [_make_param(i) for i in range(1, 26)]
        + [_make_param(i) for i in range(101, 126)],
    )
    device_map = {
        1: ("DEV", "0x35"),  # bus A
        2: ("DEV", "0x7C"),  # bus B (the CWL)
    }
    selected = (
        [(1, i) for i in range(1, 26)]
        + [(2, i) for i in range(101, 126)]
    )
    bundles = build_bundles(selected, catalog, device_map)
    assert len(bundles) >= 4
    for bundle in bundles:
        assert len(bundle) <= 20
        assert len({ir.bus_address for ir in bundle}) == 1
    # Exactly 2 bundles per bus (25 reads / cap 20 = 2 each), 50 reads total.
    by_bus: dict[str, int] = {}
    for bundle in bundles:
        by_bus[bundle[0].bus_address] = by_bus.get(bundle[0].bus_address, 0) + 1
    assert by_bus == {"0x35": 2, "0x7C": 2}
    assert sum(len(b) for b in bundles) == 50


def test_bundles_deterministic_bus_address_order() -> None:
    """Bundle order follows sorted bus addresses (deterministic bundle_ids).

    The caller stamps sequential bundle_ids onto the returned list, so the
    list order must be stable regardless of selection / device_map order.
    """
    catalog = _make_catalog([_make_param(1), _make_param(2), _make_param(3)])
    device_map = {
        1: ("DEV", "0x7C"),
        2: ("DEV", "0x35"),
        3: ("DEV", "0x51"),
    }
    # Feed the selection in a deliberately unsorted order.
    bundles = build_bundles([(1, 1), (3, 3), (2, 2)], catalog, device_map)
    bus_order = [b[0].bus_address for b in bundles]
    assert bus_order == sorted(bus_order)
    assert bus_order == ["0x35", "0x51", "0x7C"]


def test_multi_telegram_param_stays_single_bus() -> None:
    """A multi-telegram param's reads stay together AND on its own bus.

    A multi-telegram parameter has exactly one bus address, so per-bus
    grouping is compatible with keeping its telegrams in one bundle. Mixing
    it with a param on a different bus must still split the bundle.
    """
    catalog = _make_catalog([
        _make_param(100, info_numbers=[100, 101]),  # 2-telegram, bus A
        _make_param(5),                             # single-telegram, bus B
    ])
    device_map = {
        1: ("DEV", "0x35"),  # bus A -- multi-telegram param
        2: ("DEV", "0x7C"),  # bus B -- single-telegram param
    }
    bundles = build_bundles([(1, 100), (2, 5)], catalog, device_map)
    assert len(bundles) == 2
    for bundle in bundles:
        assert len({ir.bus_address for ir in bundle}) == 1
    # The 2-telegram pair shares one (single-bus) bundle.
    for bundle in bundles:
        nums = {ir.info_number for ir in bundle}
        if 100 in nums or 101 in nums:
            assert {100, 101} <= nums
            assert {ir.bus_address for ir in bundle} == {"0x35"}
