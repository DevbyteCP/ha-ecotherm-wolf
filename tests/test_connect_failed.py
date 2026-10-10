# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""``connect_failed`` drives the "Kritischer Fehler (Connect nicht moeglich)"
binary sensor.

It flips ``True`` only once the auto-reconnect has kept failing past the
repair threshold (a real "gateway unreachable" fault worth a push), never on
a brief reset/reconnect cycle, and never while the connector is paused.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.ecotherm_wolf.const import (  # noqa: E402
    DEFAULT_RECONNECT_REPAIR_SECONDS,
)
from custom_components.ecotherm_wolf.coordinator import (  # noqa: E402
    EcothermWolfCoordinator,
)
from custom_components.ecotherm_wolf.definitions.catalog import (  # noqa: E402
    Catalog,
    DeviceDefinition,
)


def _coord(hass: HomeAssistant) -> EcothermWolfCoordinator:
    catalog = Catalog(devices={"CHA-10": DeviceDefinition("CHA-10", "CHA")})
    client = MagicMock()
    client.connected = True
    client.send = AsyncMock()
    client.force_disconnect = AsyncMock()
    client.on_update = MagicMock()
    client.on_reconnect = MagicMock()
    return EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map={1: "CHA-10"}, selected=[],
    )


@pytest.mark.asyncio
async def test_connect_failed_false_when_connected(hass: HomeAssistant) -> None:
    coord = _coord(hass)
    coord._is_active = True
    coord._disconnected_since = None
    assert coord.connect_failed is False


@pytest.mark.asyncio
async def test_connect_failed_false_on_brief_disconnect(hass: HomeAssistant) -> None:
    """Down only briefly -> supervisor is mid-reconnect, not a fault yet."""
    coord = _coord(hass)
    coord._is_active = True
    coord._disconnected_since = datetime.now(UTC) - timedelta(seconds=30)
    assert coord.connect_failed is False


@pytest.mark.asyncio
async def test_connect_failed_true_on_sustained_outage(hass: HomeAssistant) -> None:
    """Down past the repair threshold -> reconnect keeps failing -> critical."""
    coord = _coord(hass)
    coord._is_active = True
    coord._disconnected_since = datetime.now(UTC) - timedelta(
        seconds=DEFAULT_RECONNECT_REPAIR_SECONDS + 60,
    )
    assert coord.connect_failed is True


@pytest.mark.asyncio
async def test_connect_failed_false_when_paused(hass: HomeAssistant) -> None:
    """Connector paused (Active switch off) -> deliberate, never a fault."""
    coord = _coord(hass)
    coord._is_active = False
    coord._disconnected_since = datetime.now(UTC) - timedelta(
        seconds=DEFAULT_RECONNECT_REPAIR_SECONDS + 60,
    )
    assert coord.connect_failed is False
