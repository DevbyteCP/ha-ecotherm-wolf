from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Needs Wolf's XMLs/certificate (scripts/fetch_wolf_files.py --bundled).
pytestmark = pytest.mark.requires_wolf_files

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402

from custom_components.ecotherm_wolf.const import CONF_DEVICES, DOMAIN  # noqa: E402
from custom_components.ecotherm_wolf.discovery import (  # noqa: E402
    DiscoveredDevice,
    SubmoduleCandidate,
)


async def _configure(hass: HomeAssistant, flow_id: str, user_input=None):
    """flow.async_configure that transparently drives progress dialogs.

    Discovery (after step 1) and the sub-module probe (before step 4) run
    as background tasks behind ``async_show_progress``. The frontend keeps
    calling configure until the next real form appears; mirror that here
    so the tests read like the wizard: submit -> next form.
    """
    result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    while result["type"] in (
        FlowResultType.SHOW_PROGRESS,
        FlowResultType.SHOW_PROGRESS_DONE,
    ):
        if result["type"] == FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
    return result


@pytest.fixture(autouse=True)
def _stub_entry_setup():
    """Stub the integration's entry setup for every config-flow test.

    Flows that reach ``async_create_entry`` make HA schedule the REAL
    ``async_setup_entry`` for the new entry -- which builds a real
    ``Ism7Client`` and opens a real socket (the HA test plugin blocks that
    with ``SocketBlockedError``). The wizard is the unit under test here,
    not entry setup; entry-setup behaviour has its own suite in
    ``test_init.py``.
    """
    with patch(
        "custom_components.ecotherm_wolf.async_setup_entry",
        return_value=True,
    ), patch(
        "custom_components.ecotherm_wolf.async_unload_entry",
        return_value=True,
    ):
        yield


# -- Discovery success path -----------------------------------------------


@pytest.mark.asyncio
async def test_discovery_shows_progress_dialog(hass: HomeAssistant):
    """Submitting step 1 surfaces a progress dialog while discovery runs.

    The connect + SystemConfigReq round trip takes seconds; without
    ``async_show_progress`` the wizard froze on step 1 with only HA's
    generic spinner. Block the mocked discovery on an event to observe the
    pending state, then release it and drive to the next form.
    """
    import asyncio as _asyncio

    gate = _asyncio.Event()

    async def _blocked_discover(client, catalog):
        await gate.wait()
        return [DiscoveredDevice(
            bus_address="0x29", device_type="CHA", dtid=270000,
            friendly_name="CHA",
        )]

    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(side_effect=_blocked_discover),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        # Discovery is still blocked -> the user sees the progress dialog.
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        assert result["progress_action"] == "discovering"

        gate.set()
        result = await _configure(hass, result["flow_id"])
        assert result["step_id"] == "select_discovered"


@pytest.mark.asyncio
async def test_discovery_success_shows_checkbox_list(hass: HomeAssistant):
    """Happy path: discovery finds devices -> user gets checkbox list."""
    discovered = [
        DiscoveredDevice(
            bus_address="0x29",
            device_type="CHA",
            dtid=270000,
            friendly_name="CHA -- Wärmepumpe",
        ),
        DiscoveredDevice(
            bus_address="0x35",
            device_type="BM-2",
            dtid=220000,
            friendly_name="BM-2 -- Bedienmodul",
        ),
    ]
    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=discovered),
    ) as mock_discover:
        # discover_devices is referenced inside async_step_devices, but the
        # function-level patch above patches the original module attribute;
        # we also need to make sure ``implemented = True`` is preserved.
        mock_discover.implemented = True

        client = AsyncMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        factory.return_value = client

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        assert result["step_id"] == "user"

        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        # We connected for discovery and got >= 1 device.
        assert result["step_id"] == "select_discovered"
        client.connect.assert_awaited()
        client.disconnect.assert_awaited()

        # Pick all of them.
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "selected": [
                    "CHA -- Wärmepumpe (Bus 0x29)",
                    "BM-2 -- Bedienmodul (Bus 0x35)",
                ],
            },
        )
        # New flow: select_discovered -> add_more -> write_addresses -> preset
        assert result["step_id"] == "add_more"

        result = await _configure(hass,
            result["flow_id"],
            user_input={"add_more": False},
        )
        # The write-address review step now sits before preset.
        assert result["step_id"] == "write_addresses"

        result = await _configure(hass,
            result["flow_id"],
            user_input={},  # accept the pre-filled write addresses
        )
        assert result["step_id"] == "preset"

        result = await _configure(hass,
            result["flow_id"],
            user_input={"preset": "standard"},
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        # Storage format is now a 3-list [device_type, read, write]. CHA
        # (DTID 270000) and BM-2 (DTID 220000) have curated write addresses
        # in the bundled catalog -- pre-filled and accepted unchanged.
        assert result["data"][CONF_DEVICES] == {
            "1": ["CHA", "0x29", "0x3"],
            "2": ["BM-2", "0x35", "0x30"],
        }


@pytest.mark.asyncio
async def test_discovery_success_pick_subset(hass: HomeAssistant):
    """User unticks one device -> only the picked subset ends up in the entry."""
    discovered = [
        DiscoveredDevice(
            bus_address="0x29",
            device_type="CHA",
            dtid=270000,
            friendly_name="CHA",
        ),
        DiscoveredDevice(
            bus_address="0x35",
            device_type="BM-2",
            dtid=220000,
            friendly_name="BM-2",
        ),
    ]
    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=discovered),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["step_id"] == "select_discovered"

        # Only pick CHA.
        result = await _configure(hass,
            result["flow_id"],
            user_input={"selected": ["CHA (Bus 0x29)"]},
        )
        assert result["step_id"] == "add_more"

        result = await _configure(hass,
            result["flow_id"],
            user_input={"add_more": False},
        )
        assert result["step_id"] == "write_addresses"

        result = await _configure(hass,
            result["flow_id"],
            user_input={},  # accept the pre-filled write address
        )
        assert result["step_id"] == "preset"

        result = await _configure(hass,
            result["flow_id"],
            user_input={"preset": "standard"},
        )
        # CHA (DTID 270000) has a curated write address (0x3).
        assert result["data"][CONF_DEVICES] == {"1": ["CHA", "0x29", "0x3"]}


