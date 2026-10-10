"""Tests for the Wolf ISM7 frame codec."""
from __future__ import annotations

import struct

import pytest

from custom_components.ecotherm_wolf.ism7.exceptions import ProtocolError
from custom_components.ecotherm_wolf.ism7.protocol import (
    MAX_FRAME_PAYLOAD_BYTES,
    DirectLogonReq,
    DirectLogonResp,
    DiscoveredBusDevice,
    InfoRead,
    InfoWrite,
    KeepAliveReq,
    KeepAliveResp,
    PayloadType,
    SystemConfigReq,
    SystemConfigResp,
    TelegramBundleReq,
    TelegramBundleResp,
    UnknownPayload,
    decode_frame_header,
    decode_payload,
    encode_frame,
)


class TestEncodeDirectLogon:
    def test_login_request_serialises(self):
        frame = encode_frame(DirectLogonReq(password="ism7"))
        # Header: [4-byte len][2-byte type=0x0008]
        assert frame[:4] == struct.pack(">I", len(frame) - 6)
        assert frame[4:6] == struct.pack(">H", PayloadType.DIRECT_LOGON_REQ)
        # Payload: <direct-logon-request><password>ism7</password></direct-logon-request>
        payload = frame[6:]
        assert b"<direct-logon-request" in payload
        assert b"<password>ism7</password>" in payload


class TestDecodeDirectLogonResp:
    def test_login_ok(self):
        payload = b'<direct-logon-response state="ok" sid="abc-123"/>'
        msg = decode_payload(PayloadType.DIRECT_LOGON_RESP, payload)
        assert isinstance(msg, DirectLogonResp)
        assert msg.state == "ok"
        assert msg.session_id == "abc-123"

    def test_login_error(self):
        payload = b'<direct-logon-response state="error"/>'
        msg = decode_payload(PayloadType.DIRECT_LOGON_RESP, payload)
        assert isinstance(msg, DirectLogonResp)
        assert msg.state == "error"
        assert msg.session_id is None

    def test_invalid_xml_raises(self):
        with pytest.raises(ProtocolError):
            decode_payload(PayloadType.DIRECT_LOGON_RESP, b"not xml at all")

    def test_wrong_root_raises(self):
        with pytest.raises(ProtocolError):
            decode_payload(PayloadType.DIRECT_LOGON_RESP, b"<other/>")


class TestKeepAlive:
    def test_request_serialises_2_bytes(self):
        frame = encode_frame(KeepAliveReq(seq=42))
        assert frame[:4] == struct.pack(">I", 2)  # length = 2
        assert frame[4:6] == struct.pack(">H", PayloadType.KEEP_ALIVE)
        assert frame[6:] == struct.pack(">h", 42)

    def test_response_decodes(self):
        msg = decode_payload(PayloadType.KEEP_ALIVE, struct.pack(">h", 42))
        assert isinstance(msg, KeepAliveResp)
        assert msg.seq == 42

    def test_response_with_negative_seq(self):
        # Sequence is signed 16-bit.
        msg = decode_payload(PayloadType.KEEP_ALIVE, struct.pack(">h", -1))
        assert isinstance(msg, KeepAliveResp)
        assert msg.seq == -1

    def test_invalid_payload_size_raises(self):
        with pytest.raises(ProtocolError):
            decode_payload(PayloadType.KEEP_ALIVE, b"\x00\x00\x00")


class TestTgrBundleReq:
    def test_subscribe_with_one_read(self):
        req = TelegramBundleReq(
            bundle_id="42",
            bundle_type="push",
            info_reads=[
                InfoRead(info_number=10000, bus_address="0;0;0;1", interval=60),
            ],
        )
        frame = encode_frame(req)
        # Frame structure
        assert frame[:4] == struct.pack(">I", len(frame) - 6)
        assert frame[4:6] == struct.pack(">H", PayloadType.TGR_BUNDLE_REQ)
        payload = frame[6:]
        # Required tbreq attrs
        assert b'bn="42"' in payload
        assert b'gw="1"' in payload
        assert b'ae="true"' in payload
        assert b'ty="push"' in payload
        # The single ird child
        assert b'<ird ' in payload
        assert b'in="10000"' in payload
        assert b'ba="0;0;0;1"' in payload
        assert b'is="60"' in payload

    def test_pull_once_omits_interval(self):
        req = TelegramBundleReq(
            bundle_id="1",
            bundle_type="pull",
            info_reads=[InfoRead(info_number=10000, bus_address="0;0;0;1")],
        )
        frame = encode_frame(req)
        payload = frame[6:]
        # interval=-1 means omit the 'is' attr.
        assert b' is=' not in payload

    def test_write_with_one_iwr(self):
        req = TelegramBundleReq(
            bundle_id="43",
            bundle_type="write",
            info_writes=[
                InfoWrite(
                    info_number=10001,
                    bus_address="0;0;0;1",
                    db_low="14",
                    db_high="00",
                ),
            ],
        )
        frame = encode_frame(req)
        payload = frame[6:]
        assert b'ty="write"' in payload
        assert b'<iwr ' in payload
        assert b'dl="14"' in payload
        assert b'dh="00"' in payload

    def test_abort_on_error_false(self):
        req = TelegramBundleReq(
            bundle_id="1",
            bundle_type="pull",
            abort_on_error=False,
        )
        frame = encode_frame(req)
        payload = frame[6:]
        assert b'ae="false"' in payload


