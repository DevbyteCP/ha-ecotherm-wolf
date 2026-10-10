# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
# Derived in part from zivillian/ism7mqtt (GPL-3.0-or-later)
"""Async TCP client for ISM7 with auto-reconnect."""
from __future__ import annotations

import asyncio
import logging
import ssl
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

from custom_components.ecotherm_wolf.const import (
    CONNECT_TIMEOUT_SECONDS,
    RECONNECT_BACKOFF,
)

from .exceptions import AuthenticationError, ConnectionLost
from .protocol import (
    DirectLogonReq,
    DirectLogonResp,
    Incoming,
    KeepAliveResp,
    Outgoing,
    PayloadType,
    SystemConfigReq,
    SystemConfigResp,
    TelegramBundleReq,
    TelegramBundleResp,
    decode_frame_header,
    decode_payload,
    encode_frame,
)

_LOGGER = logging.getLogger(__name__)


# Update listeners now receive a TelegramBundleResp (the new value-update
# carrier in the Wolf protocol). T21d wires up the actual subscribe-flow
# that produces these.
UpdateListener = Callable[[TelegramBundleResp], None]


CLIENT_CERT_PATH = Path(__file__).parent / "client.pfx"
SNI_HOSTNAME = "ism7.server"

# Cap on the ``_other_responses`` queue. Production never consumes it (the
# login handshake runs before the read loop starts; bundle + systemconfig
# responses use their own correlation maps), so unknown payload types /
# orphan responses would otherwise accumulate unbounded over a months-long
# session. Beyond the cap the OLDEST entry is dropped.
_OTHER_RESPONSES_MAX = 64