# -- Discovery fail path --------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_fail_falls_back_to_manual(hass: HomeAssistant):
    """Discovery raises (AFTER a successful connect) -> manual add form.

    The manual fallback is reserved for "gateway reachable but the
    discovery probe misbehaved" -- connect-phase failures bounce back to
    step 1 instead (see the wrong-password / unreachable-host tests).
    """
    from custom_components.ecotherm_wolf.ism7.exceptions import Ism7Error

    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(side_effect=Ism7Error("nope")),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        # Fell through to manual.
        assert result["step_id"] == "add_device"


@pytest.mark.asyncio
async def test_wrong_password_returns_to_user_step(hass: HomeAssistant):
    """Login rejected during discovery -> back to step 1 with invalid_auth.

    First-time-user trap (pre-fix): a typo'd password silently fell
    through to the MANUAL device form -- the wizard completed fine and
    only the final entry setup failed with an auth error. The user had no
    way to connect the dots. Now the password field carries the error and
    the typed values are preserved as suggested values.
    """
    from custom_components.ecotherm_wolf.ism7.exceptions import (
        AuthenticationError,
    )

    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=[]),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(
            connect=AsyncMock(side_effect=AuthenticationError("invalidcredentials")),
            disconnect=AsyncMock(),
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "tippfehler"},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["errors"] == {"password": "invalid_auth"}

        # Fixing the password lets the flow proceed (connect now succeeds,
        # empty discovery -> manual fallback).
        factory.return_value = AsyncMock(
            connect=AsyncMock(), disconnect=AsyncMock(),
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "richtig"},
        )
        assert result["step_id"] == "add_device"


