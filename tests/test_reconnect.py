"""End-to-end test: client drops + reconnects; coordinator re-subscribes."""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.ecotherm_wolf.coordinator import EcothermWolfCoordinator
from custom_components.ecotherm_wolf.definitions.catalog import (
    Catalog,
    DeviceDefinition,
    ParameterDefinition,
)
from custom_components.ecotherm_wolf.definitions.codecs import Int16Codec, ScaledCodec
from custom_components.ecotherm_wolf.ism7.client import Ism7Client
from tests.ism7.mock_server import MockIsm7Server


@pytest.fixture(autouse=True)
def _allow_local_sockets(socket_enabled: None) -> None:
    """This test speaks real localhost TCP against the mock ISM7 server;
    lift the HA plugin's socket block (same pattern as tests/ism7/)."""
    return


@pytest.mark.asyncio
async def test_resubscribes_after_reconnect(hass: HomeAssistant) -> None:
    catalog = Catalog(devices={
        "CHA-10": DeviceDefinition("CHA-10", "CHA", parameters={
            0x0080: ParameterDefinition(
                property_id=0x0080, name="t", display_name="T",
                codec=ScaledCodec(base=Int16Codec(unit="°C"), scale=10),
            ),
        }),
    })
    async with MockIsm7Server(password="ism7") as srv:
        srv.set_value_legacy(device=1, property_id=0x0080, raw="00F5")
        client = Ism7Client(host=srv.host, port=srv.port, password="ism7", use_tls=False)
        await client.connect()
        coord = EcothermWolfCoordinator(
            hass=hass, client=client, catalog=catalog,
            device_map={1: ("CHA-10", "0;0;0;1")},
            selected=[(1, 0x0080)],
        )
        await coord.async_setup()
        await asyncio.sleep(0.1)
        assert coord.data[(1, 0x0080)] == 24.5

        await client.force_disconnect()
        await asyncio.wait_for(client.wait_connected(), timeout=5.0)
        await asyncio.sleep(0.5)  # let re-subscribe + initial dump arrive
        assert coord.reconnect_count >= 1
        assert coord.data[(1, 0x0080)] == 24.5
        await client.disconnect()
