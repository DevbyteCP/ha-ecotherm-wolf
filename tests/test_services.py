"""Tests for browse_property, list_properties, rediscover_devices services."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.ecotherm_wolf.const import (
    CONF_DEVICES,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    DOMAIN,
)
from custom_components.ecotherm_wolf.ism7.protocol import (
    InfonumberReadResp,
    TelegramBundleResp,
)

# Needs Wolf's XMLs/certificate (scripts/fetch_wolf_files.py --bundled).
pytestmark = pytest.mark.requires_wolf_files


async def _setup_entry(hass: HomeAssistant) -> tuple[MockConfigEntry, MagicMock]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: 9092,
            CONF_PASSWORD: "ism7",
            CONF_DEVICES: {"1": ["CHA-10", "0;0;0;1"]},
        },
        options={"selected_parameters": []},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ecotherm_wolf.create_client"
    ) as factory:
        client = MagicMock()
        client.connected = True
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.send = AsyncMock()
        client.request = AsyncMock()
        client.on_update = MagicMock()
        client.on_reconnect = MagicMock()
        factory.return_value = client

        with patch(
            "custom_components.ecotherm_wolf.BUNDLED_DEFINITIONS_DIR",
            Path(__file__).parent / "definitions" / "fixtures" / "devices",
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
    return entry, client


@pytest.mark.asyncio
async def test_browse_property_returns_value(hass: HomeAssistant) -> None:
    entry, client = await _setup_entry(hass)
    # vorlauftemp 0x0080, dh="00" + dl="F5" -> 0x00F5 = 245, scale=10 -> 24.5
    client.request = AsyncMock(
        return_value=TelegramBundleResp(
            bundle_id="browse_1", state="ok",
            read_telegrams=[InfonumberReadResp(
                info_number=0x0080, bus_address="0;0;0;1",
                state="ok", db_low="F5", db_high="00",
            )],
        )
    )

    result = await hass.services.async_call(
        DOMAIN,
        "browse_property",
        {"device": "1", "property": "vorlauftemp"},
        blocking=True,
        return_response=True,
    )

    assert result is not None
    assert result["device_id"] == 1
    assert result["property_id"] == "0x0080"
    assert result["name"] == "vorlauftemp"
    assert result["current_value"] == 24.5


@pytest.mark.asyncio
async def test_list_properties_returns_all(hass: HomeAssistant) -> None:
    entry, client = await _setup_entry(hass)
    result = await hass.services.async_call(
        DOMAIN,
        "list_properties",
        {},
        blocking=True,
        return_response=True,
    )
    # The CHA-10 fixture has 3 parameters.
    assert result is not None
    assert "properties" in result
    assert len(result["properties"]) == 3


@pytest.mark.asyncio
async def test_reload_definitions_returns_diff(hass: HomeAssistant) -> None:
    """reload_definitions reads the bundled XMLs and returns a diff."""
    entry, client = await _setup_entry(hass)

    # The fixture catalog is loaded at setup. Reload should return zero
    # added/removed since we're loading the same bundle again (the
    # BUNDLED_DEFINITIONS_DIR patch in _setup_entry redirects both reads).
    with patch(
        "custom_components.ecotherm_wolf.BUNDLED_DEFINITIONS_DIR",
        Path(__file__).parent / "definitions" / "fixtures" / "devices",
    ):
        result = await hass.services.async_call(
            DOMAIN,
            "reload_definitions",
            {},
            blocking=True,
            return_response=True,
        )
    assert result is not None
    assert "devices_loaded" in result
    assert result["added"] == []
    assert result["removed"] == []
    assert result["changed"] == []


@pytest.mark.asyncio
async def test_reload_definitions_updates_coordinator_catalog(
    hass: HomeAssistant, tmp_path: Path,
) -> None:
    """W5: reload_definitions must swap the coordinator's catalog reference.

    Build an alternative fixtures directory with one extra device. After the
    reload, the coordinator's ``_catalog.devices`` must include that new
    device (proves we're not just updating runtime.catalog).
    """
    entry, client = await _setup_entry(hass)

    # Snapshot the original device count.
    coord = entry.runtime_data.coordinator
    original_devices = set(coord.catalog.devices.keys())

    # Copy the fixture devices dir into tmp_path and add a new device file.
    src = Path(__file__).parent / "definitions" / "fixtures" / "devices"
    for f in src.glob("*.xml"):
        (tmp_path / f.name).write_text(f.read_text(), encoding="utf-8")
    (tmp_path / "extra.xml").write_text(
        """<device type="EXTRA" displayName="Extra device">
  <parameter id="0x0001" name="probe" displayName="Probe">
    <codec type="int16"/>
  </parameter>
</device>""", encoding="utf-8",
    )

    with patch(
        "custom_components.ecotherm_wolf.BUNDLED_DEFINITIONS_DIR",
        tmp_path,
    ):
        await hass.services.async_call(
            DOMAIN,
            "reload_definitions",
            {},
            blocking=True,
            return_response=True,
        )

    # The coordinator's _catalog reference must have changed.
    assert "EXTRA" in coord.catalog.devices
    assert original_devices.issubset(set(coord.catalog.devices.keys()))


@pytest.mark.asyncio
async def test_rediscover_merges_instead_of_replacing(
    hass: HomeAssistant,
) -> None:
    """rediscover_devices must MERGE -- never drop or renumber entries.

    Regression for the destructive behaviour: the service used to rebuild
    the map with sequential discovery-only ids. Manually added sub-modules
    (which never appear in the SystemConfigResp) were silently dropped,
    and shifted ids would have hung existing entities onto the wrong
    physical module. Scenario here: device 1 (CHA-10) moved its read
    address, device 2 is a manual sub-module discovery can't see, and a
    brand-new module shows up.
    """
    from custom_components.ecotherm_wolf.discovery import DiscoveredDevice

    entry, client = await _setup_entry(hass)
    coord = entry.runtime_data.coordinator
    # Simulate a manual sub-module the user added (not discoverable).
    coord.set_device_map({
        1: ("CHA-10", "0;0;0;1", "0;0;0;9"),   # user-set divergent write
        2: ("WWSystem_BM-2", "0x35", "0x30"),  # manual sub-module
    })

    discovered = [
        DiscoveredDevice(
            bus_address="0x99",  # CHA-10 moved to a new bus address
            device_type="CHA-10",
            dtid=270000,
            friendly_name="CHA-10",
        ),
        DiscoveredDevice(
            bus_address="0x55",  # genuinely new module
            device_type="MM",
            dtid=40000,
            friendly_name="MM",
        ),
        DiscoveredDevice(
            bus_address="0x7C",  # unresolved -> must never be merged
            device_type="ambiguous_dtid_0x15",
            dtid=0,
            friendly_name="Mehrdeutig",
        ),
    ]
    with patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=discovered),
    ):
        result = await hass.services.async_call(
            DOMAIN,
            "rediscover_devices",
            {},
            blocking=True,
            return_response=True,
        )

    assert result is not None
    new_map = coord.device_map
    # Device 1 keeps its id, gets the moved read address, and KEEPS the
    # user-set divergent write address.
    assert new_map[1] == ("CHA-10", "0x99", "0;0;0;9")
    # The manual sub-module survives untouched.
    assert new_map[2] == ("WWSystem_BM-2", "0x35", "0x30")
    # The new module appends at the next free id (3) -- no renumbering.
    assert new_map[3] == ("MM", "0x55", "0x55")
    # The ambiguous row was not merged anywhere.
    assert all(t != "ambiguous_dtid_0x15" for t, _r, _w in new_map.values())
    # Response reports the changes.
    assert [a["device_id"] for a in result["added"]] == [3]
    assert [u["device_id"] for u in result["updated"]] == [1]


@pytest.mark.asyncio
async def test_diagnose_returns_state(hass: HomeAssistant) -> None:
    """diagnose returns a snapshot of the integration's internal state."""
    entry, client = await _setup_entry(hass)
    client.connected = True

    result = await hass.services.async_call(
        DOMAIN,
        "diagnose",
        {},
        blocking=True,
        return_response=True,
    )
    assert result is not None
    assert result["connected"] is True
    assert "selected_count" in result
    assert "values_cached" in result
    assert "device_map" in result
    # The 2-element stored entry normalises to read == write; the diagnose
    # dump now reports both addresses separately.
    assert result["device_map"] == {
        "1": {
            "device_type": "CHA-10",
            "read_address": "0;0;0;1",
            "write_address": "0;0;0;1",
        },
    }