@pytest.mark.asyncio
async def test_unreachable_host_returns_to_user_step(hass: HomeAssistant):
    """TCP/TLS failure during discovery -> back to step 1 with cannot_connect.

    An entry configured against an unreachable gateway can never work;
    bouncing to the manual form (pre-fix behaviour) just postponed the
    failure to the end of the wizard.
    """
    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=[]),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(
            connect=AsyncMock(side_effect=OSError("no route to host")),
            disconnect=AsyncMock(),
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "10.0.0.99", "port": 9092, "password": "ism7"},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_manual_add_multiple_devices_then_finish(hass: HomeAssistant):
    """Manual loop: add CHA, add BM-2, click done -> proceed to preset."""
    from custom_components.ecotherm_wolf.ism7.exceptions import Ism7Error

    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(side_effect=Ism7Error("nope")),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["step_id"] == "add_device"

        # Add CHA.
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "CHA",
                "bus_address": "0x29",
                "done": False,
            },
        )
        assert result["step_id"] == "add_device"
        # No errors.
        assert not result.get("errors")

        # Add BM-2.
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "BM-2",
                "bus_address": "0x35",
                "done": False,
            },
        )
        assert result["step_id"] == "add_device"

        # Click done. The device_type dropdown validates against the
        # catalog options, so an untouched dropdown is OMITTED from the
        # submission (sending "" would fail schema validation) -- exactly
        # what the frontend does.
        result = await _configure(hass,
            result["flow_id"],
            user_input={"done": True},
        )
        # Manual entries route through the write-address review step too.
        assert result["step_id"] == "write_addresses"

        result = await _configure(hass,
            result["flow_id"],
            user_input={},  # accept the pre-filled write addresses
        )
        assert result["step_id"] == "preset"

        result = await _configure(hass,
            result["flow_id"],
            user_input={"preset": "standard"},
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        # CHA and BM-2 carry curated write addresses (0x3 / 0x30); a manual
        # entry of an uncurated type would default to write == read.
        assert result["data"][CONF_DEVICES] == {
            "1": ["CHA", "0x29", "0x3"],
            "2": ["BM-2", "0x35", "0x30"],
        }


@pytest.mark.asyncio
async def test_manual_add_with_explicit_write_address(hass: HomeAssistant):
    """A manual entry with the write-address field filled stores that value.

    The add step has an optional write-address field; when the user fills
    it with a valid hex address it becomes the device's write address
    (distinct from the read address), and the write-addresses review step
    shows it pre-filled.
    """
    from custom_components.ecotherm_wolf.ism7.exceptions import Ism7Error

    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(side_effect=Ism7Error("nope")),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["step_id"] == "add_device"

        # Add BM-2 with a divergent write address.
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "BM-2",
                "bus_address": "0x35",
                "write_address": "0x30",
                "done": True,
            },
        )
        assert result["step_id"] == "write_addresses"

        result = await _configure(hass,
            result["flow_id"],
            user_input={},  # accept the pre-filled (user-entered) write addr
        )
        assert result["step_id"] == "preset"

        result = await _configure(hass,
            result["flow_id"],
            user_input={"preset": "standard"},
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_DEVICES] == {
            "1": ["BM-2", "0x35", "0x30"],
        }


@pytest.mark.asyncio
async def test_manual_add_rejects_invalid_write_hex(hass: HomeAssistant):
    """A malformed write-address fails the hex validator on its own field."""
    from custom_components.ecotherm_wolf.ism7.exceptions import Ism7Error

    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(side_effect=Ism7Error("nope")),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["step_id"] == "add_device"

        # Read address valid, write address malformed (no 0x prefix).
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "BM-2",
                "bus_address": "0x35",
                "write_address": "30",
                "done": False,
            },
        )
        assert result["step_id"] == "add_device"
        assert result.get("errors", {}).get("write_address") == "invalid_hex"


@pytest.mark.asyncio
async def test_manual_add_rejects_invalid_hex(hass: HomeAssistant):
    """Bad bus address fails the hex validator with an error per field."""
    from custom_components.ecotherm_wolf.ism7.exceptions import Ism7Error

    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(side_effect=Ism7Error("nope")),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["step_id"] == "add_device"

        # Try malformed bus address (no 0x prefix).
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "CHA",
                "bus_address": "35",
                "done": False,
            },
        )
        # Still on add_device, with an error on bus_address.
        assert result["step_id"] == "add_device"
        assert result.get("errors", {}).get("bus_address") == "invalid_hex"

        # Try decimal-looking malformed.
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "CHA",
                "bus_address": "53",
                "done": False,
            },
        )
        assert result.get("errors", {}).get("bus_address") == "invalid_hex"


@pytest.mark.asyncio
async def test_manual_done_without_devices_errors(hass: HomeAssistant):
    """Clicking ``done`` without adding any device shows an error."""
    from custom_components.ecotherm_wolf.ism7.exceptions import Ism7Error

    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(side_effect=Ism7Error("nope")),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"done": True},
        )
        assert result["step_id"] == "add_device"
        assert result.get("errors", {}).get("base") == "no_devices_added"


@pytest.mark.asyncio
async def test_discovery_empty_falls_back_to_manual(hass: HomeAssistant):
    """Discovery returns [] (no Wolf modules) -> manual fallback."""
    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=[]),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["step_id"] == "add_device"


# -- Misc -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_step_does_not_validate_connection(hass: HomeAssistant):
    """async_step_user must not open a client connection; the connect
    happens in step_devices (for discovery) and again in async_setup_entry.
    """
    from custom_components.ecotherm_wolf.ism7.exceptions import Ism7Error

    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(side_effect=Ism7Error("nope")),
    ) as mock_discover:
        mock_discover.implemented = True
        client = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())
        factory.return_value = client

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        # First connect must happen only after step_user accepts the input.
        client.connect.assert_not_called()
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "wrong"},
        )
        # We are in add_device because discovery failed -- but we DID connect.
        assert result["step_id"] == "add_device"
        client.connect.assert_awaited()