class TestTgrBundleResp:
    def test_decode_with_reads(self):
        payload = (
            b'<tbres ts="2026-01-01" gw="1" bn="42" st="ok">'
            b'<irs se="" ba="0;0;0;1" st="ok" in="10000" dl="F4" dh="01"/>'
            b'</tbres>'
        )
        msg = decode_payload(PayloadType.TGR_BUNDLE_RESP, payload)
        assert isinstance(msg, TelegramBundleResp)
        assert msg.bundle_id == "42"
        assert msg.state == "ok"
        assert msg.gateway_id == "1"
        assert len(msg.read_telegrams) == 1
        rt = msg.read_telegrams[0]
        assert rt.info_number == 10000
        assert rt.bus_address == "0;0;0;1"
        assert rt.state == "ok"
        assert rt.db_low == "F4"
        assert rt.db_high == "01"

    def test_decode_with_writes(self):
        payload = (
            b'<tbres ts="2026-01-01" gw="1" bn="42" st="ok">'
            b'<iac se="" ba="0;0;0;1" st="ok" in="10001" dl="14" dh="00"/>'
            b'</tbres>'
        )
        msg = decode_payload(PayloadType.TGR_BUNDLE_RESP, payload)
        assert isinstance(msg, TelegramBundleResp)
        assert len(msg.write_telegrams) == 1
        wt = msg.write_telegrams[0]
        assert wt.info_number == 10001
        assert wt.state == "ok"

    def test_decode_with_error(self):
        payload = b'<tbres bn="42" st="error" emsg="something broke"/>'
        msg = decode_payload(PayloadType.TGR_BUNDLE_RESP, payload)
        assert isinstance(msg, TelegramBundleResp)
        assert msg.state == "error"
        assert msg.error_msg == "something broke"

    def test_invalid_xml_raises(self):
        with pytest.raises(ProtocolError):
            decode_payload(PayloadType.TGR_BUNDLE_RESP, b"not xml at all")

    def test_wrong_root_raises(self):
        with pytest.raises(ProtocolError):
            decode_payload(PayloadType.TGR_BUNDLE_RESP, b"<wrong/>")


class TestFrameHeader:
    def test_decode_known_type(self):
        # length=10, type=0x8 (DirectLogonReq)
        header = struct.pack(">IH", 10, 0x8)
        length, ptype = decode_frame_header(header)
        assert length == 10
        assert ptype == PayloadType.DIRECT_LOGON_REQ

    def test_decode_unknown_type(self):
        # PortalLogonReqEncrypted (0x10) is in the C# enum but not in our subset.
        header = struct.pack(">IH", 5, 0x10)
        length, ptype = decode_frame_header(header)
        assert length == 5
        assert ptype == 0x10
        # decode_payload with int routes to UnknownPayload.
        msg = decode_payload(ptype, b"\x00" * 5)
        assert isinstance(msg, UnknownPayload)
        assert msg.payload_type == 0x10

    def test_invalid_header_size_raises(self):
        with pytest.raises(ProtocolError):
            decode_frame_header(b"\x00\x00")  # too short

    def test_round_trip_login(self):
        # Encode a DirectLogonReq, then decode the header from the frame.
        frame = encode_frame(DirectLogonReq(password="hunter2"))
        length, ptype = decode_frame_header(frame[:6])
        assert ptype == PayloadType.DIRECT_LOGON_REQ
        assert length == len(frame) - 6


class TestEncoderRejectsUnknown:
    def test_unsupported_outgoing_type_raises(self):
        with pytest.raises(TypeError):
            encode_frame("not a message")  # type: ignore[arg-type]


class TestSystemConfigReq:
    def test_encode_minimal(self):
        frame = encode_frame(SystemConfigReq())
        assert frame[:4] == struct.pack(">I", len(frame) - 6)
        assert frame[4:6] == struct.pack(">H", PayloadType.SYSTEMCONFIG_REQ)
        payload = frame[6:]
        # No sid attribute when empty.
        assert b"<read-systemconfig-request" in payload
        assert b"sid=" not in payload

    def test_encode_with_sid(self):
        frame = encode_frame(SystemConfigReq(sid="gw-1"))
        payload = frame[6:]
        assert b'sid="gw-1"' in payload