def _extract_pkcs12(pfx_path: Path) -> tuple[bytes, bytes]:
    """Extract certificate and private key from a PKCS#12 file with empty password.

    Returns (cert_pem_bytes, key_pem_bytes).
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    pfx_data = pfx_path.read_bytes()
    private_key, certificate, _additional_certs = pkcs12.load_key_and_certificates(
        pfx_data, password=None,
    )
    if private_key is None or certificate is None:
        raise RuntimeError(f"failed to extract cert/key from {pfx_path}")

    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def _make_ssl_context_sync(cert_path: Path = CLIENT_CERT_PATH) -> ssl.SSLContext:
    """SSL context for the ISM7 (synchronous; blocking file I/O).

    Wolf's ISM7 uses TLS 1.2 with RSA-AES256-SHA256 (or older RC4/SSLv3 fallbacks).
    Modern Python ssl defaults to SECLEVEL=2 which excludes these. Lower to 0.
    The server cert is self-signed by Wolf's "LuCon Root CA (direct)" -- not in
    any standard CA bundle, so we intentionally disable hostname/chain verification
    (we trust the network here, not the cert).

    Use ``_make_ssl_context_async()`` from inside HA's event loop so the
    ``read_bytes`` / ``load_cert_chain`` calls run in an executor; the sync
    function is retained for the standalone smoke-test scripts.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    ctx.set_ciphers(
        "DEFAULT:@SECLEVEL=0:"
        "AES256-SHA256:"           # TLS_RSA_WITH_AES_256_CBC_SHA256 -- primary
        "RC4-SHA:RC4-MD5:"         # TLS_RSA_WITH_RC4_128_*  fallbacks
        "ECDHE-RSA-AES128-GCM-SHA256:"
        "ECDHE-RSA-AES256-GCM-SHA384:"
        "ECDHE-RSA-AES128-SHA256:"
        "ECDHE-RSA-AES256-SHA384"
    )

    # Intentionally unsafe: ISM7 server cert is self-signed; the device sits on
    # the local network and we trust the LAN, not the cert chain.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # Allow TLS 1.2 (the realistic minimum). Some older ISM7 firmware might need
    # SSLv3, but most Python builds have SSLv3 disabled at compile time.
    ctx.minimum_version = ssl.TLSVersion.TLSv1

    # Client certificate -- Wolf's required PKCS#12 cert, empty password.
    # Python's ssl can't load PKCS#12 directly; extract on the fly to temp PEM
    # files (load_cert_chain only takes filepaths). On Windows we MUST close
    # the temp file handle before ssl reopens it for read, so we write the
    # PEMs into a TemporaryDirectory that auto-cleans on __exit__ -- even if
    # ``load_cert_chain`` raises between the two writes. The previous
    # two-step NamedTemporaryFile dance could leak a cert file on exception.
    cert_pem, key_pem = _extract_pkcs12(cert_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path = Path(tmpdir) / "client.pem"
        key_path = Path(tmpdir) / "client.key"
        cert_path.write_bytes(cert_pem)
        key_path.write_bytes(key_pem)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

    return ctx


async def _make_ssl_context_async(cert_path: Path = CLIENT_CERT_PATH) -> ssl.SSLContext:
    """Async wrapper that builds the SSL context in an executor.

    Required when called from HA's event loop -- ``_make_ssl_context_sync``
    does synchronous file I/O (read pfx, write temp pem files, load_cert_chain)
    which HA flags as a blocking call.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _make_ssl_context_sync, cert_path)


class Ism7Client:
    """Single-connection client. Auto-reconnects with exponential backoff.

    Public API:
        connect, disconnect, send, request, on_update,
        wait_connected, force_disconnect, connected
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        password: str,
        use_tls: bool = True,
        cert_path: Path = CLIENT_CERT_PATH,
    ) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._use_tls = use_tls
        self._cert_path = cert_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = asyncio.Event()
        self._stopped = False
        self._update_listeners: list[UpdateListener] = []
        self._reconnect_listeners: list[Callable[[], None]] = []
        self._has_connected_once: bool = False
        # Consecutive failed reconnect attempts in ``_supervise``. Reset to 0
        # by ``_connect_once`` on any successful connect; the coordinator's
        # reboot watchdog reads this to decide when to power-cycle the ISM7.
        self._consecutive_connect_failures: int = 0
        self._other_responses: asyncio.Queue[Incoming] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        # Bundle-id correlation: a TelegramBundleResp arriving from the
        # server is matched against its in-flight TelegramBundleReq via the
        # ``bundle_id`` (``bn``) attribute. This eliminates the race where
        # an unsolicited push frame arriving during a request would
        # otherwise be served back as the request's response.
        # Non-bundle responses (DirectLogonResp etc.) still use the queue.
        self._pending_bundles: dict[str, asyncio.Future[Incoming]] = {}
        # PayloadType-keyed correlation for one-shot request/response pairs
        # that don't carry a bundle_id (currently: SystemConfigReq ->
        # SystemConfigResp). There is at most one outstanding request per
        # payload type at any moment -- callers serialise themselves.
        self._pending_by_type: dict[PayloadType, asyncio.Future[Incoming]] = {}

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def consecutive_connect_failures(self) -> int:
        """Consecutive failed reconnect attempts (0 whenever connected).

        Incremented by the supervisor on each failed reconnect, reset to 0
        on any successful connect. The coordinator's reboot watchdog reads
        this to decide when the gateway needs a power-cycle.
        """
        return self._consecutive_connect_failures

    def on_update(self, callback: UpdateListener) -> None:
        self._update_listeners.append(callback)

    def on_reconnect(self, callback: Callable[[], None]) -> None:
        """Register a listener for successful reconnects (NOT called on the first connect)."""
        self._reconnect_listeners.append(callback)

    async def connect(self) -> None:
        # ``disconnect`` latches ``_stopped`` -- reset it so a reused client
        # (disconnect -> connect) gets a LIVE supervisor again. Without this
        # the freshly created supervisor task would see the stale flag and
        # exit immediately: the connection would come up once but never
        # auto-reconnect (and the failure counter driving the reboot
        # watchdog would never climb).
        self._stopped = False
        await self._connect_once()
        self._supervisor_task = asyncio.create_task(self._supervise())

    async def disconnect(self) -> None:
        self._stopped = True
        if self._supervisor_task:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except (asyncio.CancelledError, Exception):
                # Defensive: shutdown errors should not propagate.
                pass
            self._supervisor_task = None
        await self._close()

    async def force_disconnect(self) -> None:
        """Test helper: drop the socket but let supervisor reconnect."""
        await self._close()

    async def wait_connected(self) -> None:
        await self._connected.wait()

    async def send(self, msg: Outgoing) -> None:
        if not self._writer:
            raise ConnectionLost("not connected")
        self._writer.write(encode_frame(msg))
        await self._writer.drain()

    async def request(self, msg: Outgoing, *, timeout: float = 5.0) -> Incoming:  # noqa: ASYNC109
        """Send and wait for the matching response.

        For ``TelegramBundleReq`` the response is matched by ``bundle_id``
        (``bn``) so concurrent unsolicited push frames don't get confused with
        the request's response. For other request types (login etc.) the next
        non-bundle, non-keep-alive frame from the queue is returned.
        """
        if isinstance(msg, TelegramBundleReq):
            bundle_id = msg.bundle_id
            loop = asyncio.get_running_loop()
            future: asyncio.Future[Incoming] = loop.create_future()
            # If a stale future for the same bundle_id is lingering (shouldn't
            # happen with sequential bundle_ids, but be safe), cancel it.
            stale = self._pending_bundles.pop(bundle_id, None)
            if stale is not None and not stale.done():
                stale.cancel()
            self._pending_bundles[bundle_id] = future
            try:
                await self.send(msg)
                return await asyncio.wait_for(future, timeout)
            finally:
                self._pending_bundles.pop(bundle_id, None)
        # Non-bundle requests (DirectLogonReq for example) keep using the
        # queue. The login handshake currently runs before _read_loop starts
        # so this branch is exercised only by future / synthetic requests.
        await self.send(msg)
        return await asyncio.wait_for(self._other_responses.get(), timeout)

    async def request_system_config(
        self,
        *,
        sid: str = "",
        timeout: float = 10.0,  # noqa: ASYNC109
    ) -> SystemConfigResp:
        """Send a SystemConfigReq and await the matching response.

        Used by the discovery layer to enumerate the bus devices attached to
        the ISM7. Correlation is by ``PayloadType`` (SystemConfigResp has no
        bundle_id) -- only one discovery request can be in flight at a time.
        Raises ``ConnectionLost`` if the socket drops mid-request and
        ``asyncio.TimeoutError`` (wrapped by ``wait_for``) on timeout.
        """
        if not self._writer:
            raise ConnectionLost("not connected")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Incoming] = loop.create_future()
        stale = self._pending_by_type.pop(PayloadType.SYSTEMCONFIG_RESP, None)
        if stale is not None and not stale.done():
            stale.cancel()
        self._pending_by_type[PayloadType.SYSTEMCONFIG_RESP] = future
        try:
            await self.send(SystemConfigReq(sid=sid))
            resp = await asyncio.wait_for(future, timeout)
        finally:
            self._pending_by_type.pop(PayloadType.SYSTEMCONFIG_RESP, None)
        if not isinstance(resp, SystemConfigResp):
            raise ConnectionLost(
                f"unexpected response type for SystemConfigReq: {type(resp).__name__}"
            )
        return resp

    # -- internal ----------------------------------------------------------

    async def _connect_once(self) -> None:
        _LOGGER.debug(
            "Connecting to %s:%s (tls=%s)", self._host, self._port, self._use_tls,
        )
        # open_connection has no built-in timeout. Bound the TCP+TLS connect
        # so a stalled handshake (ISM7 slot still occupied) fails cleanly as
        # ConnectionLost instead of hanging the caller forever.
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                if self._use_tls:
                    ssl_context = await _make_ssl_context_async(self._cert_path)
                    reader, writer = await asyncio.open_connection(
                        self._host, self._port,
                        ssl=ssl_context,
                        server_hostname=SNI_HOSTNAME,
                    )
                else:
                    reader, writer = await asyncio.open_connection(
                        self._host, self._port,
                    )
        except (TimeoutError, OSError) as e:
            raise ConnectionLost(
                f"connect to {self._host}:{self._port} failed: {e}",
            ) from e

        # Login handshake on LOCAL reader/writer. ``self._reader`` /
        # ``self._writer`` are only assigned after the login succeeds --
        # during a supervisor reconnect, concurrent senders (the
        # coordinator's KeepAlive loop checks ``connected`` but races the
        # flag) would otherwise write into the middle of the handshake.
        try:
            writer.write(encode_frame(DirectLogonReq(password=self._password)))
            await writer.drain()
            header = await asyncio.wait_for(reader.readexactly(6), timeout=10)
            length, ptype = decode_frame_header(header)
            payload = (
                await asyncio.wait_for(reader.readexactly(length), timeout=10)
                if length
                else b""
            )
            resp = decode_payload(ptype, payload)
            if not isinstance(resp, DirectLogonResp) or resp.state != "ok":
                err = getattr(resp, "state", None) or "login failed"
                raise AuthenticationError(err)
        except asyncio.IncompleteReadError as e:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            raise ConnectionLost("connection closed during login") from e
        except BaseException:
            # If login fails for any reason, tear down the socket so we don't
            # leak the connection (and so the mock server can shut down cleanly
            # in tests).
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            raise

        self._reader = reader
        self._writer = writer
        self._connected.set()
        # A successful connect ends any run of failed reconnect attempts.
        self._consecutive_connect_failures = 0
        if self._has_connected_once:
            for cb in list(self._reconnect_listeners):
                try:
                    cb()
                except Exception:
                    _LOGGER.exception("reconnect listener failed")
        self._has_connected_once = True
        self._reader_task = asyncio.create_task(self._read_loop())
        _LOGGER.info("Connected to %s:%s", self._host, self._port)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            try:
                header = await self._reader.readexactly(6)
            except asyncio.IncompleteReadError:
                _LOGGER.debug("Read EOF -- connection lost")
                self._connected.clear()
                return
            try:
                length, ptype = decode_frame_header(header)
            except Exception:
                _LOGGER.exception("Failed to decode frame header: %r", header)
                self._connected.clear()
                return
            try:
                payload = (
                    await self._reader.readexactly(length) if length else b""
                )
            except asyncio.IncompleteReadError:
                _LOGGER.debug("Read EOF mid-payload -- connection lost")
                self._connected.clear()
                return
            try:
                msg = decode_payload(ptype, payload)
            except Exception:
                # Defensive: a single bad frame should not take down the read
                # loop. Log and continue.
                _LOGGER.exception(
                    "Failed to decode frame: ptype=%s len=%s", ptype, length,
                )
                continue
            # Routing rule (T21f, bundle_id correlation):
            # - KeepAliveResp: drop (no listener registered). KeepAlive has
            #   no bundle_id so it cannot match the pending-bundle map.
            # - TgrBundleResp: match against ``_pending_bundles`` by
            #   ``bundle_id``. If a future is waiting, hand it the message.
            #   Otherwise it's an unsolicited push -> update listeners.
            # - Everything else (DirectLogonResp, UnknownPayload): the queue.
            if isinstance(msg, KeepAliveResp):
                pass
            elif isinstance(msg, TelegramBundleResp):
                future = self._pending_bundles.get(msg.bundle_id)
                if future is not None and not future.done():
                    future.set_result(msg)
                else:
                    # Unsolicited push (subscribe deliverable) -> listeners.
                    for cb in list(self._update_listeners):
                        try:
                            cb(msg)
                        except Exception:
                            _LOGGER.exception("update listener failed")
            elif isinstance(msg, SystemConfigResp):
                # Discovery response -- match to the type-keyed pending map.
                pending = self._pending_by_type.pop(
                    PayloadType.SYSTEMCONFIG_RESP, None,
                )
                if pending is not None and not pending.done():
                    pending.set_result(msg)
                else:
                    # Unsolicited / orphan response. Queue it for any pure
                    # ``request()`` caller that's waiting on the queue.
                    self._queue_other(msg)
            else:
                self._queue_other(msg)

    def _queue_other(self, msg: Incoming) -> None:
        """Queue a non-bundle response, dropping the oldest beyond the cap.

        See ``_OTHER_RESPONSES_MAX`` -- nothing consumes this queue in
        production, so it must not grow unbounded.
        """
        while self._other_responses.qsize() >= _OTHER_RESPONSES_MAX:
            try:
                self._other_responses.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover -- single-threaded
                break
        self._other_responses.put_nowait(msg)

    async def _supervise(self) -> None:
        """Watch reader task; reconnect with backoff when it exits."""
        while not self._stopped:
            # Wait for the current reader task (set by _connect_once) to exit.
            reader_task = self._reader_task
            if reader_task is not None:
                try:
                    await reader_task
                except asyncio.CancelledError:
                    # Reader was cancelled (probably during _close). That's
                    # fine; fall through to the reconnect logic.
                    pass
                except Exception as e:  # noqa: BLE001
                    # Reader died on its own -- the COMMON disconnect case:
                    # ConnectionResetError when the ISM7 resets its single
                    # TLS session (it does this periodically), a dropped
                    # socket, or a protocol/decode error. This MUST be
                    # swallowed: if it escapes, the whole _supervise loop
                    # dies and the reconnect-backoff below never runs, so the
                    # connection stays down until a manual reload (the
                    # production hang on 2026-06-13). Catch, log, fall through
                    # to reconnect.
                    _LOGGER.info("Reader task exited (%s); reconnecting", e)
            if self._stopped:
                return
            await self._close()
            for delay in self._backoff_iter():
                if self._stopped:
                    return
                _LOGGER.info("Reconnecting in %ss", delay)
                await asyncio.sleep(delay)
                if self._stopped:
                    return
                try:
                    await self._connect_once()
                    break
                except (OSError, ConnectionLost, AuthenticationError) as e:
                    # Count consecutive failures so the coordinator's reboot
                    # watchdog can power-cycle the gateway after enough of
                    # them. ``_connect_once`` zeroes this on the next success.
                    self._consecutive_connect_failures += 1
                    _LOGGER.warning(
                        "Reconnect failed (attempt %d): %s",
                        self._consecutive_connect_failures, e,
                    )

    @staticmethod
    def _backoff_iter() -> Iterator[int]:
        yield from RECONNECT_BACKOFF
        last = RECONNECT_BACKOFF[-1]
        while True:
            yield last

    async def _close(self) -> None:
        self._connected.clear()
        # Fail any in-flight bundle requests so callers don't wait for the
        # full timeout when we already know the socket is gone.
        for future in list(self._pending_bundles.values()):
            if not future.done():
                future.set_exception(ConnectionLost("connection closed"))
        self._pending_bundles.clear()
        for future in list(self._pending_by_type.values()):
            if not future.done():
                future.set_exception(ConnectionLost("connection closed"))
        self._pending_by_type.clear()
        # Drop any queued orphan responses -- they belong to the session
        # that just ended, and nothing consumes the queue in production.
        while not self._other_responses.empty():
            try:
                self._other_responses.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover -- single-threaded
                break
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                # Defensive: shutdown errors should not propagate.
                pass
            self._reader_task = None
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                # Defensive: socket close errors during shutdown are safe to
                # swallow; the connection is going away regardless.
                pass
            self._writer = None
        self._reader = None