@pytest.mark.asyncio
async def test_discovery_then_manual_merge(hass: HomeAssistant):
    """Discovery picks CHA + BM-2; user adds WWSystem_BM-2 + MK_BM-2 manually.

    Storage layout: discovery IDs 1..N keep their slots, manual entries
    append at N+1..M. Storage format unchanged: ``["device_type", "bus"]``.
    """
    discovered = [
        DiscoveredDevice(
            bus_address="0x29",
            device_type="CHA",
            dtid=270000,
            friendly_name="CHA",
        ),
        DiscoveredDevice(
            bus_address="0x35",
            device_type="BM-2",
            dtid=220000,
            friendly_name="BM-2",
        ),
    ]
    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=discovered),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["step_id"] == "select_discovered"

        # Take both discovered modules.
        result = await _configure(hass,
            result["flow_id"],
            user_input={"selected": ["CHA (Bus 0x29)", "BM-2 (Bus 0x35)"]},
        )
        assert result["step_id"] == "add_more"

        # User wants to add a sub-module manually.
        result = await _configure(hass,
            result["flow_id"],
            user_input={"add_more": True},
        )
        assert result["step_id"] == "add_device"

        # Add WWSystem_BM-2 on same bus as parent BM-2.
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "WWSystem_BM-2",
                "bus_address": "0x35",
                "done": False,
            },
        )
        assert result["step_id"] == "add_device"
        # Add MK_BM-2 on its dedicated mixer bus.
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "MK_BM-2",
                "bus_address": "0x85",
                "done": False,
            },
        )
        assert result["step_id"] == "add_device"

        # Done -- proceed to the write-address review step. The untouched
        # dropdown is omitted (an empty string would fail schema validation
        # against the catalog options, mirroring the real frontend).
        result = await _configure(hass,
            result["flow_id"],
            user_input={"done": True},
        )
        assert result["step_id"] == "write_addresses"
        # Each write_<id> field gets a localized label naming its module
        # via the mod_<id> placeholder ("Schreib-Adresse: CHA @ 0x29"
        # instead of the bare key "write_1").
        placeholders = result.get("description_placeholders") or {}
        assert placeholders.get("mod_1") == "CHA @ 0x29"
        assert placeholders.get("mod_2") == "BM-2 @ 0x35"
        assert placeholders.get("mod_3") == "WWSystem_BM-2 @ 0x35"
        assert placeholders.get("mod_4") == "MK_BM-2 @ 0x85"

        result = await _configure(hass,
            result["flow_id"],
            user_input={},  # accept the pre-filled write addresses
        )
        assert result["step_id"] == "preset"

        result = await _configure(hass,
            result["flow_id"],
            user_input={"preset": "standard"},
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        # Discovery IDs 1, 2 + manual IDs 3, 4. CHA / BM-2 / WWSystem_BM-2
        # carry curated write addresses (0x3 / 0x30 / 0x30); MK_BM-2 isn't
        # curated and defaults to write == read.
        assert result["data"][CONF_DEVICES] == {
            "1": ["CHA", "0x29", "0x3"],
            "2": ["BM-2", "0x35", "0x30"],
            "3": ["WWSystem_BM-2", "0x35", "0x30"],
            "4": ["MK_BM-2", "0x85", "0x85"],
        }


@pytest.mark.asyncio
async def test_add_more_renders_probed_submodules_on_one_connection(
    hass: HomeAssistant,
):
    """add_more pre-checks the sub-modules discovery probed -- no 2nd connect.

    Regression for a production hang: an earlier build auto-probed
    sub-modules when first rendering ``add_more``, which opened a SECOND TLS
    connection mid-flow. On the ISM7's single connection slot that
    connect/disconnect stalled and froze the wizard at "Schritt 4"
    indefinitely. The probe now runs during discovery on the SAME
    connection (``_run_discovery``); ``add_more`` just renders those
    candidates, pre-checking the ones the bus confirmed. So the user gets
    automatic sub-module detection AND ``create_client`` is still called
    exactly once for the whole wizard.
    """
    discovered = [
        DiscoveredDevice(
            bus_address="0x35",
            device_type="BM-2",
            dtid=220000,
            friendly_name="BM-2",
        ),
    ]
    # The probe found a warm-water sub-module on the BM-2's own bus.
    probed = [
        SubmoduleCandidate(
            device_type="WWSystem_BM-2",
            dtid=350000,
            bus_address="0x35",
            parent_device_type="BM-2",
            parent_bus_address="0x35",
            present=True,
        ),
    ]
    probe = AsyncMock(return_value=probed)
    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=discovered),
    ) as mock_discover, patch(
        "custom_components.ecotherm_wolf.discovery.probe_submodules",
        new=probe,
    ):
        mock_discover.implemented = True
        factory.return_value = AsyncMock(
            connect=AsyncMock(), disconnect=AsyncMock(),
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(
            hass, result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["step_id"] == "select_discovered"
        # The probe ran ONCE, during discovery, against the discovered list.
        assert probe.await_count == 1
        assert probe.await_args.args[2] is discovered
        # And it shared discovery's single connection -- no extra client.
        assert factory.call_count == 1

        result = await _configure(
            hass, result["flow_id"],
            user_input={"selected": ["BM-2 (Bus 0x35)"]},
        )
        # add_more renders straight away from the probed candidates -- no
        # progress dialog, and crucially no second connection.
        assert result["step_id"] == "add_more"
        assert result["type"] == FlowResultType.FORM
        assert factory.call_count == 1
        # The bus-confirmed sub-module is pre-checked.
        schema = result["data_schema"].schema
        defaults = {
            key.schema: key.default()
            for key in schema
            if hasattr(key, "default") and callable(key.default)
        }
        assert defaults.get("submodules") == ["WWSystem_BM-2@0x35"]

        # Accept the pre-checked sub-module (no manual typing needed).
        result = await _configure(
            hass, result["flow_id"],
            user_input={
                "submodules": ["WWSystem_BM-2@0x35"],
                "add_more": False,
            },
        )
        assert result["step_id"] == "write_addresses"
        result = await _configure(hass, result["flow_id"], user_input={})
        assert result["step_id"] == "preset"
        result = await _configure(
            hass, result["flow_id"], user_input={"preset": "standard"},
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        # Still one connection for the entire wizard.
        assert factory.call_count == 1
        # BM-2 (discovery, id 1) + the probed sub-module (id 2), both with
        # the curated 0x30 write address.
        assert result["data"][CONF_DEVICES] == {
            "1": ["BM-2", "0x35", "0x30"],
            "2": ["WWSystem_BM-2", "0x35", "0x30"],
        }


@pytest.mark.asyncio
async def test_discovery_ambiguous_resolution(hass: HomeAssistant):
    """Discovery flags one row ambiguous -> user picks CWL from dropdown."""
    discovered = [
        DiscoveredDevice(
            bus_address="0x29",
            device_type="CHA",
            dtid=270000,
            friendly_name="CHA",
        ),
        DiscoveredDevice(
            bus_address="0x7C",
            device_type="ambiguous_dtid_0x15",
            dtid=0,
            friendly_name="Mehrdeutig: SM1 oder Variante? (DID 0x15)",
            wrs_device_id="0x15",
            software_number="0x649B",
            candidates=["SM1", "CWL"],
        ),
    ]
    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=discovered),
    ) as mock_discover, patch(
        "custom_components.ecotherm_wolf.config_flow.BUNDLED_DEFINITIONS_DIR",
        Path(__file__).parent / "definitions" / "fixtures" / "devices",
    ):
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        # Discovery had an ambiguous row -> the resolve step appears.
        assert result["step_id"] == "resolve_ambiguous"

        # Pick CHA-10 (the only entry in the test fixture catalog) for the
        # ambiguous row. The discovery's bus_address is preserved. With the
        # new keying scheme, ambiguous rows fill slot positions 0..N-1, not
        # the index in ``_discovered`` -- so the single ambiguous row here
        # lands in slot 0 (key ``resolve_0``).
        result = await _configure(hass,
            result["flow_id"],
            user_input={"resolve_0": "CHA-10"},
        )
        assert result["step_id"] == "select_discovered"

        result = await _configure(hass,
            result["flow_id"],
            user_input={"selected": [
                "CHA (Bus 0x29)",
                # Resolving rewrites friendly_name to
                # "<type> -- <display_name>" from the fixture catalog.
                "CHA-10 -- CHA-10/400V (Test) (Bus 0x7C)",
            ]},
        )
        assert result["step_id"] == "add_more"
        result = await _configure(hass,
            result["flow_id"],
            user_input={"add_more": False},
        )
        assert result["step_id"] == "write_addresses"
        result = await _configure(hass,
            result["flow_id"],
            user_input={},  # accept the pre-filled write addresses
        )
        assert result["step_id"] == "preset"
        result = await _configure(hass,
            result["flow_id"],
            user_input={"preset": "standard"},
        )
        # Resolved entry uses the user-chosen device_type at its bus. The
        # test patches BUNDLED_DEFINITIONS_DIR to the synthetic fixtures
        # catalog, which carries no curated write addresses -- so every
        # write address defaults to the read address.
        assert result["data"][CONF_DEVICES] == {
            "1": ["CHA", "0x29", "0x29"],
            "2": ["CHA-10", "0x7C", "0x7C"],
        }


@pytest.mark.asyncio
async def test_manual_done_with_filled_form_saves_pending_entry(hass: HomeAssistant):
    """Live regression: filling the form AND ticking ``done`` in one submit
    must NOT drop the pending entry on the way to ``preset``.

    A hardware test surfaced this: the user typed MK_BM-2 / 0x85 and
    ticked "Fertig" at the same time. The pre-fix code went to preset
    without writing the entry; only the previously-confirmed manual rows
    made it into the entry. Now the pending row is appended first.
    """
    from custom_components.ecotherm_wolf.ism7.exceptions import Ism7Error

    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(side_effect=Ism7Error("nope")),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["step_id"] == "add_device"

        # Add CHA the normal way.
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "CHA",
                "bus_address": "0x29",
                "done": False,
            },
        )

        # Now: fill MK_BM-2 / 0x85 AND tick done in the same submit.
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "MK_BM-2",
                "bus_address": "0x85",
                "done": True,
            },
        )
        assert result["step_id"] == "write_addresses"

        result = await _configure(hass,
            result["flow_id"],
            user_input={},  # accept the pre-filled write addresses
        )
        assert result["step_id"] == "preset"

        result = await _configure(hass,
            result["flow_id"],
            user_input={"preset": "standard"},
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        # Both the pre-existing entry and the pending one are stored. CHA's
        # write address auto-fills from the curated DTID map (0x3); MK_BM-2
        # isn't curated, so write == read.
        assert result["data"][CONF_DEVICES] == {
            "1": ["CHA", "0x29", "0x3"],
            "2": ["MK_BM-2", "0x85", "0x85"],
        }


@pytest.mark.asyncio
async def test_manual_done_with_partial_form_keeps_step_with_error(
    hass: HomeAssistant,
):
    """``done`` ticked with only a device_type (no bus_address) -> stay on
    the same step with an error, never silently advance to preset."""
    from custom_components.ecotherm_wolf.ism7.exceptions import Ism7Error

    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(side_effect=Ism7Error("nope")),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["step_id"] == "add_device"

        # device_type present but bus_address empty + done=True. Must NOT
        # advance.
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "CHA",
                "bus_address": "",
                "done": True,
            },
        )
        assert result["step_id"] == "add_device"
        assert result.get("errors", {}).get("bus_address") == "invalid_hex"


