"""Tests for the auto-discovery layer."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.ecotherm_wolf.definitions.catalog import (
    Catalog,
    DeviceDefinition,
    ParameterDefinition,
)
from custom_components.ecotherm_wolf.definitions.codecs import Int16Codec
from custom_components.ecotherm_wolf.definitions.converters import (
    NumericConverter16,
)
from custom_components.ecotherm_wolf.discovery import (
    DiscoveredDevice,
    _map_response,
    _probe_inforead,
    discover_devices,
    probe_submodules,
)
from custom_components.ecotherm_wolf.ism7.protocol import (
    DiscoveredBusDevice,
    InfonumberReadResp,
    SystemConfigResp,
    TelegramBundleResp,
)


def _make_catalog(devs: list[DeviceDefinition]) -> Catalog:
    return Catalog(devices={d.device_type: d for d in devs})


class TestMapResponse:
    def test_dtid_match_via_wrs_id(self):
        catalog = _make_catalog([
            DeviceDefinition(
                device_type="CHA",
                display_name="Wärmepumpe",
                dtid=270000,
                wrs_device_id="0x29",
            ),
            DeviceDefinition(
                device_type="BM-2",
                display_name="Bedienmodul",
                dtid=220000,
                wrs_device_id="0x20",
            ),
        ])
        bus_devices = [
            DiscoveredBusDevice(
                bus_address="0x29",
                device_id_hex="0x29",
            ),
            DiscoveredBusDevice(
                bus_address="0x35",
                device_id_hex="0x20",
            ),
        ]
        result = _map_response(bus_devices, catalog)
        assert len(result) == 2
        cha, bm2 = result
        assert cha.device_type == "CHA"
        assert cha.dtid == 270000
        assert "Wärmepumpe" in cha.friendly_name
        assert cha.bus_address == "0x29"
        assert bm2.device_type == "BM-2"
        assert bm2.dtid == 220000
        assert bm2.bus_address == "0x35"

    def test_unknown_did_falls_back_to_placeholder(self):
        catalog = _make_catalog([
            DeviceDefinition(
                device_type="CHA",
                display_name="Wärmepumpe",
                dtid=270000,
                wrs_device_id="0x29",
            ),
        ])
        bus_devices = [
            DiscoveredBusDevice(
                bus_address="0x99",
                device_id_hex="0x99",
            ),
        ]
        result = _map_response(bus_devices, catalog)
        assert len(result) == 1
        d = result[0]
        assert d.dtid == 0
        assert d.device_type.startswith("unknown_dtid_")
        assert "DID 0x99" in d.friendly_name

    def test_multiple_templates_share_wrs_id_disambiguates_by_software(self):
        """When two templates share a wrs_device_id, the software_number
        attribute on the bus device is used as a tie-breaker. Reproduce
        zivillian's ``major*100 + minor`` formula: sv=F0, sr=01 ->
        major=240, minor=1, number=24001 -> 0x5DC1.
        """
        catalog = _make_catalog([
            DeviceDefinition(
                device_type="VariantA",
                display_name="A",
                dtid=111000,
                wrs_device_id="0x20",
                software_number="0x100",
            ),
            DeviceDefinition(
                device_type="VariantB",
                display_name="B",
                dtid=222000,
                wrs_device_id="0x20",
                software_number="0x5DC1",
            ),
        ])
        bus_devices = [
            DiscoveredBusDevice(
                bus_address="0x35",
                device_id_hex="0x20",
                software_version_hex="F0",
                software_revision_hex="01",
            ),
        ]
        result = _map_response(bus_devices, catalog)
        assert len(result) == 1
        assert result[0].device_type == "VariantB"
        assert result[0].dtid == 222000

    def test_multiple_templates_no_software_match_flags_ambiguous(self):
        """If software_number doesn't match any candidate at a shared WRS-id,
        flag the row as ambiguous so the config flow can ask the user.

        Documenting: previously we'd silently pick the first candidate. Live
        Wolf catalogs have several variants under the same WRS-id (especially
        BM-2 / SM1 / CWL); silent-pick-first masked real catalog mismatches.
        """
        catalog = _make_catalog([
            DeviceDefinition(
                device_type="VariantA",
                display_name="A",
                dtid=111000,
                wrs_device_id="0x20",
                software_number="0x100",
            ),
            DeviceDefinition(
                device_type="VariantB",
                display_name="B",
                dtid=222000,
                wrs_device_id="0x20",
                software_number="0x200",
            ),
        ])
        bus_devices = [
            DiscoveredBusDevice(
                bus_address="0x35",
                device_id_hex="0x20",
                software_version_hex="FF",
                software_revision_hex="FF",
            ),
        ]
        result = _map_response(bus_devices, catalog)
        assert len(result) == 1
        # First registered candidate is offered as best guess but the row is
        # marked ambiguous; candidates contain both variants.
        assert result[0].device_type == "ambiguous_dtid_0x20"
        assert "VariantA" in result[0].candidates
        assert "VariantB" in result[0].candidates

    @staticmethod
    def _did_0x15_catalog():
        """SM1 at WRS-id 0x15 plus two non-WRS-indexable CWL templates."""
        return _make_catalog([
            DeviceDefinition(
                device_type="SM1",
                display_name="Solar Module 1",
                dtid=60000,
                wrs_device_id="0x15",
                software_number="0xE3",
            ),
            DeviceDefinition(
                device_type="CWL",
                display_name="Comfort Wohnungslüftung",
                dtid=230000,
                wrs_device_id="0x0",  # not WRS-indexable
                software_number="0x0",
            ),
            DeviceDefinition(
                device_type="CWL-2",
                display_name="CWL-2",
                dtid=390000,
                wrs_device_id="0x0",
                software_number="0x0",
            ),
        ])

    def test_cwl_at_did_0x15_with_blanked_firmware_defaults_to_cwl(self):
        """Live Wolf scenario: a CWL reports as DID=0x15 (the SM1 WRS-id)
        with its firmware blanked to 0xFF/0xFF. The catalog can't
        auto-resolve to CWL (CWL has WRSDeviceIds=0x0), so the row stays
        ambiguous -- but the blanked-firmware signature is a reliable CWL
        tell, so CWL is promoted to the front of the candidates and the
        resolve-step's dropdown defaults to it instead of SM1.
        """
        catalog = self._did_0x15_catalog()
        bus_devices = [
            DiscoveredBusDevice(
                bus_address="0x7C",
                device_id_hex="0x15",
                software_version_hex="FF",
                software_revision_hex="FF",
            ),
        ]
        result = _map_response(bus_devices, catalog)
        assert len(result) == 1
        assert result[0].device_type == "ambiguous_dtid_0x15"
        # SM1 stays a candidate (the user can still pick it), but CWL is
        # promoted to the front so the dropdown defaults to it.
        assert "SM1" in result[0].candidates
        assert "CWL-2" in result[0].candidates
        assert result[0].candidates[0] == "CWL"
        assert "CWL" in result[0].friendly_name

    def test_did_0x15_with_real_firmware_keeps_sm1_first(self):
        """The CWL heuristic is narrow: an ambiguous DID-0x15 device that
        DOES report a real firmware (not 0xFF/0xFF) is not assumed to be a
        CWL -- SM1 stays the first candidate / dropdown default.
        """
        catalog = self._did_0x15_catalog()
        bus_devices = [
            DiscoveredBusDevice(
                bus_address="0x7C",
                device_id_hex="0x15",
                software_version_hex="02",  # real firmware -> not a CWL
                software_revision_hex="05",
            ),
        ]
        result = _map_response(bus_devices, catalog)
        assert len(result) == 1
        assert result[0].device_type == "ambiguous_dtid_0x15"
        assert result[0].candidates[0] == "SM1"

    def test_cha_with_real_software_number_is_not_ambiguous(self):
        """Live scenario: DID=0x29 + software_number=0xCD60 (sv=1, sr=0x60)
        is the actual CHA report. SM1's SoftwareNumber=0xE3 wouldn't match,
        but the catalog's CHA template has SoftwareNumber=0x0 (no constraint)
        -- so the single-candidate match is still considered confident.
        """
        catalog = _make_catalog([
            DeviceDefinition(
                device_type="CHA",
                display_name="Wärmepumpe",
                dtid=270000,
                wrs_device_id="0x29",
                software_number="0x0",  # no constraint
            ),
        ])
        bus_devices = [
            DiscoveredBusDevice(
                bus_address="0x29",
                device_id_hex="0x29",
                software_version_hex="01",
                software_revision_hex="60",
            ),
        ]
        result = _map_response(bus_devices, catalog)
        assert len(result) == 1
        assert result[0].device_type == "CHA"
        assert result[0].candidates == []

    def test_empty_catalog_all_unknown(self):
        catalog = _make_catalog([])
        bus_devices = [
            DiscoveredBusDevice(
                bus_address="0x29",
                device_id_hex="0x29",
            ),
        ]
        result = _map_response(bus_devices, catalog)
        assert len(result) == 1
        assert result[0].dtid == 0

    def test_empty_bus_list(self):
        catalog = _make_catalog([
            DeviceDefinition(
                device_type="CHA",
                display_name="Wärmepumpe",
                dtid=270000,
                wrs_device_id="0x29",
            ),
        ])
        result = _map_response([], catalog)
        assert result == []

    def test_firmware_version_is_formatted(self):
        catalog = _make_catalog([
            DeviceDefinition(
                device_type="CHA",
                display_name="Wärmepumpe",
                dtid=270000,
                wrs_device_id="0x29",
            ),
        ])
        bus_devices = [
            DiscoveredBusDevice(
                bus_address="0x29",
                device_id_hex="0x29",
                software_version_hex="02",
                software_revision_hex="0A",
            ),
        ]
        result = _map_response(bus_devices, catalog)
        # major=2, minor=0x0A=10 -> "2.10"
        assert result[0].firmware_version == "2.10"


class TestDiscoveryWithMockClient:
    @pytest.mark.asyncio
    async def test_discover_devices_calls_request_system_config(self):
        catalog = _make_catalog([
            DeviceDefinition(
                device_type="CHA",
                display_name="Wärmepumpe",
                dtid=270000,
                wrs_device_id="0x29",
            ),
        ])
        client = AsyncMock()
        client.request_system_config.return_value = SystemConfigResp(
            sid="x",
            bus_type="ebus",
            devices=[
                DiscoveredBusDevice(
                    bus_address="0x29",
                    device_id_hex="0x29",
                ),
            ],
        )
        result = await discover_devices(client, catalog, timeout=1.0)
        # Hardware test confirmed the ISM7 closes the TCP connection on an
        # empty ``sid``; we always send "1" (zivillians TelegramBundleReq
        # hardcodes the same gateway-id).
        client.request_system_config.assert_awaited_once_with(
            sid="1", timeout=1.0,
        )
        assert len(result) == 1
        assert result[0].device_type == "CHA"


def test_discover_devices_is_marked_implemented():
    """Config flow checks this attribute to decide whether to probe."""
    assert getattr(discover_devices, "implemented", False) is True


def test_discovered_device_back_compat_device_id_property():
    """The pre-discovery code path used ``DiscoveredDevice.device_id``
    (a single int). The new shape exposes ``dtid``; the alias keeps the
    older accessor working."""
    d = DiscoveredDevice(
        bus_address="0x29",
        device_type="CHA",
        dtid=270000,
        friendly_name="CHA",
    )
    assert d.device_id == 270000


def _sub_device(
    device_type: str,
    dtid: int,
    *,
    telegram_nr: int = 5,
    service: int | None = None,
    with_converter: bool = True,
) -> DeviceDefinition:
    """A catalog device with one probe-able parameter (for probe_submodules)."""
    converter = None
    if with_converter:
        converter = NumericConverter16(
            dtid, telegram_nr=telegram_nr, type_code="US",
        )
        converter.service_read_number = service
    return DeviceDefinition(
        device_type=device_type,
        display_name=device_type,
        dtid=dtid,
        parameters={
            dtid: ParameterDefinition(
                property_id=dtid,
                name=device_type.lower().replace("-", "_"),
                display_name=device_type,
                codec=Int16Codec(),
                info_numbers=[telegram_nr],
                converter=converter,
            ),
        },
    )


def _ok_resp() -> TelegramBundleResp:
    """A probe response where the eBus device answered -> module present."""
    return TelegramBundleResp(
        bundle_id="probe", state="ok",
        read_telegrams=[InfonumberReadResp(
            info_number=5, bus_address="0x35", state="ok",
            db_low="00", db_high="00",
        )],
    )


def _timeout_resp() -> TelegramBundleResp:
    """A probe response where the eBus device stayed silent -> module absent."""
    return TelegramBundleResp(
        bundle_id="probe", state="ok",
        read_telegrams=[InfonumberReadResp(
            info_number=5, bus_address="0x85", state="to",
            db_low="", db_high="",
        )],
    )


class TestProbeSubmodules:
    """probe_submodules checks the bus for logical sub-modules.

    Curated map: BM-2 (220000) -> WWSystem_BM-2 (350000, parent bus),
    MK_BM-2 (360000, 0x85), ZirkSol_BM-2 (260000, parent bus).
    """

    @staticmethod
    def _catalog() -> Catalog:
        return _make_catalog([
            DeviceDefinition(
                device_type="BM-2", display_name="BM-2",
                dtid=220000, wrs_device_id="0x20",
            ),
            _sub_device("WWSystem_BM-2", 350000),
            _sub_device("MK_BM-2", 360000),
            _sub_device("ZirkSol_BM-2", 260000, service=4070),
        ])

    # Parents are passed as the raw DiscoveredDevice list -- probe runs on
    # the discovery connection, before ambiguous resolution.
    @staticmethod
    def _bm2_discovered() -> list[DiscoveredDevice]:
        return [DiscoveredDevice(
            bus_address="0x35", device_type="BM-2", dtid=220000,
            friendly_name="BM-2",
        )]

    @pytest.mark.asyncio
    async def test_present_and_absent_submodules(self):
        """st=ok -> present (pre-checked); st=to -> absent (offered unchecked)."""
        client = AsyncMock()
        # 3 candidates probed sequentially: WWSystem (ok), MK (timeout),
        # ZirkSol (ok).
        client.request = AsyncMock(side_effect=[
            _ok_resp(), _timeout_resp(), _ok_resp(),
        ])
        result = await probe_submodules(
            client, self._catalog(), self._bm2_discovered(),
        )

        assert [c.device_type for c in result] == [
            "WWSystem_BM-2", "MK_BM-2", "ZirkSol_BM-2",
        ]
        by_type = {c.device_type: c for c in result}
        # WWSystem_BM-2: "parent" bus rule -> probed at the BM-2's 0x35.
        assert by_type["WWSystem_BM-2"].bus_address == "0x35"
        assert by_type["WWSystem_BM-2"].present is True
        # MK_BM-2: explicit 0x85, no eBus answer -> absent.
        assert by_type["MK_BM-2"].bus_address == "0x85"
        assert by_type["MK_BM-2"].present is False
        # ZirkSol_BM-2: "parent" bus rule -> 0x35, answered -> present.
        assert by_type["ZirkSol_BM-2"].bus_address == "0x35"
        assert by_type["ZirkSol_BM-2"].present is True
        # parent identity carried for the flow's per-parent filtering.
        assert all(c.parent_device_type == "BM-2" for c in result)
        assert all(c.parent_bus_address == "0x35" for c in result)

    @pytest.mark.asyncio
    async def test_ambiguous_parent_probed_via_best_guess(self):
        """An ambiguous parent (still unresolved during discovery) is probed
        via its WRS-id best guess -- this is what lets the probe run on the
        discovery connection instead of needing a second one after resolve.
        """
        client = AsyncMock()
        client.request = AsyncMock(side_effect=[_ok_resp(), _ok_resp(), _ok_resp()])
        discovered = [DiscoveredDevice(
            bus_address="0x35",
            device_type="ambiguous_dtid_0x20",  # not yet resolved
            dtid=0,
            friendly_name="Mehrdeutig",
            candidates=["BM-2"],                 # best guess
        )]
        result = await probe_submodules(client, self._catalog(), discovered)
        # Best guess BM-2 -> its sub-modules get probed despite dtid=0.
        assert {c.device_type for c in result} == {
            "WWSystem_BM-2", "MK_BM-2", "ZirkSol_BM-2",
        }

    @pytest.mark.asyncio
    async def test_unknown_parent_yields_no_candidates(self):
        """A parent with no curated sub-modules -> empty result, no probe."""
        client = AsyncMock()
        client.request = AsyncMock()
        result = await probe_submodules(
            client, self._catalog(),
            [DiscoveredDevice(
                bus_address="0x29", device_type="CHA", dtid=270000,
                friendly_name="CHA",
            )],
        )
        assert result == []
        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probe_error_is_treated_as_absent(self):
        """A probe that raises must not crash discovery -- the candidate is
        just offered as not-present (unchecked suggestion)."""
        client = AsyncMock()
        client.request = AsyncMock(side_effect=TimeoutError("no ack"))
        result = await probe_submodules(
            client, self._catalog(), self._bm2_discovered(),
        )
        assert len(result) == 3
        assert all(c.present is False for c in result)

    def test_probe_inforead_carries_service_number(self):
        """_probe_inforead picks the first converter-backed parameter and
        carries its ServiceReadNumber as snr (else -1)."""
        with_snr = _sub_device(
            "ZirkSol_BM-2", 260000, telegram_nr=7, service=4070,
        )
        ir = _probe_inforead(with_snr, "0x35")
        assert ir is not None
        assert ir.info_number == 7
        assert ir.bus_address == "0x35"
        assert ir.service_number == 4070
        # No service number -> snr omitted (-1).
        ir2 = _probe_inforead(_sub_device("MK_BM-2", 360000, telegram_nr=3), "0x85")
        assert ir2 is not None
        assert ir2.service_number == -1

    def test_probe_inforead_none_when_no_converter(self):
        """A sub-module with no converter-backed parameter can't be probed."""
        no_conv = _sub_device("X", 999, with_converter=False)
        assert _probe_inforead(no_conv, "0x35") is None
