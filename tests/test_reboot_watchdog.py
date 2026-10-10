# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""ISM7 reboot watchdog.

When the auto-reconnect can't re-establish the link after a configured number
of consecutive attempts, the coordinator power-cycles the gateway once over
its HTTP firmware endpoint, then keeps reconnecting. A cooldown prevents a
reboot loop while the gateway boots; the feature stays inert until a reboot
password is configured.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

import aiohttp  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.ecotherm_wolf.const import (  # noqa: E402
    REBOOT_FORM_DATA,
    REBOOT_PATH,
)
from custom_components.ecotherm_wolf.coordinator import (  # noqa: E402
    EcothermWolfCoordinator,
)
from custom_components.ecotherm_wolf.definitions.catalog import (  # noqa: E402
    Catalog,
    DeviceDefinition,
)


def _coord(
    hass: HomeAssistant, *, failures: int = 0, **kwargs,
) -> EcothermWolfCoordinator:
    catalog = Catalog(devices={"CHA-10": DeviceDefinition("CHA-10", "CHA")})
    client = MagicMock()
    client.connected = False
    client.consecutive_connect_failures = failures
    client.send = AsyncMock()
    client.force_disconnect = AsyncMock()
    client.on_update = MagicMock()
    client.on_reconnect = MagicMock()
    opts = {
        "reboot_after_failures": 5,
        "reboot_host": "192.0.2.10",
        "reboot_username": "admin",
        "reboot_password": "secret",
        "reboot_notify_service": "",
    }
    opts.update(kwargs)
    return EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map={1: "CHA-10"}, selected=[], **opts,
    )


# -- enable / threshold gating --------------------------------------------- #
@pytest.mark.asyncio
async def test_no_reboot_when_password_missing(hass: HomeAssistant) -> None:
    """Inert without credentials -- nothing to call on the protected endpoint."""
    coord = _coord(hass, failures=10, reboot_password="")
    coord._reboot_gateway_http = AsyncMock(return_value=True)
    await coord._maybe_reboot_gateway()
    coord._reboot_gateway_http.assert_not_called()


@pytest.mark.asyncio
async def test_no_reboot_when_threshold_zero(hass: HomeAssistant) -> None:
    coord = _coord(hass, failures=10, reboot_after_failures=0)
    coord._reboot_gateway_http = AsyncMock(return_value=True)
    await coord._maybe_reboot_gateway()
    coord._reboot_gateway_http.assert_not_called()


@pytest.mark.asyncio
async def test_no_reboot_below_threshold(hass: HomeAssistant) -> None:
    coord = _coord(hass, failures=4, reboot_after_failures=5)
    coord._reboot_gateway_http = AsyncMock(return_value=True)
    await coord._maybe_reboot_gateway()
    coord._reboot_gateway_http.assert_not_called()


# -- firing + cooldown ------------------------------------------------------ #
@pytest.mark.asyncio
async def test_reboot_fires_at_threshold(hass: HomeAssistant) -> None:
    coord = _coord(hass, failures=5, reboot_after_failures=5)
    coord._reboot_gateway_http = AsyncMock(return_value=True)
    coord._notify_reboot = AsyncMock()
    await coord._maybe_reboot_gateway()
    coord._reboot_gateway_http.assert_awaited_once()
    coord._notify_reboot.assert_awaited_once_with(5)
    assert coord._last_reboot_at is not None


@pytest.mark.asyncio
async def test_cooldown_blocks_second_reboot(hass: HomeAssistant) -> None:
    coord = _coord(hass, failures=9, reboot_cooldown_seconds=600)
    coord._reboot_gateway_http = AsyncMock(return_value=True)
    coord._notify_reboot = AsyncMock()
    await coord._maybe_reboot_gateway()  # fires
    await coord._maybe_reboot_gateway()  # within cooldown -> blocked
    coord._reboot_gateway_http.assert_awaited_once()