@pytest.mark.asyncio
async def test_resolve_ambiguous_renders_per_row_placeholders(hass: HomeAssistant):
    """Each ambiguous row gets a per-slot label populated from its discovery
    metadata (bus address, DID, firmware version).

    The translation files declare ``resolve_<i>`` field labels that consume
    ``{bus_<i>}/{did_<i>}/{fw_<i>}``. The config flow must emit those
    placeholders in slot order so the rendered form reads e.g. "Modul auf
    Bus 0x7C (DID 0x15, Firmware 1.02)" for the first ambiguous row.
    """
    discovered = [
        # First row is unambiguous -- it should not consume a resolve slot.
        DiscoveredDevice(
            bus_address="0x29",
            device_type="CHA",
            dtid=270000,
            friendly_name="CHA",
        ),
        # Two ambiguous rows -> slots 0 and 1 with their own placeholders.
        DiscoveredDevice(
            bus_address="0x7C",
            device_type="ambiguous_dtid_0x15",
            dtid=0,
            friendly_name="Mehrdeutig (DID 0x15)",
            wrs_device_id="0x15",
            software_number="0x649B",
            firmware_version="1.02",
            candidates=["SM1"],
        ),
        DiscoveredDevice(
            bus_address="0x85",
            device_type="ambiguous_dtid_0x42",
            dtid=0,
            friendly_name="Mehrdeutig (DID 0x42)",
            wrs_device_id="0x42",
            software_number="0xFFFF",
            firmware_version="2.07",
            candidates=["MK_BM-2"],
        ),
    ]
    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=discovered),
    ) as mock_discover, patch(
        "custom_components.ecotherm_wolf.config_flow.BUNDLED_DEFINITIONS_DIR",
        Path(__file__).parent / "definitions" / "fixtures" / "devices",
    ):
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["step_id"] == "resolve_ambiguous"

        # The form's description_placeholders carry bus/did/fw per slot.
        placeholders = result.get("description_placeholders") or {}
        assert placeholders.get("bus_0") == "0x7C"
        assert placeholders.get("did_0") == "0x15"
        assert placeholders.get("fw_0") == "1.02"
        assert placeholders.get("bus_1") == "0x85"
        assert placeholders.get("did_1") == "0x42"
        assert placeholders.get("fw_1") == "2.07"


