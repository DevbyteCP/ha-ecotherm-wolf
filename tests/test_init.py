from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.ecotherm_wolf.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SELECTED_PARAMETERS,
    DOMAIN,
)


@pytest.mark.requires_wolf_files
@pytest.mark.asyncio
async def test_setup_and_unload(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: 9092,
            CONF_PASSWORD: "ism7",
        },
        options={CONF_SELECTED_PARAMETERS: []},
    )
    entry.add_to_hass(hass)

    # We don't have a real ISM7 -- patch the client so setup doesn't fail
    from unittest.mock import AsyncMock, patch
    with patch("custom_components.ecotherm_wolf.create_client") as factory:
        client = AsyncMock()
        client.connected = True
        factory.return_value = client
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.runtime_data is not None

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.requires_wolf_files
@pytest.mark.asyncio
async def test_options_update_triggers_reload(hass: HomeAssistant):
    """B1: editing options must reload the entry (heartbeat / debug / etc.)."""
    from unittest.mock import AsyncMock, patch

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: 9092,
            CONF_PASSWORD: "ism7",
        },
        options={CONF_SELECTED_PARAMETERS: [], "heartbeat_seconds": 60},
    )
    entry.add_to_hass(hass)

    with patch("custom_components.ecotherm_wolf.create_client") as factory:
        client = AsyncMock()
        client.connected = True
        factory.return_value = client
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        original_runtime = entry.runtime_data

        # Mutate options: this triggers our async_on_unload update-listener,
        # which calls hass.config_entries.async_reload(entry.entry_id).
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "heartbeat_seconds": 120},
        )
        await hass.async_block_till_done()

        # Reload spun up a fresh coordinator: runtime_data is a new object.
        assert entry.runtime_data is not None
        assert entry.runtime_data is not original_runtime


@pytest.mark.asyncio
async def test_setup_raises_auth_failed_on_auth_error(hass: HomeAssistant):
    """W4: wrong password -> ConfigEntryAuthFailed (triggers re-auth flow)."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.config_entries import ConfigEntryState

    from custom_components.ecotherm_wolf.ism7.exceptions import AuthenticationError

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: 9092,
            CONF_PASSWORD: "wrong",
        },
        options={CONF_SELECTED_PARAMETERS: []},
    )
    entry.add_to_hass(hass)

    with patch("custom_components.ecotherm_wolf.create_client") as factory:
        client = AsyncMock()
        client.connect.side_effect = AuthenticationError("bad password")
        factory.return_value = client
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state in (
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.SETUP_RETRY,
    )


@pytest.mark.asyncio
async def test_setup_raises_not_ready_on_connection_error(hass: HomeAssistant):
    """W4: TCP failures -> ConfigEntryNotReady (HA retries with backoff)."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.config_entries import ConfigEntryState

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.0.2.1",  # TEST-NET-1: guaranteed unroutable
            CONF_PORT: 9092,
            CONF_PASSWORD: "ism7",
        },
        options={CONF_SELECTED_PARAMETERS: []},
    )
    entry.add_to_hass(hass)

    with patch("custom_components.ecotherm_wolf.create_client") as factory:
        client = AsyncMock()
        client.connect.side_effect = ConnectionRefusedError("nope")
        factory.return_value = client
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state == ConfigEntryState.SETUP_RETRY


@pytest.mark.requires_wolf_files
@pytest.mark.asyncio
async def test_pre_discovery_storage_format_still_loads(hass: HomeAssistant):
    """Backward-compat: entries created before the discovery rebuild used
    ``CONF_DEVICES = {"1": ["CHA-test", "0;0;0;1"]}`` (a 2-element list, the
    pre write-address-split shape). Such an entry must still load cleanly
    without any migration -- ``_normalise_device_map`` defaults the write
    address to the read address for 2-element entries.
    """
    from unittest.mock import AsyncMock, patch

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: 9092,
            CONF_PASSWORD: "ism7",
            "devices": {"1": ["CHA-test", "0;0;0;1"]},
        },
        options={CONF_SELECTED_PARAMETERS: []},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ecotherm_wolf.create_client",
    ) as factory:
        client = AsyncMock()
        client.connected = True
        factory.return_value = client
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data is not None
        coord = entry.runtime_data.coordinator
        # The 2-element entry expands to a 3-tuple with write == read.
        assert coord.device_map[1] == ("CHA-test", "0;0;0;1", "0;0;0;1")
        assert coord.read_address_for(1) == "0;0;0;1"
        assert coord.write_address_for(1) == "0;0;0;1"


