from __future__ import annotations

import pytest

from custom_components.ecotherm_wolf.definitions.catalog import (
    Catalog,
    DeviceDefinition,
    ParameterDefinition,
    UnknownDeviceError,
    UnknownParameterError,
)
from custom_components.ecotherm_wolf.definitions.codecs import (
    EnumCodec,
    Int16Codec,
    ScaledCodec,
)


def make_chai_definition() -> DeviceDefinition:
    return DeviceDefinition(
        device_type="CHA-10",
        display_name="CHA-10/400V",
        parameters={
            # Read-only scaled sensor, in Wolf's App set -> minimal/standard.
            0x0080: ParameterDefinition(
                property_id=0x0080,
                name="vorlauftemp",
                display_name="Vorlauftemperatur",
                codec=ScaledCodec(base=Int16Codec(unit="°C"), scale=10),
                preset="standard",
                app=True,
            ),
            # Writable enum (select), in Wolf's App set -> standard/extended,
            # but NOT minimal (minimal is read-only sensors only).
            0x00A1: ParameterDefinition(
                property_id=0x00A1,
                name="betriebsart",
                display_name="Betriebsart",
                codec=EnumCodec(
                    members={0: "Aus", 1: "Auto", 2: "Heizen"},
                    writable=True,
                ),
                preset="standard",
                app=True,
            ),
        },
    )