@pytest.mark.asyncio
async def test_add_device_duplicate_detection(hass: HomeAssistant):
    """Re-entering the same (device_type, bus_address) pair on the manual
    add step surfaces ``duplicate_device`` instead of silently creating
    two slots for the same module.
    """
    from custom_components.ecotherm_wolf.ism7.exceptions import Ism7Error

    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(side_effect=Ism7Error("nope")),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["step_id"] == "add_device"

        # Add CHA @ 0x8 once -- accepted.
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "CHA",
                "bus_address": "0x8",
                "done": False,
            },
        )
        assert result["step_id"] == "add_device"
        assert not result.get("errors")

        # Add CHA @ 0x8 again -- rejected with duplicate_device on the form.
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "CHA",
                "bus_address": "0x8",
                "done": False,
            },
        )
        assert result["step_id"] == "add_device"
        assert result.get("errors", {}).get("base") == "duplicate_device"

        # Same module but uppercase bus address is still a duplicate.
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "CHA",
                "bus_address": "0X8",
                "done": False,
            },
        )
        assert result.get("errors", {}).get("base") == "duplicate_device"

        # Different module on the same bus -- accepted (no duplicate).
        result = await _configure(hass,
            result["flow_id"],
            user_input={
                "device_type": "BM-2",
                "bus_address": "0x8",
                "done": False,
            },
        )
        assert not result.get("errors")


