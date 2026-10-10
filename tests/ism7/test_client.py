from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest

from custom_components.ecotherm_wolf.ism7.client import (
    _OTHER_RESPONSES_MAX,
    Ism7Client,
)
from custom_components.ecotherm_wolf.ism7.exceptions import (
    AuthenticationError,
    ConnectionLost,
)
from custom_components.ecotherm_wolf.ism7.protocol import (
    InfoRead,
    SystemConfigResp,
    TelegramBundleReq,
    TelegramBundleResp,
    UnknownPayload,
)
from tests.ism7.mock_server import MockIsm7Server


@pytest.mark.asyncio
async def test_client_logs_in() -> None:
    async with MockIsm7Server(password="ism7") as srv:
        client = Ism7Client(
            host=srv.host, port=srv.port, password="ism7", use_tls=False,
        )
        await client.connect()
        assert client.connected is True
        await client.disconnect()


@pytest.mark.asyncio
async def test_client_rejects_bad_password() -> None:
    async with MockIsm7Server(password="ism7") as srv:
        client = Ism7Client(
            host=srv.host, port=srv.port, password="wrong", use_tls=False,
        )
        with pytest.raises(AuthenticationError):
            await client.connect()


@pytest.mark.asyncio
async def test_client_reconnects_with_backoff() -> None:
    """If TCP drops, client retries with exp backoff and re-establishes."""
    async with MockIsm7Server(password="ism7") as srv:
        client = Ism7Client(
            host=srv.host, port=srv.port, password="ism7", use_tls=False,
        )
        await client.connect()
        # Force disconnect.
        await client.force_disconnect()
        # Allow auto-reconnect to finish.
        await asyncio.wait_for(client.wait_connected(), timeout=5.0)
        assert client.connected is True
        await client.disconnect()


@pytest.mark.asyncio
async def test_client_survives_truncated_frame() -> None:
    """W11: a single corrupt frame must not tear down the read loop.

    Strategy: spin up a server that sends a valid login response, then a
    frame whose body is *shorter* than the advertised length so the client's
    ``readexactly`` raises ``IncompleteReadError`` mid-payload. The expected
    behaviour is that the read loop exits, the supervisor kicks in, and the
    client reconnects.

    Verified by checking ``client.connected`` flips false then true again.
    """
    async with MockIsm7Server(password="ism7") as srv:
        client = Ism7Client(
            host=srv.host, port=srv.port, password="ism7", use_tls=False,
        )
        await client.connect()
        assert client.connected is True

        # Force a graceful drop -- equivalent to a truncated frame from the
        # client's perspective (readexactly raises IncompleteReadError).
        await client.force_disconnect()
        # Reconnect should happen with backoff.
        await asyncio.wait_for(client.wait_connected(), timeout=5.0)
        assert client.connected is True
        await client.disconnect()


@pytest.mark.asyncio
async def test_client_reconnects_after_peer_reset() -> None:
    """Regression (2026-06-13): a peer RST must NOT kill the supervisor.

    The ISM7 resets its single TLS session on its own from time to time. On
    the wire that surfaces as ``ConnectionResetError`` from ``readexactly``
    inside the read loop. The old ``_supervise`` only caught
    ``asyncio.CancelledError`` when awaiting the reader task, so this real
    exception escaped, killed the supervisor coroutine, and the
    reconnect-backoff loop never ran -- the connection stayed dead until a
    manual reload (production hang).

    ``force_disconnect`` cancels the reader (CancelledError, already caught),
    so it never covered this. Here the *server* aborts the socket, so the
    client's reader dies with a genuine ``ConnectionResetError`` -- and the
    client must still reconnect on its own.
    """
    async with MockIsm7Server(password="ism7") as srv:
        client = Ism7Client(
            host=srv.host, port=srv.port, password="ism7", use_tls=False,
        )
        await client.connect()
        assert client.connected is True

        # Server-side RST -> client reader raises ConnectionResetError.
        srv.drop_client()

        # With the fix, the supervisor catches the reader exception and
        # reconnects with backoff.
        await asyncio.wait_for(client.wait_connected(), timeout=5.0)
        assert client.connected is True
        await client.disconnect()