@pytest.mark.asyncio
async def test_reboot_again_after_cooldown(hass: HomeAssistant) -> None:
    coord = _coord(hass, failures=9, reboot_cooldown_seconds=600)
    coord._reboot_gateway_http = AsyncMock(return_value=True)
    coord._notify_reboot = AsyncMock()
    await coord._maybe_reboot_gateway()  # fires
    coord._last_reboot_at = datetime.now(UTC) - timedelta(seconds=601)
    await coord._maybe_reboot_gateway()  # cooldown elapsed -> fires again
    assert coord._reboot_gateway_http.await_count == 2


@pytest.mark.asyncio
async def test_no_notify_when_http_failed(hass: HomeAssistant) -> None:
    """A dead web endpoint -> no success notify, but cooldown still armed."""
    coord = _coord(hass, failures=5)
    coord._reboot_gateway_http = AsyncMock(return_value=False)
    coord._notify_reboot = AsyncMock()
    await coord._maybe_reboot_gateway()
    coord._reboot_gateway_http.assert_awaited_once()
    coord._notify_reboot.assert_not_called()
    assert coord._last_reboot_at is not None  # won't hammer every tick


# -- the actual HTTP POST --------------------------------------------------- #
@pytest.mark.asyncio
async def test_reboot_http_posts_form_with_basic_auth(hass: HomeAssistant) -> None:
    coord = _coord(
        hass, reboot_host="192.0.2.10",
        reboot_username="admin", reboot_password="secret",
    )

    class _Resp:
        status = 200

        async def __aenter__(self) -> _Resp:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    session = MagicMock()
    session.post = MagicMock(return_value=_Resp())
    with patch(
        "custom_components.ecotherm_wolf.coordinator.async_get_clientsession",
        return_value=session,
    ):
        ok = await coord._reboot_gateway_http()

    assert ok is True
    session.post.assert_called_once()
    args, kwargs = session.post.call_args
    assert args[0] == f"http://192.0.2.10{REBOOT_PATH}"
    assert kwargs["data"] == dict(REBOOT_FORM_DATA)
    assert kwargs["auth"] == aiohttp.BasicAuth("admin", "secret")


@pytest.mark.asyncio
async def test_reboot_http_returns_false_on_client_error(
    hass: HomeAssistant,
) -> None:
    coord = _coord(hass)
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with patch(
        "custom_components.ecotherm_wolf.coordinator.async_get_clientsession",
        return_value=session,
    ):
        ok = await coord._reboot_gateway_http()
    assert ok is False


# -- notify ----------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_notify_reboot_calls_service(hass: HomeAssistant) -> None:
    coord = _coord(hass, reboot_notify_service="notify.mobile_app_x")
    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock,
    ) as call:
        await coord._notify_reboot(7)
    call.assert_awaited_once()
    args = call.call_args.args
    assert args[0] == "notify"
    assert args[1] == "mobile_app_x"
    assert "7" in args[2]["message"]


@pytest.mark.asyncio
async def test_notify_reboot_noop_without_service(hass: HomeAssistant) -> None:
    coord = _coord(hass, reboot_notify_service="")
    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock,
    ) as call:
        await coord._notify_reboot(3)
    call.assert_not_called()


# -- runtime 'ISM7 Auto-Reboot' switch -------------------------------------- #
@pytest.mark.asyncio
async def test_switch_off_suppresses_reboot(hass: HomeAssistant) -> None:
    """Even fully configured + over threshold, the switch off blocks the reboot."""
    coord = _coord(hass, failures=10)
    coord._reboot_gateway_http = AsyncMock(return_value=True)
    coord.set_reboot_watchdog_enabled(False)
    assert coord.reboot_watchdog_enabled is False
    await coord._maybe_reboot_gateway()
    coord._reboot_gateway_http.assert_not_called()


@pytest.mark.asyncio
async def test_switch_on_allows_reboot(hass: HomeAssistant) -> None:
    coord = _coord(hass, failures=10)
    coord._reboot_gateway_http = AsyncMock(return_value=True)
    coord._notify_reboot = AsyncMock()
    coord.set_reboot_watchdog_enabled(False)
    coord.set_reboot_watchdog_enabled(True)  # back on (default is on)
    assert coord.reboot_watchdog_enabled is True
    await coord._maybe_reboot_gateway()
    coord._reboot_gateway_http.assert_awaited_once()
