"""Diagnostics export -- verify password is redacted."""
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
    CONF_REBOOT_PASSWORD,
    DOMAIN,
)
from custom_components.ecotherm_wolf.diagnostics import (
    async_get_config_entry_diagnostics,
)

# Needs Wolf's XMLs/certificate (scripts/fetch_wolf_files.py --bundled).
pytestmark = pytest.mark.requires_wolf_files


@pytest.mark.asyncio
async def test_diagnostics_redacts_password(hass: HomeAssistant):
    """The password must NOT appear in diagnostics output."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "1.2.3.4",
            CONF_PORT: 9092,
            CONF_PASSWORD: "secret_pw_must_not_leak",
            CONF_DEVICES: {"1": ["CHA-test", "0;0;0;1"]},
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
        client.on_update = MagicMock()
        client.on_reconnect = MagicMock()
        factory.return_value = client

        with patch(
            "custom_components.ecotherm_wolf.BUNDLED_DEFINITIONS_DIR",
            Path(__file__).parent / "definitions" / "fixtures" / "devices",
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

    result = await async_get_config_entry_diagnostics(hass, entry)
    # Password should not be present anywhere in the output
    flat = repr(result)
    assert "secret_pw_must_not_leak" not in flat
    # entry_data should be present but redacted
    assert "entry_data" in result
    assert result["entry_data"][CONF_PASSWORD] == "**REDACTED**"


@pytest.mark.asyncio
async def test_diagnostics_redacts_reboot_password_in_options(
    hass: HomeAssistant,
):
    """The reboot watchdog's password lives in entry.options -- must be redacted.

    Regression (1.0.6): 1.0.5 exported ``entry_options`` raw, so the ISM7
    web-UI admin password entered for the reboot watchdog leaked into
    diagnostics downloads (which the README asks users to attach to issues).
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "1.2.3.4",
            CONF_PORT: 9092,
            CONF_PASSWORD: "secret_pw_must_not_leak",
            CONF_DEVICES: {"1": ["CHA-test", "0;0;0;1"]},
        },
        options={
            "selected_parameters": [],
            CONF_REBOOT_PASSWORD: "web_admin_pw_must_not_leak",
            "reboot_host": "1.2.3.4",
            "reboot_username": "admin",
        },
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
        client.on_update = MagicMock()
        client.on_reconnect = MagicMock()
        factory.return_value = client

        with patch(
            "custom_components.ecotherm_wolf.BUNDLED_DEFINITIONS_DIR",
            Path(__file__).parent / "definitions" / "fixtures" / "devices",
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

    result = await async_get_config_entry_diagnostics(hass, entry)
    flat = repr(result)
    assert "web_admin_pw_must_not_leak" not in flat
    assert result["entry_options"][CONF_REBOOT_PASSWORD] == "**REDACTED**"
    # Non-secret reboot options stay readable for debugging.
    assert result["entry_options"]["reboot_username"] == "admin"