@pytest.mark.requires_wolf_files
@pytest.mark.asyncio
async def test_three_element_storage_format_loads(hass: HomeAssistant):
    """A 3-element ``CONF_DEVICES`` entry (the post-split shape) loads with
    distinct read and write bus addresses preserved.
    """
    from unittest.mock import AsyncMock, patch

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: 9092,
            CONF_PASSWORD: "ism7",
            "devices": {"1": ["CHA-test", "0;0;0;1", "0;0;0;9"]},
        },
        options={CONF_SELECTED_PARAMETERS: []},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ecotherm_wolf.create_client",
    ) as factory:
        client = AsyncMock()
        client.connected = True
        factory.return_value = client
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coord = entry.runtime_data.coordinator
        assert coord.device_map[1] == ("CHA-test", "0;0;0;1", "0;0;0;9")
        assert coord.read_address_for(1) == "0;0;0;1"
        assert coord.write_address_for(1) == "0;0;0;9"


@pytest.mark.asyncio
async def test_duplicate_host_aborts(hass: HomeAssistant):
    """W2: configuring the same host:port twice is refused."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.data_entry_flow import FlowResultType

    # First entry already configured for 1.2.3.4:9092.
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "1.2.3.4", CONF_PORT: 9092, CONF_PASSWORD: "ism7"},
        unique_id="1.2.3.4:9092",
    )
    existing.add_to_hass(hass)

    with patch("custom_components.ecotherm_wolf.config_flow.create_client") as factory:
        factory.return_value = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"host": "1.2.3.4", "port": 9092, "password": "ism7"},
        )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"


# -- Wolf resource files (<config>/ecotherm_wolf/) --------------------------

_ENTRY_DATA = {CONF_HOST: "127.0.0.1", CONF_PORT: 9092, CONF_PASSWORD: "ism7"}


@pytest.mark.asyncio
async def test_setup_retries_and_raises_issue_when_wolf_files_missing(
    hass: HomeAssistant, tmp_path,
):
    """No files anywhere -> setup retry + repair issue naming the files."""
    from unittest.mock import patch

    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import issue_registry as ir

    entry = MockConfigEntry(
        domain=DOMAIN, data=_ENTRY_DATA, options={CONF_SELECTED_PARAMETERS: []},
    )
    entry.add_to_hass(hass)
    user_dir = tmp_path / "ecotherm_wolf"

    with patch(
        "custom_components.ecotherm_wolf._resource_dir", return_value=user_dir,
    ), patch(
        "custom_components.ecotherm_wolf.BUNDLED_WOLF_DIR", tmp_path / "none",
    ), patch(
        "custom_components.ecotherm_wolf.BUNDLED_CLIENT_CERT", tmp_path / "none.pfx",
    ), patch("custom_components.ecotherm_wolf.create_client") as factory:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    factory.assert_not_called()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "missing_wolf_files")
    assert issue is not None
    assert issue.translation_placeholders == {
        "path": str(user_dir),
        "files": "device.xml, parameter.xml, converter.xml, client.pfx",
    }


@pytest.mark.asyncio
async def test_setup_uses_files_from_config_dir(hass: HomeAssistant, tmp_path):
    """Files in <config>/ecotherm_wolf/ are used; the cert path reaches the client."""
    import shutil
    from unittest.mock import AsyncMock, patch

    from homeassistant.helpers import issue_registry as ir

    from custom_components.ecotherm_wolf.resources import (
        BUNDLED_CLIENT_CERT,
        BUNDLED_WOLF_DIR,
        WOLF_XML_FILES,
    )

    if not BUNDLED_CLIENT_CERT.exists() or not BUNDLED_WOLF_DIR.exists():
        pytest.skip("Wolf files not present in this checkout")
    user_dir = tmp_path / "ecotherm_wolf"
    user_dir.mkdir()
    for name in WOLF_XML_FILES:
        shutil.copy(BUNDLED_WOLF_DIR / name, user_dir / name)
    shutil.copy(BUNDLED_CLIENT_CERT, user_dir / "client.pfx")

    entry = MockConfigEntry(
        domain=DOMAIN, data=_ENTRY_DATA, options={CONF_SELECTED_PARAMETERS: []},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.ecotherm_wolf._resource_dir", return_value=user_dir,
    ), patch(
        "custom_components.ecotherm_wolf.BUNDLED_WOLF_DIR", tmp_path / "none",
    ), patch(
        "custom_components.ecotherm_wolf.BUNDLED_CLIENT_CERT", tmp_path / "none.pfx",
    ), patch("custom_components.ecotherm_wolf.create_client") as factory:
        client = AsyncMock()
        client.connected = True
        factory.return_value = client
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert factory.call_args.kwargs["cert_path"] == user_dir / "client.pfx"
        # Curated presets still come from the package, not from user_dir.
        catalog = entry.runtime_data.catalog
        assert catalog.curated_standard
        assert ir.async_get(hass).async_get_issue(DOMAIN, "missing_wolf_files") is None
        assert await hass.config_entries.async_unload(entry.entry_id)