class TestSystemConfigResp:
    def test_decode_two_bus_devices(self):
        payload = (
            b'<read-systemconfig-response sid="gw-1">'
            b'<busconfig type="ebus">'
            b'<busDevices>'
            b'<busDevice ba="0x35" sv="F0" sr="01" cfg="0;0;0;1" did="0x20"/>'
            b'<busDevice ba="0x29" sv="00" sr="00" cfg="" did="0x29"/>'
            b'</busDevices>'
            b'</busconfig>'
            b'</read-systemconfig-response>'
        )
        msg = decode_payload(PayloadType.SYSTEMCONFIG_RESP, payload)
        assert isinstance(msg, SystemConfigResp)
        assert msg.sid == "gw-1"
        assert msg.bus_type == "ebus"
        assert len(msg.devices) == 2

        bm2 = msg.devices[0]
        assert isinstance(bm2, DiscoveredBusDevice)
        assert bm2.bus_address == "0x35"
        assert bm2.device_id_hex == "0x20"
        assert bm2.software_version_hex == "F0"
        assert bm2.software_revision_hex == "01"
        # major=0xF0 = 240, minor=0x01 = 1; major*100 + minor = 24001 = 0x5DC1.
        assert bm2.software_number == "0x5DC1"

        cha = msg.devices[1]
        assert cha.bus_address == "0x29"
        assert cha.device_id_hex == "0x29"

    def test_decode_empty_buslist(self):
        payload = (
            b'<read-systemconfig-response sid="x">'
            b'<busconfig type="ebus"><busDevices/></busconfig>'
            b'</read-systemconfig-response>'
        )
        msg = decode_payload(PayloadType.SYSTEMCONFIG_RESP, payload)
        assert isinstance(msg, SystemConfigResp)
        assert msg.devices == []

    def test_decode_missing_wrapper_tolerated(self):
        # Defensive: real firmware always wraps in <busconfig>, but if not we
        # should still find <busDevice> children.
        payload = (
            b'<read-systemconfig-response sid="x">'
            b'<busDevice ba="0x35" did="0x20"/>'
            b'</read-systemconfig-response>'
        )
        msg = decode_payload(PayloadType.SYSTEMCONFIG_RESP, payload)
        assert isinstance(msg, SystemConfigResp)
        assert len(msg.devices) == 1
        assert msg.devices[0].bus_address == "0x35"

    def test_decode_invalid_xml_raises(self):
        with pytest.raises(ProtocolError):
            decode_payload(PayloadType.SYSTEMCONFIG_RESP, b"<<<")

    def test_decode_wrong_root_raises(self):
        with pytest.raises(ProtocolError):
            decode_payload(PayloadType.SYSTEMCONFIG_RESP, b"<other/>")

    def test_software_number_fallback_on_bad_hex(self):
        dev = DiscoveredBusDevice(
            bus_address="0x35",
            device_id_hex="0x20",
            software_version_hex="ZZ",
            software_revision_hex="",
        )
        assert dev.software_number == "0x0"


class TestFramePartial:
    """Defensive parsing of incomplete / truncated frames.

    The TCP stream-reader (``Ism7Client._read_loop``) reads the 6-byte header
    first, then ``length`` body bytes. If only the header arrives but the body
    is cut, that read raises ``IncompleteReadError`` at the stream layer --
    long before ``decode_payload`` ever sees the bytes. From the codec's
    perspective, however, a truncated XML body that *does* reach
    ``decode_payload`` must surface as ``ProtocolError`` rather than a
    generic crash. This test pins that contract.
    """

    def test_truncated_body_raises_protocol_error(self):
        # Valid header advertising a 100-byte body, then a body that's only
        # the first 20 bytes of an unterminated <direct-logon-response ...
        payload = b"<direct-logon-response state="
        with pytest.raises(ProtocolError):
            decode_payload(PayloadType.DIRECT_LOGON_RESP, payload)

    def test_header_only_decodes_length_correctly(self):
        # If we have only the header (body not yet on the wire), the codec
        # must still parse the length so the reader can wait for the body.
        header = struct.pack(">IH", 250, 0x4)  # 250-byte body, TGR_BUNDLE_REQ
        length, ptype = decode_frame_header(header)
        assert length == 250
        assert ptype == PayloadType.TGR_BUNDLE_REQ


class TestFrameLengthCap:
    """The uint32 length field is sanity-capped.

    A desynced stream (or corrupt header) can advertise up to 4 GiB -- an
    uncapped ``readexactly(length)`` would buffer gigabytes and stall the
    read loop until the stale watchdog fires. The decoder must reject such
    lengths outright so the connection tears down and re-syncs instead.
    """

    def test_oversized_length_raises_protocol_error(self):
        header = struct.pack(">IH", MAX_FRAME_PAYLOAD_BYTES + 1, 0x5)
        with pytest.raises(ProtocolError, match="sanity cap"):
            decode_frame_header(header)

    def test_length_at_cap_is_accepted(self):
        header = struct.pack(">IH", MAX_FRAME_PAYLOAD_BYTES, 0x5)
        length, ptype = decode_frame_header(header)
        assert length == MAX_FRAME_PAYLOAD_BYTES
        assert ptype == PayloadType.TGR_BUNDLE_RESP