@pytest.mark.asyncio
async def test_preset_step_default_is_standard(hass: HomeAssistant):
    """The preset radio defaults to ``standard`` (recommended option)."""
    discovered = [
        DiscoveredDevice(
            bus_address="0x29",
            device_type="CHA",
            dtid=270000,
            friendly_name="CHA",
        ),
    ]
    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=discovered),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["step_id"] == "select_discovered"
        result = await _configure(hass,
            result["flow_id"],
            user_input={"selected": ["CHA (Bus 0x29)"]},
        )
        assert result["step_id"] == "add_more"
        result = await _configure(hass,
            result["flow_id"],
            user_input={"add_more": False},
        )
        # The write-address review step precedes preset now.
        assert result["step_id"] == "write_addresses"
        result = await _configure(hass,
            result["flow_id"],
            user_input={},
        )
        # Now on preset step -- check the default schema value.
        assert result["step_id"] == "preset"
        schema = result["data_schema"].schema
        defaults = {
            key.schema: key.default()
            for key in schema
            if hasattr(key, "default") and callable(key.default)
        }
        assert defaults.get("preset") == "standard"


@pytest.mark.asyncio
async def test_write_addresses_step_prefills_curated_default(hass: HomeAssistant):
    """The write-addresses step pre-fills the curated write address.

    BM-2 (DTID 220000) has a curated write address (0x30) in the bundled
    Wolf catalog -- the discovery flow seeds it and the write-addresses
    step shows the field defaulting to 0x30, not the read address 0x35.
    """
    discovered = [
        DiscoveredDevice(
            bus_address="0x35",
            device_type="BM-2",
            dtid=220000,
            friendly_name="BM-2",
        ),
    ]
    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=discovered),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"selected": ["BM-2 (Bus 0x35)"]},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"add_more": False},
        )
        assert result["step_id"] == "write_addresses"
        # The field defaults to the curated write address (0x30), not 0x35.
        schema = result["data_schema"].schema
        defaults = {
            key.schema: key.default()
            for key in schema
            if hasattr(key, "default") and callable(key.default)
        }
        assert defaults.get("write_1") == "0x30"


@pytest.mark.asyncio
async def test_write_addresses_step_user_can_correct(hass: HomeAssistant):
    """The user can override the pre-filled write address in the step.

    Discovery seeds BM-2's curated write address (0x30); the user types a
    different value (0x31) in the write-addresses step and that value is
    what gets stored.
    """
    discovered = [
        DiscoveredDevice(
            bus_address="0x35",
            device_type="BM-2",
            dtid=220000,
            friendly_name="BM-2",
        ),
    ]
    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=discovered),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"selected": ["BM-2 (Bus 0x35)"]},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"add_more": False},
        )
        assert result["step_id"] == "write_addresses"
        # Override the pre-filled address.
        result = await _configure(hass,
            result["flow_id"],
            user_input={"write_1": "0x31"},
        )
        assert result["step_id"] == "preset"
        result = await _configure(hass,
            result["flow_id"],
            user_input={"preset": "standard"},
        )
        assert result["data"][CONF_DEVICES] == {"1": ["BM-2", "0x35", "0x31"]}


