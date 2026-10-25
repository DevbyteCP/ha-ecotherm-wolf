"""Tests for the Wolf-XML format loader (T21d).

The bundled Wolf XMLs come from zivillian/ism7mqtt under GPL-3.0 and live in
``custom_components/ecotherm_wolf/definitions/data/wolf``. The loader maps:

- ``DeviceTemplate`` -> ``DeviceDefinition`` (dtid + parameters)
- ``NumericParameterDescriptor`` -> ``Int16Codec`` or ``ScaledCodec``
- ``ListParameterDescriptor`` -> ``EnumCodec`` (or ``BoolCodec`` for Ein/Aus)
"""
from __future__ import annotations

import logging
from pathlib import Path
from textwrap import dedent

import pytest
from lxml import etree

from custom_components.ecotherm_wolf.definitions.codecs import (
    BoolCodec,
    EnumCodec,
    ScaledCodec,
)
from custom_components.ecotherm_wolf.definitions.wolf_loader import (
    _load_curated_ranges,
    _load_curated_standard,
    _load_curated_write_addresses,
    load_wolf_catalog,
)

# Needs Wolf's XMLs/certificate (scripts/fetch_wolf_files.py --bundled).
pytestmark = pytest.mark.requires_wolf_files


WOLF_DIR = (
    Path(__file__).parent.parent.parent
    / "custom_components"
    / "ecotherm_wolf"
    / "definitions"
    / "data"
    / "wolf"
)
# The author's curated selection ships next to the wolf/ XML dir.
CURATED_JSON = WOLF_DIR.parent / "curated_standard.json"
# The curated write-bus-address map ships in the same data/ directory.
CURATED_WRITE_JSON = WOLF_DIR.parent / "curated_bus_addresses.json"
# Curated bounds overrides for deduplicated variants, same data/ directory.
CURATED_RANGES_JSON = WOLF_DIR.parent / "curated_ranges.json"

# Wolf modules whose write bus address differs from their read address,
# from the author's ism7-parameters-Wolf.json. DTIDs not listed here have
# write == read and are intentionally absent from the curated map.
EXPECTED_CURATED_WRITE_ADDRESSES = {
    220000: "0x30",
    350000: "0x30",
    270000: "0x3",
}

# The 6 DTIDs the author curated, with the expected PTID-set sizes. DTID
# 360000 (MK_BM-2) curates the surviving deduped rows (360116 Raumtemperatur,
# 360029 Sollwertkorrektur), not the dead duplicates -> 17 unique PTIDs.
EXPECTED_CURATED_SIZES = {
    40000: 17,
    220000: 13,
    230000: 20,
    270000: 44,
    350000: 4,
    360000: 17,
}


@pytest.fixture(scope="module")
def wolf_catalog():
    if not WOLF_DIR.exists():
        pytest.skip("Wolf XMLs not bundled in this checkout")
    return load_wolf_catalog(WOLF_DIR)


def test_loads_known_devices(wolf_catalog) -> None:
    # CHA, BM, BM-2, MM, KM are all real Wolf module names.
    for name in ("CHA", "BM", "BM-2", "CGB-2", "CWL"):
        assert name in wolf_catalog.devices, f"missing {name}"


def test_device_has_dtid(wolf_catalog) -> None:
    """Every loaded device gets a non-zero DTID from the XML."""
    for dev in wolf_catalog.devices.values():
        assert dev.dtid > 0


def test_parameters_have_info_numbers_from_converter(wolf_catalog) -> None:
    """Each parameter's info_numbers come from its ConverterTemplate.

    PTID 10000 ("Tagtemperatur") has CTID 10000 -> NumericConverter16Template
    with TelegramNr=5, so info_numbers=[5] (NOT [10000]). Fixes the
    pre-T21e assumption that PTID and InfoNumber were always equal.
    """
    dhk = wolf_catalog.get_device("DHK_BM")
    p = dhk.parameters[10000]
    assert p.info_numbers == [5]
    # 32-bit converters give 2 info_numbers.
    for dev in wolf_catalog.devices.values():
        for param in dev.parameters.values():
            assert param.info_numbers, "every loaded parameter must have ≥1 info_number"
            assert param.converter is not None