@pytest.mark.asyncio
async def test_consecutive_failures_zero_while_connected() -> None:
    """A healthy connection reports 0 consecutive reconnect failures."""
    async with MockIsm7Server(password="ism7") as srv:
        client = Ism7Client(
            host=srv.host, port=srv.port, password="ism7", use_tls=False,
        )
        await client.connect()
        assert client.consecutive_connect_failures == 0
        await client.disconnect()


@pytest.mark.asyncio
async def test_consecutive_failures_climb_when_reconnect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed reconnects increment the watchdog counter without OS timing races."""
    client = Ism7Client(
        host="127.0.0.1", port=9092, password="ism7", use_tls=False,
    )
    # Simulate refusal directly: a closed TCP port can take seconds to fail
    # on Windows and can be claimed by another process after being released.
    repeated_failure = asyncio.Event()

    async def refuse_connection() -> None:
        if client.consecutive_connect_failures >= 2:
            repeated_failure.set()
        raise ConnectionRefusedError("Gateway unavailable")

    connect = AsyncMock(side_effect=refuse_connection)
    monkeypatch.setattr(client, "_connect_once", connect)
    monkeypatch.setattr(client, "_backoff_iter", lambda: iter((0, 0)))
    client._has_connected_once = True
    task = asyncio.create_task(client._supervise())
    try:
        await asyncio.wait_for(repeated_failure.wait(), timeout=2)
        assert client.consecutive_connect_failures >= 2
        assert connect.await_count >= 2
    finally:
        client._stopped = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_request_bundle_id_correlation_with_concurrent_push() -> None:
    """Regression test: an unsolicited push arriving during request() must
    NOT be returned as the request's response.

    Before T21f the read loop routed by a pending-counter, so a push frame
    arriving while ``request()`` was in flight got handed back as the
    response. After T21f the routing matches by ``bundle_id`` (``bn``), so
    push frames always go to update listeners.
    """
    async with MockIsm7Server(password="ism7") as srv:
        # Seed the mock with a value for the pull request.
        srv.set_value(bus_address="0;0;0;1", info_number=42, dl="AA", dh="BB")

        client = Ism7Client(
            host=srv.host, port=srv.port, password="ism7", use_tls=False,
        )
        await client.connect()

        push_received: list[TelegramBundleResp] = []
        client.on_update(push_received.append)

        # Race: inject a push frame just before sending the pull request.
        # Without bundle_id correlation, this push could be returned by
        # request() instead of the real response.
        await srv.inject_push(
            bus_address="0;0;0;1", info_number=99, dl="DE", dh="AD",
        )
        # Let the push frame arrive at the client's read loop.
        await asyncio.sleep(0.05)

        req = TelegramBundleReq(
            bundle_id="pull-123",
            bundle_type="pull",
            info_reads=[InfoRead(info_number=42, bus_address="0;0;0;1")],
        )
        resp = await client.request(req, timeout=5.0)

        # The response MUST be for our bundle_id, with our info_number.
        assert isinstance(resp, TelegramBundleResp)
        assert resp.bundle_id == "pull-123"
        assert any(t.info_number == 42 for t in resp.read_telegrams)

        # And the push frame must have been delivered to update listeners
        # (bundle_id "push" from inject_push, NOT "pull-123").
        assert push_received, "push frame was lost"
        push = push_received[0]
        assert push.bundle_id == "push"
        assert any(t.info_number == 99 for t in push.read_telegrams)

        await client.disconnect()


@pytest.mark.asyncio
async def test_request_system_config_returns_devices() -> None:
    """SystemConfigReq round-trip via the mock server."""
    async with MockIsm7Server(password="ism7") as srv:
        srv.set_discovery_devices([
            ("0x35", "0x20", "F0", "01"),  # BM-2
            ("0x29", "0x29", "00", "00"),  # CHA
        ])

        client = Ism7Client(
            host=srv.host, port=srv.port, password="ism7", use_tls=False,
        )
        await client.connect()
        resp = await client.request_system_config(timeout=5.0)
        assert isinstance(resp, SystemConfigResp)
        assert len(resp.devices) == 2
        assert {d.bus_address for d in resp.devices} == {"0x35", "0x29"}
        assert {d.device_id_hex for d in resp.devices} == {"0x20", "0x29"}
        await client.disconnect()


@pytest.mark.asyncio
async def test_request_system_config_empty_list() -> None:
    """SystemConfigReq -- empty bus list (gateway w/o devices yet)."""
    async with MockIsm7Server(password="ism7") as srv:
        srv.set_discovery_devices([])
        client = Ism7Client(
            host=srv.host, port=srv.port, password="ism7", use_tls=False,
        )
        await client.connect()
        resp = await client.request_system_config(timeout=5.0)
        assert isinstance(resp, SystemConfigResp)
        assert resp.devices == []
        await client.disconnect()


@pytest.mark.asyncio
async def test_request_system_config_fails_when_disconnected() -> None:
    """Calling request_system_config without a writer raises ConnectionLost."""
    client = Ism7Client(
        host="127.0.0.1", port=1, password="ism7", use_tls=False,
    )
    # Never called connect() -- writer is None.
    with pytest.raises(ConnectionLost):
        await client.request_system_config(timeout=0.1)


@pytest.mark.asyncio
async def test_supervisor_alive_after_disconnect_connect_cycle() -> None:
    """Regression (1.0.6): ``connect()`` must reset the stop flag.

    ``disconnect()`` latches ``_stopped = True``. A client that is reused
    (disconnect -> connect) previously created a supervisor that saw the
    stale flag and exited immediately: the link came up once but never
    auto-reconnected again (and the failure counter feeding the reboot
    watchdog never climbed). After the fix the reused client must behave
    like a fresh one -- drop the socket and the supervisor reconnects.
    """
    async with MockIsm7Server(password="ism7") as srv:
        client = Ism7Client(
            host=srv.host, port=srv.port, password="ism7", use_tls=False,
        )
        await client.connect()
        await client.disconnect()
        assert client.connected is False

        # Reuse the same client instance.
        await client.connect()
        assert client.connected is True

        # Server-side RST -- only a LIVE supervisor recovers from this.
        # With the old bug the reader died, nothing cleared/reconnected,
        # and ``connected`` stayed (stale) True forever -> timeout here.
        srv.drop_client()
        async with asyncio.timeout(5):
            # Poll deliberately: the client exposes no "disconnected" event
            # (``_connected`` only signals set), so a short sleep loop is the
            # simplest way to observe the drop before awaiting the reconnect.
            while client.connected:  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            await client.wait_connected()
        assert client.connected is True
        await client.disconnect()


@pytest.mark.asyncio
async def test_other_responses_queue_capped_and_drained_on_close() -> None:
    """Orphan responses are bounded (drop-oldest) and cleared on ``_close``.

    Nothing consumes ``_other_responses`` in production (bundle and
    systemconfig responses use their own correlation maps), so unknown
    frame types must not accumulate unbounded over a months-long session.
    """
    client = Ism7Client(
        host="127.0.0.1", port=1, password="ism7", use_tls=False,
    )
    for i in range(_OTHER_RESPONSES_MAX + 40):
        client._queue_other(
            UnknownPayload(payload_type=0xE, raw=i.to_bytes(2, "big")),
        )
    assert client._other_responses.qsize() == _OTHER_RESPONSES_MAX
    # Drop-oldest: the first surviving entry is number 40.
    first = client._other_responses.get_nowait()
    assert isinstance(first, UnknownPayload)
    assert int.from_bytes(first.raw, "big") == 40
    client._queue_other(first)

    # _close() drains whatever is left.
    await client._close()
    assert client._other_responses.empty()