@pytest.mark.asyncio
async def test_write_addresses_step_empty_means_same_as_read(hass: HomeAssistant):
    """Clearing the write-address field stores write == read.

    Discovery seeds BM-2's curated write address (0x30), but the user
    explicitly clears the field -- the stored write address falls back to
    the read address (0x35).
    """
    discovered = [
        DiscoveredDevice(
            bus_address="0x35",
            device_type="BM-2",
            dtid=220000,
            friendly_name="BM-2",
        ),
    ]
    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=discovered),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"selected": ["BM-2 (Bus 0x35)"]},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"add_more": False},
        )
        assert result["step_id"] == "write_addresses"
        result = await _configure(hass,
            result["flow_id"],
            user_input={"write_1": ""},
        )
        assert result["step_id"] == "preset"
        result = await _configure(hass,
            result["flow_id"],
            user_input={"preset": "standard"},
        )
        # Empty field -> write == read.
        assert result["data"][CONF_DEVICES] == {"1": ["BM-2", "0x35", "0x35"]}


@pytest.mark.asyncio
async def test_write_addresses_step_rejects_invalid_hex(hass: HomeAssistant):
    """A malformed write address in the review step surfaces invalid_hex."""
    discovered = [
        DiscoveredDevice(
            bus_address="0x35",
            device_type="BM-2",
            dtid=220000,
            friendly_name="BM-2",
        ),
    ]
    with patch(
        "custom_components.ecotherm_wolf.config_flow.create_client"
    ) as factory, patch(
        "custom_components.ecotherm_wolf.discovery.discover_devices",
        new=AsyncMock(return_value=discovered),
    ) as mock_discover:
        mock_discover.implemented = True
        factory.return_value = AsyncMock(connect=AsyncMock(), disconnect=AsyncMock())

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"selected": ["BM-2 (Bus 0x35)"]},
        )
        result = await _configure(hass,
            result["flow_id"],
            user_input={"add_more": False},
        )
        assert result["step_id"] == "write_addresses"
        result = await _configure(hass,
            result["flow_id"],
            user_input={"write_1": "not-hex"},
        )
        # Stays on the step with a per-field error.
        assert result["step_id"] == "write_addresses"
        assert result.get("errors", {}).get("write_1") == "invalid_hex"


@pytest.mark.asyncio
async def test_reauth_flow_updates_password(hass: HomeAssistant):
    """ConfigEntryAuthFailed triggers reauth; the flow must exist and work.

    Live bug: the ISM7 answered ``invalidcredentials``, ``__init__`` raised
    ``ConfigEntryAuthFailed``, and HA crashed the reauth attempt with
    ``UnknownStep: Handler EcothermWolfConfigFlow doesn't support step
    reauth``. The flow asks for the password only (no connection probe --
    the ISM7's single-session guard would make a probe fail spuriously)
    and reloads the entry with the new value.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "1.2.3.4",
            "port": 9092,
            "password": "old-password",
            CONF_DEVICES: {"1": ["CHA", "0x8", "0x3"]},
        },
        options={"selected_parameters": []},
        unique_id="1.2.3.4:9092",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    # Patch setup so the reload triggered by the flow doesn't try to open
    # a real connection.
    with patch(
        "custom_components.ecotherm_wolf.async_setup_entry",
        return_value=True,
    ):
        result = await _configure(hass,
            result["flow_id"],
            user_input={"password": "new-password"},
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new-password"
    # Everything else stays untouched.
    assert entry.data["host"] == "1.2.3.4"
    assert entry.data[CONF_DEVICES] == {"1": ["CHA", "0x8", "0x3"]}


@pytest.mark.asyncio
async def test_options_flow_init_shows_menu(hass: HomeAssistant):
    """Smoke test: opening options flow shows the three-tab menu."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "1.2.3.4",
            "port": 9092,
            "password": "ism7",
            CONF_DEVICES: {"1": ["CHA-test", "0;0;0;1"]},
        },
        options={"selected_parameters": []},
    )
    entry.add_to_hass(hass)

    with patch("custom_components.ecotherm_wolf.create_client") as factory:
        client = MagicMock()
        client.connected = True
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.send = AsyncMock()
        client.on_update = MagicMock()
        factory.return_value = client

        with patch(
            "custom_components.ecotherm_wolf.BUNDLED_DEFINITIONS_DIR",
            Path(__file__).parent / "definitions" / "fixtures" / "devices",
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            result = await hass.config_entries.options.async_init(entry.entry_id)
            assert result["type"] == FlowResultType.MENU
            assert set(result["menu_options"]) == {
                "parameters",
                "connection",
                "advanced",
                "reboot",
            }


@pytest.mark.asyncio
async def test_step_user_reports_missing_wolf_files(hass: HomeAssistant):
    """Without the Wolf files the wizard stays on step 1 with a clear hint."""
    with patch(
        "custom_components.ecotherm_wolf.config_flow.missing_files",
        return_value=["client.pfx"],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "127.0.0.1", "port": 9092, "password": "ism7"},
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "missing_wolf_files"}
    assert result["description_placeholders"]["files"] == "client.pfx"
    assert result["description_placeholders"]["path"] == hass.config.path(
        "ecotherm_wolf",
    )