def test_numeric_parameter_with_decimals_is_scaled(wolf_catalog) -> None:
    """PTID 10000 (Tagtemperatur) has Decimals=1 and writable bounds 5..30."""
    dhk = wolf_catalog.get_device("DHK_BM")
    p = dhk.parameters[10000]
    assert isinstance(p.codec, ScaledCodec)
    assert p.codec.scale == 10
    assert p.codec.base.unit == "°C"
    assert p.codec.base.min_value == 5
    assert p.codec.base.max_value == 30


def test_list_parameter_with_two_options_becomes_enum(wolf_catalog) -> None:
    """PTID 10004 (Raumeinfluss) is a 2-option list 0;Aus;1;Ein -> Bool, OR
    enum if not detected as boolean -- depends on heuristic."""
    dhk = wolf_catalog.get_device("DHK_BM")
    p = dhk.parameters[10004]
    # 0;Aus;1;Ein matches the boolean heuristic.
    assert isinstance(p.codec, BoolCodec)


def test_list_parameter_with_three_options_is_enum(wolf_catalog) -> None:
    """PTID 10010 has 6 options -> EnumCodec."""
    dhk = wolf_catalog.get_device("DHK_BM")
    p = dhk.parameters[10010]
    assert isinstance(p.codec, EnumCodec)
    assert p.codec.options == [
        "Abgastest", "Standby-Betrieb", "Automatikbetrieb",
        "Heizbetrieb", "Absenkbetrieb", "Sommerbetrieb",
    ]


def test_readonly_numeric_has_no_bounds(wolf_catalog) -> None:
    """PTID 10012 (Raumtemperatur) is read-only; no min/max."""
    dhk = wolf_catalog.get_device("DHK_BM")
    p = dhk.parameters[10012]
    # ReadOnly: ScaledCodec wrapping Int16Codec with no bounds.
    assert isinstance(p.codec, ScaledCodec)
    assert p.codec.base.min_value is None
    assert p.codec.base.max_value is None


