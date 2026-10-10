"""Watchdog triggers active reset when stale threshold exceeded."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.ecotherm_wolf.coordinator import EcothermWolfCoordinator
from custom_components.ecotherm_wolf.definitions.catalog import (
    Catalog,
    DeviceDefinition,
)


@pytest.mark.asyncio
async def test_active_reset_when_stale(hass: HomeAssistant) -> None:
    """When _last_push_at is older than threshold, force_disconnect is called."""
    catalog = Catalog(devices={"CHA-10": DeviceDefinition("CHA-10", "CHA")})
    client = MagicMock()
    client.connected = True
    client.send = AsyncMock()
    client.force_disconnect = AsyncMock()
    client.on_update = MagicMock()
    client.on_reconnect = MagicMock()

    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map={1: "CHA-10"}, selected=[],
    )
    # Make watchdog very aggressive so the test runs fast.
    coord._stale_threshold = timedelta(seconds=0)
    coord._watchdog_interval_seconds = 0.1
    # Set _last_push_at to 10 minutes ago.
    coord._last_push_at = datetime.now(UTC) - timedelta(minutes=10)

    await coord.async_setup()
    # Wait for watchdog to fire at least once (interval is 0.1s).
    await asyncio.sleep(0.3)
    assert client.force_disconnect.await_count >= 1
    assert coord.stale_minutes >= 9  # ~10 minutes, allowing slight clock drift
    await coord.async_shutdown()


@pytest.mark.asyncio
async def test_no_reset_when_fresh(hass: HomeAssistant) -> None:
    """Recent push -> no force_disconnect."""
    catalog = Catalog(devices={"CHA-10": DeviceDefinition("CHA-10", "CHA")})
    client = MagicMock()
    client.connected = True
    client.send = AsyncMock()
    client.force_disconnect = AsyncMock()
    client.on_update = MagicMock()
    client.on_reconnect = MagicMock()

    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map={1: "CHA-10"}, selected=[],
    )
    coord._stale_threshold = timedelta(seconds=10)
    coord._watchdog_interval_seconds = 0.1
    coord._last_push_at = datetime.now(UTC)  # just now

    await coord.async_setup()
    await asyncio.sleep(0.3)
    assert client.force_disconnect.await_count == 0
    assert coord.stale_minutes == 0
    await coord.async_shutdown()


@pytest.mark.asyncio
async def test_active_reset_disabled(hass: HomeAssistant) -> None:
    """When active_reset disabled, stale_minutes still tracked but no force_disconnect."""
    catalog = Catalog(devices={"CHA-10": DeviceDefinition("CHA-10", "CHA")})
    client = MagicMock()
    client.connected = True
    client.send = AsyncMock()
    client.force_disconnect = AsyncMock()
    client.on_update = MagicMock()
    client.on_reconnect = MagicMock()

    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map={1: "CHA-10"}, selected=[],
    )
    coord._stale_threshold = timedelta(seconds=0)
    coord._watchdog_interval_seconds = 0.1
    coord._active_reset_enabled = False
    coord._last_push_at = datetime.now(UTC) - timedelta(minutes=5)

    await coord.async_setup()
    await asyncio.sleep(0.3)
    assert client.force_disconnect.await_count == 0
    assert coord.stale_minutes >= 4
    await coord.async_shutdown()