class TestCatalog:
    def test_lookup_device_by_type(self):
        cat = Catalog(devices={"CHA-10": make_chai_definition()})
        assert cat.get_device("CHA-10").display_name == "CHA-10/400V"

    def test_lookup_unknown_device_raises(self):
        cat = Catalog(devices={})
        with pytest.raises(UnknownDeviceError):
            cat.get_device("NONEXISTENT")

    def test_lookup_parameter(self):
        cat = Catalog(devices={"CHA-10": make_chai_definition()})
        param = cat.get_parameter("CHA-10", 0x0080)
        assert param.name == "vorlauftemp"

    def test_lookup_unknown_parameter_raises(self):
        cat = Catalog(devices={"CHA-10": make_chai_definition()})
        with pytest.raises(UnknownParameterError):
            cat.get_parameter("CHA-10", 0xDEAD)

    def test_preset_all_returns_everything(self):
        cat = Catalog(devices={"CHA-10": make_chai_definition()})
        params = cat.parameters_for_preset("CHA-10", "all")
        assert len(params) == 2

    def test_preset_standard_is_app_flagged(self):
        """``standard`` = parameters carrying Wolf's ``<App>true</App>`` flag."""
        cat = Catalog(devices={"CHA-10": make_chai_definition()})
        params = cat.parameters_for_preset("CHA-10", "standard")
        # Both fixture params have app=True.
        assert {p.property_id for p in params} == {0x0080, 0x00A1}

    def test_preset_standard_excludes_non_app(self):
        """A parameter without the App flag is dropped from ``standard``."""
        dev = make_chai_definition()
        dev.parameters[0x00A1].app = False
        cat = Catalog(devices={"CHA-10": dev})
        params = cat.parameters_for_preset("CHA-10", "standard")
        assert {p.property_id for p in params} == {0x0080}

    def test_preset_extended_adds_writable(self):
        """``extended`` = ``standard`` plus every writable parameter."""
        dev = make_chai_definition()
        # Drop the App flag on the writable enum: it must still surface in
        # ``extended`` (because it's writable) but not in ``standard``.
        dev.parameters[0x00A1].app = False
        cat = Catalog(devices={"CHA-10": dev})
        assert {p.property_id for p in cat.parameters_for_preset("CHA-10", "standard")} == {
            0x0080
        }
        assert {p.property_id for p in cat.parameters_for_preset("CHA-10", "extended")} == {
            0x0080,
            0x00A1,
        }

    def test_preset_minimal_is_readonly_sensors(self):
        """``minimal`` = the read-only display sensors within ``standard``."""
        cat = Catalog(devices={"CHA-10": make_chai_definition()})
        params = cat.parameters_for_preset("CHA-10", "minimal")
        # Only the read-only ScaledCodec sensor; the writable enum is excluded.
        assert {p.property_id for p in params} == {0x0080}

    def test_preset_nesting_chain(self):
        """minimal ⊆ standard ⊆ extended ⊆ all for every device."""
        cat = Catalog(devices={"CHA-10": make_chai_definition()})
        for dev_type in cat.devices:
            mn = {p.property_id for p in cat.parameters_for_preset(dev_type, "minimal")}
            st = {p.property_id for p in cat.parameters_for_preset(dev_type, "standard")}
            ex = {p.property_id for p in cat.parameters_for_preset(dev_type, "extended")}
            al = {p.property_id for p in cat.parameters_for_preset(dev_type, "all")}
            assert mn <= st <= ex <= al

    def test_standard_uses_curated_list_for_known_dtid(self):
        """When the device's DTID is curated, ``standard`` = that PTID list.

        The fixture device has DTID 0; give the catalog a curated mapping for
        DTID 0 that picks *only* the read-only sensor (0x0080). The App-flagged
        writable enum (0x00A1) must NOT appear -- the curated list wins over
        the ``<App>`` heuristic.
        """
        cat = Catalog(
            devices={"CHA-10": make_chai_definition()},
            curated_standard={0: {0x0080}},
        )
        params = cat.parameters_for_preset("CHA-10", "standard")
        assert {p.property_id for p in params} == {0x0080}

    def test_standard_curated_skips_missing_ptids(self):
        """Curated PTIDs absent from the catalog are skipped, not fatal.

        0xDEAD never exists; 0x0080 does. The result is the intersection.
        """
        cat = Catalog(
            devices={"CHA-10": make_chai_definition()},
            curated_standard={0: {0x0080, 0xDEAD}},
        )
        params = cat.parameters_for_preset("CHA-10", "standard")
        assert {p.property_id for p in params} == {0x0080}

    def test_standard_falls_back_to_app_for_non_curated_dtid(self):
        """A DTID absent from ``curated_standard`` uses the ``<App>`` heuristic."""
        cat = Catalog(
            devices={"CHA-10": make_chai_definition()},
            # DTID 999 is curated, but the fixture device has DTID 0 -> fallback.
            curated_standard={999: {0x1234}},
        )
        params = cat.parameters_for_preset("CHA-10", "standard")
        # Both fixture params carry app=True -> full App fallback set.
        assert {p.property_id for p in params} == {0x0080, 0x00A1}

    def test_extended_is_superset_of_curated_standard(self):
        """``extended`` stays a superset of ``standard`` even when curated.

        Curated ``standard`` picks only 0x0080. ``extended`` must still include
        it *and* the writable enum 0x00A1.
        """
        cat = Catalog(
            devices={"CHA-10": make_chai_definition()},
            curated_standard={0: {0x0080}},
        )
        st = {p.property_id for p in cat.parameters_for_preset("CHA-10", "standard")}
        ex = {p.property_id for p in cat.parameters_for_preset("CHA-10", "extended")}
        assert st == {0x0080}
        assert st <= ex
        assert 0x00A1 in ex

    def test_preset_nesting_chain_with_curated(self):
        """minimal ⊆ standard ⊆ extended ⊆ all holds with a curated mapping."""
        cat = Catalog(
            devices={"CHA-10": make_chai_definition()},
            curated_standard={0: {0x0080}},
        )
        for dev_type in cat.devices:
            mn = {p.property_id for p in cat.parameters_for_preset(dev_type, "minimal")}
            st = {p.property_id for p in cat.parameters_for_preset(dev_type, "standard")}
            ex = {p.property_id for p in cat.parameters_for_preset(dev_type, "extended")}
            al = {p.property_id for p in cat.parameters_for_preset(dev_type, "all")}
            assert mn <= st <= ex <= al

    def test_preset_returns_sorted_deterministic(self):
        """Every preset yields a property_id-sorted list."""
        cat = Catalog(devices={"CHA-10": make_chai_definition()})
        for preset in ("minimal", "standard", "extended", "all"):
            ids = [p.property_id for p in cat.parameters_for_preset("CHA-10", preset)]
            assert ids == sorted(ids)

    def test_filter_by_unknown_preset_uses_preset_attr_fallback(self):
        """An unknown preset falls back to the explicit ``preset`` attribute.

        Hand-written override XMLs may still tag parameters with a ``preset``
        string; an unrecognised preset name matches those exactly (and yields
        ``[]`` when nothing is tagged).
        """
        cat = Catalog(devices={"CHA-10": make_chai_definition()})
        # Fixture params carry preset="standard" -- "extreme" matches none.
        assert cat.parameters_for_preset("CHA-10", "extreme") == []
        # ...but the literal attribute value still resolves.
        params = cat.parameters_for_preset("CHA-10", "standard")
        assert len(params) == 2

    def test_diff_returns_added_and_removed(self):
        old = Catalog(devices={"CHA-10": make_chai_definition()})
        new_def = make_chai_definition()
        new_def.parameters[0x010C] = ParameterDefinition(
            property_id=0x010C,
            name="sollwert_tag",
            display_name="Sollwert Tag",
            codec=Int16Codec(min_value=5, max_value=30, unit="°C"),
            preset="standard",
        )
        new = Catalog(devices={"CHA-10": new_def})
        diff = old.diff(new)
        assert ("CHA-10", 0x010C, "sollwert_tag") in diff.added
        assert diff.removed == []

    def test_diff_detects_removed(self):
        """A parameter present in old but not new is reported as removed."""
        new_def = make_chai_definition()
        del new_def.parameters[0x00A1]
        old = Catalog(devices={"CHA-10": make_chai_definition()})
        new = Catalog(devices={"CHA-10": new_def})
        diff = old.diff(new)
        assert ("CHA-10", 0x00A1, "betriebsart") in diff.removed
        assert diff.added == []

    def test_diff_detects_changed_codec_type(self):
        """A parameter that changes codec type is detected."""
        old_def = make_chai_definition()
        new_def = make_chai_definition()
        new_def.parameters[0x0080] = ParameterDefinition(
            property_id=0x0080,
            name="vorlauftemp",
            display_name="Vorlauftemperatur",
            codec=Int16Codec(unit="°C"),  # was ScaledCodec
            preset="standard",
        )
        diff = Catalog(devices={"CHA-10": old_def}).diff(
            Catalog(devices={"CHA-10": new_def})
        )
        assert ("CHA-10", 0x0080, "vorlauftemp") in diff.changed

    def test_diff_detects_changed_display_name(self):
        """A parameter that changes display_name is detected."""
        old_def = make_chai_definition()
        new_def = make_chai_definition()
        new_def.parameters[0x0080].display_name = "Vorlauftemperatur (renamed)"
        diff = Catalog(devices={"CHA-10": old_def}).diff(
            Catalog(devices={"CHA-10": new_def})
        )
        assert ("CHA-10", 0x0080, "vorlauftemp") in diff.changed