def test_loader_rejects_missing_xmls(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_wolf_catalog(tmp_path)


def test_snake_case_names(wolf_catalog) -> None:
    """Display names get translated to snake_case for HA entity stability."""
    dhk = wolf_catalog.get_device("DHK_BM")
    p = dhk.parameters[10000]
    # "Tagtemperatur" -> "tagtemperatur"
    assert p.name == "tagtemperatur"
    p = dhk.parameters[10005]
    # "Wi/So Umschaltung" -> "wi_so_umschaltung"
    assert p.name == "wi_so_umschaltung"


def test_collision_disambiguates_with_ptid(wolf_catalog) -> None:
    """Two PTIDs with the same Name (Tagtemperatur) get unique snake names."""
    dhk = wolf_catalog.get_device("DHK_BM")
    mk = wolf_catalog.get_device("MK_BM")
    n1 = dhk.parameters[10000].name
    n2 = mk.parameters[20000].name
    assert n1 != n2


def test_malformed_parameter_xml_skips_and_continues(
    tmp_path: Path, caplog,
) -> None:
    """A malformed/unreadable parameter.xml must raise rather than corrupt the catalog.

    The loader contract: bundled XMLs are trusted, but if they're corrupt we
    surface that as a hard error -- corrupt input would otherwise yield an
    empty catalog that silently breaks every entity. The user-override path
    (load_catalog_from_dir) is more permissive and logs-and-continues; this
    test pins the wolf-loader's stricter contract.
    """
    import logging

    (tmp_path / "device.xml").write_text(
        "<DeviceList></DeviceList>", encoding="utf-8",
    )
    (tmp_path / "parameter.xml").write_text(
        "<DescriptorList><not-closing>", encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(etree.XMLSyntaxError):
            load_wolf_catalog(tmp_path)


# --- App flag + catalog dedup (live-test bug fixes) ----------------------
#
# These build minimal hand-rolled XML triples so the assertions don't depend
# on the (large, GPL-bundled) real Wolf catalog. The structure mirrors what
# ``load_wolf_catalog`` parses: ``DeviceTemplate`` (iter), ``ParameterList``
# and ``ConverterTemplates`` as direct children of their roots.


def _write_wolf_triple(
    tmp_path: Path,
    *,
    parameters: str,
    converters: str,
    parameter_refs: str,
) -> Path:
    """Write a device/parameter/converter.xml triple, return the dir."""
    (tmp_path / "device.xml").write_text(
        dedent(f"""\
        <DeviceTemplateConfig>
          <DeviceTemplates>
            <DeviceTemplate DTID="10000" Name="TESTDEV">
              <SoftwareNumber>0x0</SoftwareNumber>
              <WRSDeviceIds>0x0</WRSDeviceIds>
              <ParameterReferenceList>
                {parameter_refs}
              </ParameterReferenceList>
            </DeviceTemplate>
          </DeviceTemplates>
        </DeviceTemplateConfig>
        """),
        encoding="utf-8",
    )
    (tmp_path / "parameter.xml").write_text(
        dedent(f"""\
        <ParameterTemplateConfig>
          <ParameterList>
            {parameters}
          </ParameterList>
        </ParameterTemplateConfig>
        """),
        encoding="utf-8",
    )
    (tmp_path / "converter.xml").write_text(
        dedent(f"""\
        <ConverterTemplateConfig>
          <ConverterTemplates>
            {converters}
          </ConverterTemplates>
        </ConverterTemplateConfig>
        """),
        encoding="utf-8",
    )
    return tmp_path


def _numeric_param(ptid: int, name: str, *, app: bool) -> str:
    app_elem = "<App>true</App>" if app else ""
    return (
        f'<NumericParameterDescriptor PTID="{ptid}">'
        f"<Name>{name}</Name><UnitName>&#176;C</UnitName>{app_elem}"
        f"<ReadOnlyConditionId>False</ReadOnlyConditionId>"
        f"<MinValueCondition>0</MinValueCondition>"
        f"<MaxValueCondition>100</MaxValueCondition>"
        f"<Decimals>0</Decimals>"
        f"</NumericParameterDescriptor>"
    )


def _converter(ctid: int, telegram_nr: int) -> str:
    return (
        f'<NumericConverter16Template CTID="{ctid}">'
        f"<TelegramNr>{telegram_nr}</TelegramNr><Type>US</Type>"
        f"</NumericConverter16Template>"
    )


def test_app_flag_parsed_from_parameter_xml(tmp_path: Path) -> None:
    """``<App>true</App>`` -> ``param.app is True``; absent element -> False."""
    wolf_dir = _write_wolf_triple(
        tmp_path,
        parameters="\n".join([
            _numeric_param(100, "MitApp", app=True),
            _numeric_param(200, "OhneApp", app=False),
        ]),
        converters="\n".join([_converter(100, 5), _converter(200, 6)]),
        parameter_refs=(
            '<ParameterReference PTID="100"/>'
            '<ParameterReference PTID="200"/>'
        ),
    )
    cat = load_wolf_catalog(wolf_dir)
    dev = cat.get_device("TESTDEV")
    assert dev.parameters[100].app is True
    assert dev.parameters[200].app is False


def test_dedup_drops_identical_redundant_parameters(
    tmp_path: Path, caplog,
) -> None:
    """Two params, same Name + converter + TelegramNr -> only the first kept."""
    wolf_dir = _write_wolf_triple(
        tmp_path,
        parameters="\n".join([
            _numeric_param(100, "Programmwahl", app=True),
            _numeric_param(101, "Programmwahl", app=True),
        ]),
        # Both converters read the SAME telegram (10100) -> redundant.
        converters="\n".join([_converter(100, 10100), _converter(101, 10100)]),
        parameter_refs=(
            '<ParameterReference PTID="100"/>'
            '<ParameterReference PTID="101"/>'
        ),
    )
    with caplog.at_level(logging.DEBUG):
        cat = load_wolf_catalog(wolf_dir)
    dev = cat.get_device("TESTDEV")
    # Only the first (catalog-order) survives.
    assert set(dev.parameters) == {100}
    assert dev.parameters[100].display_name == "Programmwahl"
    assert "Deduplicated 1 redundant parameters for device TESTDEV" in caplog.text


def test_dedup_keeps_params_with_different_telegram_nr(tmp_path: Path) -> None:
    """Same Name but different TelegramNr -> both kept, 2nd gets a suffix.

    Mirrors the real Wolf case: Programmwahl 360051@10100 reads a different
    bus telegram than 360135@9026, so both are genuine values. HA needs
    distinct entity names, so the 2nd survivor's ``display_name`` gets a
    ``" (2)"`` suffix; the technical ``name`` is left alone.
    """
    wolf_dir = _write_wolf_triple(
        tmp_path,
        parameters="\n".join([
            _numeric_param(100, "Programmwahl", app=True),
            _numeric_param(101, "Programmwahl", app=True),
        ]),
        # Different telegrams -> genuinely distinct values.
        converters="\n".join([_converter(100, 10100), _converter(101, 9026)]),
        parameter_refs=(
            '<ParameterReference PTID="100"/>'
            '<ParameterReference PTID="101"/>'
        ),
    )
    cat = load_wolf_catalog(wolf_dir)
    dev = cat.get_device("TESTDEV")
    assert set(dev.parameters) == {100, 101}
    # First keeps the bare name, second gets the disambiguation suffix.
    assert dev.parameters[100].display_name == "Programmwahl"
    assert dev.parameters[101].display_name == "Programmwahl (2)"
    # Technical names stay untouched (PTID-disambiguated by the loader).
    assert dev.parameters[100].name != dev.parameters[101].name
    assert not dev.parameters[101].name.endswith("(2)")


def test_dedup_is_per_device_not_global(tmp_path: Path) -> None:
    """The same Name+converter on two different devices is NOT deduplicated."""
    (tmp_path / "device.xml").write_text(
        dedent("""\
        <DeviceTemplateConfig>
          <DeviceTemplates>
            <DeviceTemplate DTID="10000" Name="DEV_A">
              <SoftwareNumber>0x0</SoftwareNumber>
              <WRSDeviceIds>0x0</WRSDeviceIds>
              <ParameterReferenceList>
                <ParameterReference PTID="100"/>
              </ParameterReferenceList>
            </DeviceTemplate>
            <DeviceTemplate DTID="10001" Name="DEV_B">
              <SoftwareNumber>0x0</SoftwareNumber>
              <WRSDeviceIds>0x0</WRSDeviceIds>
              <ParameterReferenceList>
                <ParameterReference PTID="100"/>
              </ParameterReferenceList>
            </DeviceTemplate>
          </DeviceTemplates>
        </DeviceTemplateConfig>
        """),
        encoding="utf-8",
    )
    (tmp_path / "parameter.xml").write_text(
        dedent(f"""\
        <ParameterTemplateConfig>
          <ParameterList>
            {_numeric_param(100, "Vorlauftemperatur", app=True)}
          </ParameterList>
        </ParameterTemplateConfig>
        """),
        encoding="utf-8",
    )
    (tmp_path / "converter.xml").write_text(
        dedent(f"""\
        <ConverterTemplateConfig>
          <ConverterTemplates>
            {_converter(100, 5)}
          </ConverterTemplates>
        </ConverterTemplateConfig>
        """),
        encoding="utf-8",
    )
    cat = load_wolf_catalog(tmp_path)
    # Both devices keep the parameter -- dedup never crosses device bounds.
    assert 100 in cat.get_device("DEV_A").parameters
    assert 100 in cat.get_device("DEV_B").parameters


def test_dedup_bundled_catalog_collapses_programmwahl() -> None:
    """Regression: the real MK_BM-2 catalog had 'Programmwahl' 10x.

    Live hardware test showed MK_BM-2 with 87 catalog rows including ten
    identically-named 'Programmwahl' entries (8 reading telegram 10100, one
    9025, one 9026). After dedup only the three distinct-telegram survivors
    remain, the 2nd/3rd carrying a disambiguation suffix.
    """
    if not WOLF_DIR.exists():
        pytest.skip("Wolf XMLs not bundled in this checkout")
    cat = load_wolf_catalog(WOLF_DIR)
    mk = cat.get_device("MK_BM-2")
    progs = [
        p.display_name for p in mk.parameters.values()
        if p.display_name.startswith("Programmwahl")
        and "AFB" not in p.display_name
        and "SmartHome" not in p.display_name
    ]
    # 10 redundant rows collapse to 3 (distinct TelegramNrs 10100/9025/9026).
    assert sorted(progs) == [
        "Programmwahl", "Programmwahl (2)", "Programmwahl (3)",
    ]
    # The whole device shrinks substantially (87 raw rows -> ~33).
    assert len(mk.parameters) < 40


# --- Curated "standard" preset (the author's ism7mqtt parameter selection) ----


def test_curated_standard_json_loads_six_devices() -> None:
    """``curated_standard.json`` parses into the 6 curated DTIDs.

    Each DTID maps to a ``set[int]`` of PTIDs of the expected size
    (see ``EXPECTED_CURATED_SIZES``).
    """
    curated = _load_curated_standard(CURATED_JSON)
    assert set(curated) == set(EXPECTED_CURATED_SIZES)
    for dtid, expected_size in EXPECTED_CURATED_SIZES.items():
        assert isinstance(curated[dtid], set)
        assert len(curated[dtid]) == expected_size
    # MK_BM-2 curates the surviving Raumtemperatur (360116), not the dead
    # duplicate 360000 (same telegram, dropped by dedup).
    assert 360116 in curated[360000]
    assert 360000 not in curated[360000]


def test_curated_standard_attached_to_catalog() -> None:
    """``load_wolf_catalog`` populates ``Catalog.curated_standard``."""
    if not WOLF_DIR.exists():
        pytest.skip("Wolf XMLs not bundled in this checkout")
    cat = load_wolf_catalog(WOLF_DIR)
    assert set(cat.curated_standard) == set(EXPECTED_CURATED_SIZES)


def test_standard_preset_for_curated_device_is_intersection() -> None:
    """``standard`` for a curated device = curated PTIDs ∩ catalog PTIDs.

    CHA (DTID 270000) -- every one of the author's 44 PTIDs exists in the
    bundled catalog, so ``standard`` is exactly those 44.
    """
    if not WOLF_DIR.exists():
        pytest.skip("Wolf XMLs not bundled in this checkout")
    cat = load_wolf_catalog(WOLF_DIR)
    cha = cat.get_device("CHA")
    assert cha.dtid == 270000
    curated_ptids = cat.curated_standard[270000]
    catalog_ptids = set(cha.parameters)
    expected = curated_ptids & catalog_ptids
    got = {p.property_id for p in cat.parameters_for_preset("CHA", "standard")}
    assert got == expected
    assert got == curated_ptids  # all 44 present for CHA
    assert len(got) == 44


def test_standard_preset_skips_curated_ptids_absent_from_catalog() -> None:
    """Curated PTIDs without a supported converter are skipped from standard.

    BM-2 (DTID 220000): the author's list contains 220032 ("Uhrzeit"), whose
    BM2Time converter type isn't supported, so the loader drops it. It must
    be silently skipped -- ``standard`` is the surviving subset.
    """
    if not WOLF_DIR.exists():
        pytest.skip("Wolf XMLs not bundled in this checkout")
    cat = load_wolf_catalog(WOLF_DIR)
    bm2 = cat.get_device("BM-2")
    assert bm2.dtid == 220000
    got = {p.property_id for p in cat.parameters_for_preset("BM-2", "standard")}
    # 220032 is curated but has no supported converter -> dropped from result.
    assert 220032 in cat.curated_standard[220000]
    assert 220032 not in got
    # Result is exactly the intersection (no crash, no phantom PTIDs).
    assert got == cat.curated_standard[220000] & set(bm2.parameters)


def test_standard_preset_fallback_for_non_curated_device() -> None:
    """A device whose DTID isn't curated uses the ``<App>`` heuristic.

    CGB-2 is a Wolf gas boiler -- not in the author's 6-device list. Its
    ``standard`` preset must still be non-empty (App-flagged parameters).
    """
    if not WOLF_DIR.exists():
        pytest.skip("Wolf XMLs not bundled in this checkout")
    cat = load_wolf_catalog(WOLF_DIR)
    cgb = cat.get_device("CGB-2")
    assert cgb.dtid not in cat.curated_standard
    standard = cat.parameters_for_preset("CGB-2", "standard")
    assert standard, "fallback standard preset must not be empty"
    # Fallback == exactly the App-flagged parameters.
    assert {p.property_id for p in standard} == {
        p.property_id for p in cgb.parameters.values() if p.app
    }


def test_preset_nesting_chain_all_bundled_devices() -> None:
    """minimal ⊆ standard ⊆ extended ⊆ all for every bundled device.

    Covers both the 6 curated devices and the ~35 fallback devices.
    """
    if not WOLF_DIR.exists():
        pytest.skip("Wolf XMLs not bundled in this checkout")
    cat = load_wolf_catalog(WOLF_DIR)
    for dev_type in cat.devices:
        mn = {p.property_id for p in cat.parameters_for_preset(dev_type, "minimal")}
        st = {p.property_id for p in cat.parameters_for_preset(dev_type, "standard")}
        ex = {p.property_id for p in cat.parameters_for_preset(dev_type, "extended")}
        al = {p.property_id for p in cat.parameters_for_preset(dev_type, "all")}
        assert mn <= st <= ex <= al, f"chain broken for {dev_type}"


def test_curated_standard_missing_file_returns_empty(tmp_path: Path) -> None:
    """A missing ``curated_standard.json`` -> empty dict, logged at WARNING."""
    missing = tmp_path / "does_not_exist.json"
    assert _load_curated_standard(missing) == {}


def test_curated_standard_malformed_json_returns_empty(tmp_path: Path) -> None:
    """Broken JSON -> empty dict, no exception."""
    bad = tmp_path / "curated_standard.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    assert _load_curated_standard(bad) == {}


def test_curated_standard_unexpected_shape_returns_empty(tmp_path: Path) -> None:
    """Valid JSON with the wrong shape -> empty dict, no exception."""
    # Top level is a list, not an object.
    list_json = tmp_path / "list.json"
    list_json.write_text("[1, 2, 3]", encoding="utf-8")
    assert _load_curated_standard(list_json) == {}
    # Object but no 'presets' key.
    no_presets = tmp_path / "no_presets.json"
    no_presets.write_text('{"foo": "bar"}', encoding="utf-8")
    assert _load_curated_standard(no_presets) == {}
    # 'presets' is not an object.
    bad_presets = tmp_path / "bad_presets.json"
    bad_presets.write_text('{"presets": [1, 2]}', encoding="utf-8")
    assert _load_curated_standard(bad_presets) == {}


def test_curated_standard_skips_bad_entries_keeps_good(tmp_path: Path) -> None:
    """Per-entry robustness: bad DTID keys / PTID values are dropped, rest kept."""
    mixed = tmp_path / "curated_standard.json"
    mixed.write_text(
        '{"presets": {'
        '"270000": [1, 2, 3], '          # good
        '"not_an_int": [4, 5], '         # bad DTID key -> skip whole entry
        '"360000": "should_be_a_list", ' # bad value type -> skip whole entry
        '"40000": [10, "bad", 12], '     # one bad PTID -> drop that PTID only
        '"220000": []'                   # empty list -> dropped (no PTIDs)
        '}}',
        encoding="utf-8",
    )
    result = _load_curated_standard(mixed)
    assert result == {270000: {1, 2, 3}, 40000: {10, 12}}


def test_catalog_robust_when_curated_file_missing(tmp_path: Path) -> None:
    """The full catalog still loads when ``curated_standard.json`` is absent.

    Copy only the wolf/ XMLs into a temp tree (no curated_standard.json
    alongside) -- ``load_wolf_catalog`` must succeed, ``curated_standard`` is
    empty, and ``standard`` falls back to the ``<App>`` heuristic for every
    device (non-empty for a real module like CHA).
    """
    if not WOLF_DIR.exists():
        pytest.skip("Wolf XMLs not bundled in this checkout")
    wolf_copy = tmp_path / "wolf"
    wolf_copy.mkdir()
    for xml in ("device.xml", "parameter.xml", "converter.xml"):
        src = WOLF_DIR / xml
        if src.exists():
            (wolf_copy / xml).write_bytes(src.read_bytes())
    cat = load_wolf_catalog(wolf_copy)
    assert cat.curated_standard == {}
    # standard still resolves via the App fallback.
    assert cat.parameters_for_preset("CHA", "standard")


# --- Curated write-bus-address map (Wolf's separate read/write addresses) ----


def test_curated_write_addresses_json_loads_known_modules() -> None:
    """``curated_bus_addresses.json`` parses into the known DTID -> write map.

    Only the 3 Wolf modules with a divergent write address are listed; the
    file maps each DTID to a hex write-address string.
    """
    write_map = _load_curated_write_addresses(CURATED_WRITE_JSON)
    assert write_map == EXPECTED_CURATED_WRITE_ADDRESSES


def test_curated_write_addresses_attached_to_catalog() -> None:
    """``load_wolf_catalog`` populates ``Catalog.curated_write_addresses``."""
    if not WOLF_DIR.exists():
        pytest.skip("Wolf XMLs not bundled in this checkout")
    cat = load_wolf_catalog(WOLF_DIR)
    assert cat.curated_write_addresses == EXPECTED_CURATED_WRITE_ADDRESSES


def test_curated_write_addresses_missing_file_returns_empty(
    tmp_path: Path,
) -> None:
    """A missing ``curated_bus_addresses.json`` -> empty dict, no exception."""
    missing = tmp_path / "does_not_exist.json"
    assert _load_curated_write_addresses(missing) == {}


def test_curated_write_addresses_malformed_json_returns_empty(
    tmp_path: Path,
) -> None:
    """Broken JSON -> empty dict, no exception."""
    bad = tmp_path / "curated_bus_addresses.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    assert _load_curated_write_addresses(bad) == {}


def test_curated_write_addresses_unexpected_shape_returns_empty(
    tmp_path: Path,
) -> None:
    """Valid JSON with the wrong shape -> empty dict, no exception."""
    # Top level is a list, not an object.
    list_json = tmp_path / "list.json"
    list_json.write_text("[1, 2, 3]", encoding="utf-8")
    assert _load_curated_write_addresses(list_json) == {}
    # Object but no 'write_offsets' key.
    no_offsets = tmp_path / "no_offsets.json"
    no_offsets.write_text('{"foo": "bar"}', encoding="utf-8")
    assert _load_curated_write_addresses(no_offsets) == {}
    # 'write_offsets' is not an object.
    bad_offsets = tmp_path / "bad_offsets.json"
    bad_offsets.write_text('{"write_offsets": [1, 2]}', encoding="utf-8")
    assert _load_curated_write_addresses(bad_offsets) == {}


def test_curated_write_addresses_skips_bad_entries_keeps_good(
    tmp_path: Path,
) -> None:
    """Per-entry robustness: bad DTID keys / non-string values are dropped."""
    mixed = tmp_path / "curated_bus_addresses.json"
    mixed.write_text(
        '{"write_offsets": {'
        '"270000": "0x3", '          # good
        '"not_an_int": "0x9", '      # bad DTID key -> skip
        '"220000": 48, '            # non-string value -> skip
        '"350000": "  ", '          # empty/blank string -> skip
        '"40000": "0x51"'           # good
        '}}',
        encoding="utf-8",
    )
    result = _load_curated_write_addresses(mixed)
    assert result == {270000: "0x3", 40000: "0x51"}


def test_catalog_robust_when_curated_write_file_missing(tmp_path: Path) -> None:
    """The full catalog still loads when ``curated_bus_addresses.json`` is gone.

    Copy only the wolf/ XMLs into a temp tree -- ``load_wolf_catalog`` must
    succeed and ``curated_write_addresses`` is empty (write defaults to read
    for every module).
    """
    if not WOLF_DIR.exists():
        pytest.skip("Wolf XMLs not bundled in this checkout")
    wolf_copy = tmp_path / "wolf"
    wolf_copy.mkdir()
    for xml in ("device.xml", "parameter.xml", "converter.xml"):
        src = WOLF_DIR / xml
        if src.exists():
            (wolf_copy / xml).write_bytes(src.read_bytes())
    cat = load_wolf_catalog(wolf_copy)
    assert cat.curated_write_addresses == {}


def _bounded_param(ptid: int, name: str, lo: int, hi: int) -> str:
    return (
        f'<NumericParameterDescriptor PTID="{ptid}">'
        f"<Name>{name}</Name><UnitName>&#176;C</UnitName>"
        f"<ReadOnlyConditionId>False</ReadOnlyConditionId>"
        f"<MinValueCondition>{lo}</MinValueCondition>"
        f"<MaxValueCondition>{hi}</MaxValueCondition>"
        f"<Decimals>0</Decimals>"
        f"</NumericParameterDescriptor>"
    )


def _write_variant_pair(tmp_path: Path) -> Path:
    """Two rows reading the same telegram with different bounds (MM MI14)."""
    wolf_dir = tmp_path / "wolf"
    wolf_dir.mkdir()
    return _write_wolf_triple(
        wolf_dir,
        parameters="\n".join([
            _bounded_param(100, "Konstanttemperatur", 50, 80),
            _bounded_param(101, "Konstanttemperatur", 20, 95),
        ]),
        converters="\n".join([_converter(100, 7000), _converter(101, 7000)]),
        parameter_refs=(
            '<ParameterReference PTID="100"/>'
            '<ParameterReference PTID="101"/>'
        ),
    )


def test_curated_range_takes_bounds_from_dropped_variant(tmp_path: Path) -> None:
    """The kept PTID stays, but its bounds come from the named variant.

    The PTID must not change -- it is the entity unique_id and the stored
    subscription -- only the codec (min/max) is taken from the variant.
    """
    wolf_dir = _write_variant_pair(tmp_path)
    (tmp_path / "curated_ranges.json").write_text(
        '{"range_variants": {"10000": {"100": 101}}}', encoding="utf-8",
    )
    cat = load_wolf_catalog(wolf_dir, curated_dir=tmp_path)
    dev = cat.get_device("TESTDEV")
    assert set(dev.parameters) == {100}
    codec = dev.parameters[100].codec
    assert (codec.min_value, codec.max_value) == (20, 95)


def test_without_curated_range_first_variant_bounds_stay(tmp_path: Path) -> None:
    """No curated entry -> unchanged behaviour (first row's bounds)."""
    wolf_dir = _write_variant_pair(tmp_path)
    cat = load_wolf_catalog(wolf_dir, curated_dir=tmp_path)
    codec = cat.get_device("TESTDEV").parameters[100].codec
    assert (codec.min_value, codec.max_value) == (50, 80)


def test_curated_range_ignores_variant_of_other_value(
    tmp_path: Path, caplog,
) -> None:
    """A source PTID that reads a different telegram is not a variant."""
    wolf_dir = tmp_path / "wolf"
    wolf_dir.mkdir()
    _write_wolf_triple(
        wolf_dir,
        parameters="\n".join([
            _bounded_param(100, "Konstanttemperatur", 50, 80),
            _bounded_param(101, "Konstanttemperatur", 20, 95),
        ]),
        converters="\n".join([_converter(100, 7000), _converter(101, 7001)]),
        parameter_refs=(
            '<ParameterReference PTID="100"/>'
            '<ParameterReference PTID="101"/>'
        ),
    )
    (tmp_path / "curated_ranges.json").write_text(
        '{"range_variants": {"10000": {"100": 101}}}', encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        cat = load_wolf_catalog(wolf_dir, curated_dir=tmp_path)
    codec = cat.get_device("TESTDEV").parameters[100].codec
    assert (codec.min_value, codec.max_value) == (50, 80)
    assert "not a variant" in caplog.text


def test_curated_ranges_missing_or_broken_returns_empty(tmp_path: Path) -> None:
    """Missing file, broken JSON or wrong shape -> empty mapping."""
    assert _load_curated_ranges(tmp_path / "missing.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{ nope", encoding="utf-8")
    assert _load_curated_ranges(bad) == {}
    shape = tmp_path / "shape.json"
    shape.write_text('{"range_variants": [1]}', encoding="utf-8")
    assert _load_curated_ranges(shape) == {}
    mixed = tmp_path / "mixed.json"
    mixed.write_text(
        '{"range_variants": {"x": {"1": 2}, "40000": {"40018": 40065, "y": 1}}}',
        encoding="utf-8",
    )
    assert _load_curated_ranges(mixed) == {40000: {40018: 40065}}


def test_bundled_mm_konstanttemperatur_allows_20_to_95() -> None:
    """MM MI14 Konstanttemperatur: BM-2 accepts 20 degC, not only 50.

    Verified on a real installation (2026-10-25): set to 46 degC on the
    BM-2 expert level, Kesselsoll followed. The first catalog row (40018)
    says 50..80, the variant 40065 says 20..95.
    """
    if not WOLF_DIR.exists():
        pytest.skip("Wolf XMLs not bundled in this checkout")
    cat = load_wolf_catalog(WOLF_DIR)
    param = cat.get_device("MM").parameters[40018]
    assert (param.codec.min_value, param.codec.max_value) == (20, 95)
    assert 40065 not in cat.get_device("MM").parameters
