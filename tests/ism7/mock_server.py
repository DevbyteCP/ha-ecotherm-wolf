"""In-process TCP mock of an ISM7 gateway, speaking the framed format."""
from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable

from lxml import etree

from custom_components.ecotherm_wolf.ism7.protocol import PayloadType


class MockIsm7Server:
    """Asyncio TCP server that imitates the Wolf ISM7 wire protocol.

    Frames: [4-byte BE len][2-byte BE PayloadType][N-byte payload].

    Test API for value injection:

    - ``set_value(bus_address, info_number, dl, dh)``: store a (bus, info)
      cell's current value. Subsequent ``pull`` requests respond with it; on
      a ``push`` subscribe we emit one immediate update with the current
      value so tests don't need to wait for a real interval.
    - ``set_value_legacy(device, property_id, raw)``: back-compat shim used by
      the older reconnect test which still spoke in (device_id, property_id)
      coordinates. Maps device_id 1 to bus_address ``"0;0;0;1"``.
    - ``inject_push(bus_address, info_number, dl, dh)``: send an unsolicited
      push update from the mock to the connected client (used by tests that
      simulate value changes after subscribe).

    The ``write`` and ``remove`` bundle types are echoed with state="ok".
    """

    def __init__(self, password: str = "ism7") -> None:
        self._password = password
        self._server: asyncio.AbstractServer | None = None
        self.host: str = "127.0.0.1"
        self.port: int = 0
        # (bus_address, info_number) -> (dl, dh)
        self._values: dict[tuple[str, int], tuple[str, str]] = {}
        # Active subscriptions: list of (bus_address, info_number) per writer.
        self._subscriptions: dict[
            asyncio.StreamWriter, set[tuple[str, int]]
        ] = {}
        # Most recent connected writer -- for inject_push.
        self._last_writer: asyncio.StreamWriter | None = None
        # Devices to report in response to SystemConfigReq. Each entry is
        # ``(bus_address, did, sv, sr)``. Empty list -> empty response.
        self._discovery_devices: list[tuple[str, str, str, str]] = []
        # Override behaviour for SystemConfigReq -- callable returning raw
        # bytes for the response payload. Tests use this to inject errors.
        self.discovery_handler: Callable[[], bytes] | None = None

    async def __aenter__(self) -> MockIsm7Server:
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    # -- test API ---------------------------------------------------------

    def set_value(
        self,
        *,
        bus_address: str,
        info_number: int,
        dl: str,
        dh: str = "00",
    ) -> None:
        """Store the current value for a (bus_address, info_number) cell."""
        self._values[(bus_address, info_number)] = (dl.upper(), dh.upper())

    def set_value_legacy(
        self,
        *,
        device: int,
        property_id: int,
        raw: str,
    ) -> None:
        """Back-compat: device_id N -> bus_address ``"0;0;0;N"``, raw is 4-char hex.

        Used by the older reconnect test which doesn't yet speak
        (bus_address, info_number) directly. Splits the 4-char hex into
        dh+dl in big-endian order: ``"00F5"`` -> dh="00", dl="F5".
        """
        bus = f"0;0;0;{device}"
        if len(raw) == 4:
            dh = raw[0:2]
            dl = raw[2:4]
        elif len(raw) == 2:
            dh = "00"
            dl = raw
        else:
            raise ValueError(f"raw must be 2 or 4 hex chars, got {raw!r}")
        self.set_value(
            bus_address=bus, info_number=property_id, dl=dl, dh=dh,
        )

    def set_discovery_devices(
        self,
        devices: list[tuple[str, str, str, str]],
    ) -> None:
        """Pre-seed the SystemConfigReq response.

        Each tuple is ``(bus_address_hex, wrs_did_hex, sv_hex, sr_hex)``.
        """
        self._discovery_devices = list(devices)

    async def inject_push(
        self,
        *,
        bus_address: str,
        info_number: int,
        dl: str,
        dh: str = "00",
    ) -> None:
        """Send an unsolicited push update to the most recently connected client."""
        writer = self._last_writer
        if writer is None or writer.is_closing():
            return
        self.set_value(
            bus_address=bus_address, info_number=info_number, dl=dl, dh=dh,
        )
        resp = self._build_pull_response(
            bundle_id="push",
            entries=[(bus_address, info_number)],
        )
        await self._write_frame(writer, PayloadType.TGR_BUNDLE_RESP, resp)

    def drop_client(self) -> None:
        """Abruptly reset the current client connection (RST).

        The client's ``readexactly`` then raises ``ConnectionResetError``
        (NOT ``CancelledError``) -- this mirrors the ISM7 dropping its single
        TLS session on its own, the 2026-06-13 production incident. Unlike
        ``client.force_disconnect`` (which cancels the reader task), this
        exercises the path where the reader dies with a real exception, which
        used to kill the supervisor and stop all reconnects.
        """
        writer = self._last_writer
        if writer is None:
            return
        writer.transport.abort()

    # -- handler ----------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        authenticated = False
        self._subscriptions[writer] = set()
        self._last_writer = writer
        try:
            while not reader.at_eof():
                header = await reader.readexactly(6)
                length, type_value = struct.unpack(">IH", header)
                payload = await reader.readexactly(length) if length > 0 else b""

                if type_value == PayloadType.DIRECT_LOGON_REQ:
                    pw: str | None
                    try:
                        root = etree.fromstring(payload)
                        pw_elem = root.find("password")
                        pw = pw_elem.text if pw_elem is not None else None
                    except Exception:
                        pw = None
                    if pw == self._password:
                        authenticated = True
                        resp_xml = b'<direct-logon-response state="ok" sid="mock-1"/>'
                    else:
                        resp_xml = b'<direct-logon-response state="error"/>'
                    await self._write_frame(
                        writer, PayloadType.DIRECT_LOGON_RESP, resp_xml,
                    )
                elif type_value == PayloadType.KEEP_ALIVE and authenticated:
                    # Echo the seq.
                    await self._write_frame(writer, PayloadType.KEEP_ALIVE, payload)
                elif type_value == PayloadType.TGR_BUNDLE_REQ and authenticated:
                    await self._handle_bundle(writer, payload)
                elif (
                    type_value == PayloadType.SYSTEMCONFIG_REQ
                    and authenticated
                ):
                    resp = self._build_systemconfig_response()
                    await self._write_frame(
                        writer, PayloadType.SYSTEMCONFIG_RESP, resp,
                    )
                # Unknown / unauthenticated other types: silently ignore.
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            self._subscriptions.pop(writer, None)
            if self._last_writer is writer:
                self._last_writer = None
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_bundle(
        self,
        writer: asyncio.StreamWriter,
        payload: bytes,
    ) -> None:
        try:
            root = etree.fromstring(payload)
        except Exception:
            return
        bundle_id = root.attrib.get("bn", "")
        bundle_type = root.attrib.get("ty", "pull")
        info_reads = [
            (r.attrib.get("ba", ""), int(r.attrib["in"]))
            for r in root.findall("ird")
            if r.attrib.get("in", "").isdigit()
        ]
        info_writes = [
            (
                w.attrib.get("ba", ""),
                int(w.attrib["in"]),
                w.attrib.get("dl", ""),
                w.attrib.get("dh", ""),
            )
            for w in root.findall("iwr")
            if w.attrib.get("in", "").isdigit()
        ]

        if bundle_type == "pull":
            resp = self._build_pull_response(
                bundle_id=bundle_id, entries=info_reads,
            )
            await self._write_frame(writer, PayloadType.TGR_BUNDLE_RESP, resp)
        elif bundle_type == "push":
            for entry in info_reads:
                self._subscriptions[writer].add(entry)
            # Push subscribe is fire-and-forget on the client side
            # (``client.send``). Real Wolf hardware sends an
            # initial-values frame shortly after the subscribe; the mock
            # mirrors that by emitting a single read-frame with the
            # current values. The client's read loop routes it to update
            # listeners because no request() is in flight.
            push_resp = self._build_pull_response(
                bundle_id="push_initial", entries=info_reads,
            )
            await self._write_frame(
                writer, PayloadType.TGR_BUNDLE_RESP, push_resp,
            )
        elif bundle_type == "remove":
            for entry in info_reads:
                self._subscriptions[writer].discard(entry)
            await self._write_frame(
                writer, PayloadType.TGR_BUNDLE_RESP, self._build_ack(bundle_id),
            )
        elif bundle_type == "write":
            for bus, info, dl, dh in info_writes:
                self.set_value(
                    bus_address=bus, info_number=info, dl=dl, dh=dh,
                )
            resp = self._build_write_ack(
                bundle_id=bundle_id, writes=info_writes,
            )
            await self._write_frame(writer, PayloadType.TGR_BUNDLE_RESP, resp)
        else:
            await self._write_frame(
                writer, PayloadType.TGR_BUNDLE_RESP, self._build_ack(bundle_id),
            )

    def _build_systemconfig_response(self) -> bytes:
        if self.discovery_handler is not None:
            return self.discovery_handler()
        root = etree.Element("read-systemconfig-response", sid="mock-1")
        busconfig = etree.SubElement(root, "busconfig", type="ebus")
        bus_devices = etree.SubElement(busconfig, "busDevices")
        for ba, did, sv, sr in self._discovery_devices:
            etree.SubElement(
                bus_devices,
                "busDevice",
                attrib={
                    "ba": ba,
                    "sv": sv,
                    "sr": sr,
                    "cfg": "",
                    "did": did,
                },
            )
        return etree.tostring(root)  # type: ignore[no-any-return]

    def _build_ack(self, bundle_id: str) -> bytes:
        return (
            b'<tbres ts="2026-01-01T00:00:00Z" gw="1" bn="'
            + bundle_id.encode()
            + b'" st="ok"/>'
        )

    def _build_pull_response(
        self,
        *,
        bundle_id: str,
        entries: list[tuple[str, int]],
    ) -> bytes:
        root = etree.Element(
            "tbres",
            ts="2026-01-01T00:00:00Z",
            gw="1",
            bn=bundle_id,
            st="ok",
        )
        for bus, info in entries:
            dl, dh = self._values.get((bus, info), ("00", "00"))
            etree.SubElement(
                root, "irs",
                attrib={"in": str(info), "ba": bus, "st": "ok",
                        "dl": dl, "dh": dh},
            )
        return etree.tostring(root)  # type: ignore[no-any-return]

    def _build_write_ack(
        self,
        *,
        bundle_id: str,
        writes: list[tuple[str, int, str, str]],
    ) -> bytes:
        root = etree.Element(
            "tbres",
            ts="2026-01-01T00:00:00Z",
            gw="1",
            bn=bundle_id,
            st="ok",
        )
        for bus, info, dl, dh in writes:
            etree.SubElement(
                root, "iac",
                attrib={"in": str(info), "ba": bus, "st": "ok",
                        "dl": dl, "dh": dh},
            )
        return etree.tostring(root)  # type: ignore[no-any-return]

    async def _write_frame(
        self,
        writer: asyncio.StreamWriter,
        ptype: PayloadType,
        payload: bytes,
    ) -> None:
        header = struct.pack(">IH", len(payload), int(ptype))
        writer.write(header + payload)
        await writer.drain()
