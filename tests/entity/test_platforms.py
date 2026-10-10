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
    CONF_SELECTED_PARAMETERS,
    DOMAIN,
)


@pytest.mark.requires_wolf_files
@pytest.mark.asyncio
async def test_creates_entities_per_platform(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: 9092,
            CONF_PASSWORD: "ism7",
            CONF_DEVICES: {"1": ["CHA-10", "0;0;0;1"]},
        },
        options={CONF_SELECTED_PARAMETERS: [["1", "0x0080"], ["1", "0x00A1"]]},
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

        # Use the test fixture catalog (CHA-test.xml has 3 parameters; we
        # select 0x0080 sensor and 0x00A1 select).
        with patch(
            "custom_components.ecotherm_wolf.BUNDLED_DEFINITIONS_DIR",
            Path(__file__).parent.parent / "definitions" / "fixtures" / "devices",
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

    states = hass.states.async_all()
    entity_ids = [s.entity_id for s in states]
    assert any("vorlauftemperatur" in e for e in entity_ids)
    assert any("betriebsart" in e for e in entity_ids)


def test_resolve_native_step_from_codec_resolution() -> None:
    """Scaled codecs step in their resolution; plain ints keep HA's default.

    Regression (1.0.6): without an explicit step HA defaults to 1.0 for any
    range > 1, so half-degree setpoints (ScaledCodec, decimals=1) were
    unreachable from the UI slider/box.
    """
    from custom_components.ecotherm_wolf.definitions.codecs import (
        Int16Codec,
        ScaledCodec,
    )
    from custom_components.ecotherm_wolf.entity.number import (
        _resolve_native_step,
    )

    scaled = ScaledCodec(
        base=Int16Codec(min_value=5, max_value=30, unit="°C"), scale=10,
    )
    assert _resolve_native_step(scaled) == pytest.approx(0.1)
    two_decimals = ScaledCodec(
        base=Int16Codec(min_value=0, max_value=5, unit="bar"), scale=100,
    )
    assert _resolve_native_step(two_decimals) == pytest.approx(0.01)
    # Plain integer codec: no override -> HA's default step applies.
    assert _resolve_native_step(
        Int16Codec(min_value=5, max_value=30, unit="°C"),
    ) is None
