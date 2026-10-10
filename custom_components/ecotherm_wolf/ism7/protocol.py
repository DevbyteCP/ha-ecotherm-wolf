# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
# Derived in part from zivillian/ism7mqtt (GPL-3.0-or-later)
"""Wolf ISM7 wire protocol -- XML frame codec.

Frame format (reverse-engineered from zivillian/ism7mqtt, GPL-3.0):
- [4 bytes: payload length, big-endian uint32]
- [2 bytes: PayloadType, big-endian uint16]
- [N bytes: UTF-8 XML payload (or 2-byte int16 seq for KeepAlive)]

Implementation notes:
- We use lxml for XML parsing/serialisation.
- Use the PayloadType enum for type dispatch (matches Wolf's values).
- Frame-decoder takes raw bytes (after TLS) and returns (PayloadType, payload).
- Frame-encoder takes a typed dataclass and returns bytes ready for the wire.

SystemConfig request/response (auto-discovery):
- Outgoing: ``<read-systemconfig-request sid="..." />`` with type 0x2
  (see ``ism7mqtt/ISM7/Protocol/SystemconfigReq.cs`` in zivillian's repo).
- Incoming: ``<read-systemconfig-response sid="...">
                <busconfig type="...">
                  <busDevices>
                    <busDevice ba="0x35" sv="00" sr="01" cfg="..." did="0x20" />
                    ...
                  </busDevices>
                </busconfig>
              </read-systemconfig-response>`` with type 0x3
  (see ``ism7mqtt/ISM7/Protocol/SystemconfigResp.cs``).
- ``did`` is the WRSDeviceId byte (e.g. ``0x29`` for CHA, ``0x20`` for BM-2),
  matching ``DeviceTemplate/WRSDeviceIds`` in Wolf's ``device.xml`` (assumption
  -- the proprietary LuCon library does the final DTID lookup and we mimic
  it via WRS-id + SoftwareNumber). ``sv``/``sr`` are software-version /
  -revision hex bytes; combined as ``major*100 + minor`` they reproduce the
  ``SoftwareNumber`` field on a template (e.g. ``0xF001`` for BM-2).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from lxml import etree

from .exceptions import ProtocolError

# Hardened parser for device-supplied XML. lxml's default parser resolves
# internal DTD entities -- a buggy or hostile gateway could send an
# entity-expansion bomb ("billion laughs") and stall the event loop. The
# ISM7 never legitimately uses DTDs or entities, so turn it all off.
_XML_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
)

# Sanity cap on the payload length advertised in a frame header. The length
# field is a uint32 (up to 4 GiB); real ISM7 frames top out at a few KB (a
# full 20-telegram pull response is ~4 KB of XML). If the stream ever
# desyncs -- or a buggy firmware writes a garbage header -- an uncapped
# ``readexactly(length)`` would buffer gigabytes and stall the read loop
# until the stale watchdog fires. Reject absurd lengths outright instead;
# the resulting ProtocolError tears the connection down and the supervisor
# reconnects with a fresh, re-synchronised stream.
MAX_FRAME_PAYLOAD_BYTES = 1_048_576  # 1 MiB -- two orders of magnitude headroom


class PayloadType(IntEnum):
    """ISM7 payload types (uint16, big-endian on the wire)."""

    PORTAL_LOGON_REQ = 0x0
    PORTAL_LOGON_RESP = 0x1
    SYSTEMCONFIG_REQ = 0x2
    SYSTEMCONFIG_RESP = 0x3
    TGR_BUNDLE_REQ = 0x4
    TGR_BUNDLE_RESP = 0x5
    ISM_CONFIG_REQ = 0x6
    ISM_CONFIG_RESP = 0x7
    DIRECT_LOGON_REQ = 0x8
    DIRECT_LOGON_RESP = 0x9
    ISM_PROTOCOL_CONFIG_REQ = 0xA
    ISM_PROTOCOL_CONFIG_RESP = 0xB
    ISM_PROTOCOL_VALUE_REQ = 0xC
    ISM_PROTOCOL_VALUE_RESP = 0xD
    BINARY_DATA = 0xE
    KEEP_ALIVE = 0xF


class BundleType(StrEnum):
    """Telegram-bundle ``ty`` attribute values.

    The wire format wants the lowercase strings -- StrEnum makes the enum
    members usable directly anywhere a ``str`` is expected (string formatting,
    XML attributes, dataclass ``str`` fields).
    """

    PULL = "pull"     # one-shot read snapshot
    PUSH = "push"     # subscribe with an Intervall for periodic updates
    WRITE = "write"   # one or more InfoWrites
    REMOVE = "remove" # unsubscribe a previous push bundle
    CLEAR = "clear"   # drop all subscriptions on this connection


# -- Outgoing messages -----------------------------------------------------


@dataclass(slots=True)
class DirectLogonReq:
    password: str

    @property
    def payload_type(self) -> PayloadType:
        return PayloadType.DIRECT_LOGON_REQ


@dataclass(slots=True)
class KeepAliveReq:
    seq: int  # signed 16-bit

    @property
    def payload_type(self) -> PayloadType:
        return PayloadType.KEEP_ALIVE


@dataclass(slots=True)
class InfoRead:
    """A single read entry inside a TelegramBundleReq."""

    info_number: int  # 'in' attr
    bus_address: str  # 'ba' attr
    seq: str = ""  # 'se' attr
    interval: int = -1  # 'is' attr; -1 = once, >=0 = subscribe interval seconds
    service_number: int = -1  # 'snr' attr; <0 = omit


@dataclass(slots=True)
class InfoWrite:
    """A single write entry inside a TelegramBundleReq."""

    info_number: int
    bus_address: str
    db_low: str  # 'dl' attr -- hex string
    db_high: str  # 'dh' attr -- hex string
    seq: str = ""
    service_number: int = -1


@dataclass(slots=True)
class TelegramBundleReq:
    bundle_id: str  # 'bn'
    gateway_id: str = "1"  # 'gw'
    abort_on_error: bool = True  # 'ae'
    # 'ty' -- see ``BundleType``. Accepts either the enum or a bare string for
    # backward-compat with callers / tests that still pass ``"pull"`` etc.
    bundle_type: BundleType | str = BundleType.PULL
    info_reads: list[InfoRead] = field(default_factory=list)
    info_writes: list[InfoWrite] = field(default_factory=list)

    @property
    def payload_type(self) -> PayloadType:
        return PayloadType.TGR_BUNDLE_REQ


@dataclass(slots=True)
class SystemConfigReq:
    """Request the ISM7 system configuration (auto-discovery)."""

    sid: str = ""  # gateway / session id; safe to omit on the LAN protocol

    @property
    def payload_type(self) -> PayloadType:
        return PayloadType.SYSTEMCONFIG_REQ


# -- Incoming messages -----------------------------------------------------


@dataclass(slots=True)
class DirectLogonResp:
    state: str  # "ok" | "error" | ...
    session_id: str | None = None


@dataclass(slots=True)
class KeepAliveResp:
    seq: int


@dataclass(slots=True)
class InfonumberReadResp:
    """Response entry for one read inside a TelegramBundleResp."""

    info_number: int
    bus_address: str
    state: str
    db_low: str
    db_high: str
    seq: str = ""
    service_number: int | None = None


@dataclass(slots=True)
class InfonumberWriteResp:
    info_number: int
    bus_address: str
    state: str
    db_low: str
    db_high: str
    seq: str = ""
    service_number: int | None = None


@dataclass(slots=True)
class TelegramBundleResp:
    bundle_id: str
    state: str
    timestamp: str = ""
    gateway_id: str = ""
    error_msg: str | None = None
    read_telegrams: list[InfonumberReadResp] = field(default_factory=list)
    write_telegrams: list[InfonumberWriteResp] = field(default_factory=list)


@dataclass(slots=True)
class DiscoveredBusDevice:
    """One row from ``<busDevices>`` in the SystemConfigResp.

    Attributes match Wolf's wire XML (see ``SystemconfigResp.BusDevice`` in
    zivillian's C# source):

    - ``bus_address``: ``ba`` -- the bus address as a hex string ("0x35").
    - ``software_version_hex``: ``sv`` -- software major version, hex byte.
    - ``software_revision_hex``: ``sr`` -- software minor version, hex byte.
    - ``configuration``: ``cfg`` -- opaque configuration blob (unused).
    - ``device_id_hex``: ``did`` -- the WRSDeviceId byte ("0x29" for CHA).

    The XML doesn't transport the DTID directly; the catalog mapper has to
    look up ``did`` in ``DeviceTemplate.WRSDeviceIds`` and optionally use
    ``software_number`` (== ``sv * 100 + sr`` as hex) for disambiguation.
    """

    bus_address: str
    device_id_hex: str
    software_version_hex: str = ""
    software_revision_hex: str = ""
    configuration: str = ""

    @property
    def software_number(self) -> str:
        """Reconstruct ``DeviceTemplate/SoftwareNumber`` (``"0xF001"``)."""
        try:
            major = int(self.software_version_hex, 16)
            minor = int(self.software_revision_hex, 16)
        except ValueError:
            return "0x0"
        number = major * 100 + minor
        return f"0x{number:X}"


@dataclass(slots=True)
class SystemConfigResp:
    """Decoded ``<read-systemconfig-response>`` frame."""

    sid: str
    bus_type: str  # ``busconfig`` ``type`` attribute (firmware-dependent)
    devices: list[DiscoveredBusDevice] = field(default_factory=list)


@dataclass(slots=True)
class UnknownPayload:
    """For payload types we don't decode (BinaryData, ProtocolConfig, etc.)."""

    payload_type: int
    raw: bytes


Outgoing = DirectLogonReq | KeepAliveReq | TelegramBundleReq | SystemConfigReq
Incoming = (
    DirectLogonResp
    | KeepAliveResp
    | TelegramBundleResp
    | SystemConfigResp
    | UnknownPayload
)


# -- Encode ----------------------------------------------------------------


def encode_frame(msg: Outgoing) -> bytes:
    """Serialize an outgoing message to a complete frame.

    Frame: [4-byte len][2-byte type][payload].
    """
    if isinstance(msg, DirectLogonReq):
        payload = _encode_direct_logon_req(msg)
    elif isinstance(msg, KeepAliveReq):
        # KeepAlive payload is 2-byte big-endian int16
        payload = struct.pack(">h", msg.seq)
    elif isinstance(msg, TelegramBundleReq):
        payload = _encode_tgr_bundle_req(msg)
    elif isinstance(msg, SystemConfigReq):
        payload = _encode_systemconfig_req(msg)
    else:
        raise TypeError(f"unsupported outgoing type: {type(msg).__name__}")

    header = struct.pack(">IH", len(payload), int(msg.payload_type))
    return header + payload


def _encode_direct_logon_req(msg: DirectLogonReq) -> bytes:
    root = etree.Element("direct-logon-request")
    pw = etree.SubElement(root, "password")
    pw.text = msg.password
    out: bytes = etree.tostring(root, xml_declaration=False, encoding="utf-8")
    return out


def _encode_systemconfig_req(msg: SystemConfigReq) -> bytes:
    """Encode the discovery request.

    The wire shape is ``<read-systemconfig-request sid="..." />``; ``sid``
    is the gateway/session id. Empty is accepted by the firmware we've
    seen (the C# reference encodes it as an empty string when uninitialised).
    """
    attrs = {"sid": msg.sid} if msg.sid else {}
    root = etree.Element("read-systemconfig-request", **attrs)
    out: bytes = etree.tostring(root, xml_declaration=False, encoding="utf-8")
    return out


def _encode_tgr_bundle_req(msg: TelegramBundleReq) -> bytes:
    root = etree.Element(
        "tbreq",
        bn=msg.bundle_id,
        gw=msg.gateway_id,
        ae="true" if msg.abort_on_error else "false",
        # ``bundle_type`` may be a BundleType enum or a bare string; coerce to
        # str so lxml accepts it as an attribute value.
        ty=str(msg.bundle_type),
    )
    for r in msg.info_reads:
        attrs = {
            "in": str(r.info_number),
            "ba": r.bus_address,
        }
        if r.seq:
            attrs["se"] = r.seq
        if r.interval >= 0:
            attrs["is"] = str(r.interval)
        if r.service_number >= 0:
            attrs["snr"] = str(r.service_number)
        etree.SubElement(root, "ird", **attrs)
    for w in msg.info_writes:
        # zivillian's iwr serialises attributes as se,ba,in,dl,dh (snr
        # optional) and ALWAYS includes ``se`` -- even as an empty string
        # (``Seq = String.Empty``). The real ISM7 firmware drops the TCP
        # connection on an iwr telegram that is missing the ``se``
        # attribute, so we always emit it (empty by default for writes).
        attrs = {
            "se": w.seq,
            "ba": w.bus_address,
            "in": str(w.info_number),
            "dl": w.db_low,
            "dh": w.db_high,
        }
        if w.service_number >= 0:
            attrs["snr"] = str(w.service_number)
        etree.SubElement(root, "iwr", **attrs)
    out: bytes = etree.tostring(root, xml_declaration=False, encoding="utf-8")
    return out


# -- Decode ----------------------------------------------------------------


def decode_frame_header(header: bytes) -> tuple[int, PayloadType | int]:
    """Decode the 6-byte frame header.

    Returns (payload_length, payload_type). If the type value does not match
    a known PayloadType, returns the raw int -- the caller can wrap it in an
    UnknownPayload when decoding the body.
    """
    if len(header) != 6:
        raise ProtocolError(f"expected 6-byte header, got {len(header)} bytes")
    length, type_value = struct.unpack(">IH", header)
    if length > MAX_FRAME_PAYLOAD_BYTES:
        raise ProtocolError(
            f"frame payload length {length} exceeds sanity cap "
            f"{MAX_FRAME_PAYLOAD_BYTES} -- stream desync or corrupt header"
        )
    if type_value in PayloadType._value2member_map_:
        return length, PayloadType(type_value)
    return length, type_value


def decode_payload(payload_type: PayloadType | int, payload: bytes) -> Incoming:
    """Decode a payload given its type.

    For XML payloads, the payload is UTF-8 XML.
    """
    if not isinstance(payload_type, PayloadType):
        return UnknownPayload(payload_type=int(payload_type), raw=payload)

    if payload_type == PayloadType.DIRECT_LOGON_RESP:
        return _decode_direct_logon_resp(payload)
    if payload_type == PayloadType.KEEP_ALIVE:
        if len(payload) != 2:
            raise ProtocolError(
                f"keep-alive payload must be 2 bytes, got {len(payload)}"
            )
        seq = struct.unpack(">h", payload)[0]
        return KeepAliveResp(seq=seq)
    if payload_type == PayloadType.TGR_BUNDLE_RESP:
        return _decode_tgr_bundle_resp(payload)
    if payload_type == PayloadType.SYSTEMCONFIG_RESP:
        return _decode_systemconfig_resp(payload)
    return UnknownPayload(payload_type=int(payload_type), raw=payload)


def _norm_state(s: str | None) -> str:
    """Normalize protocol state attribute to lowercase.

    The real ISM7 sends `state="OK"` (uppercase) on responses; our mock and
    early tests used `state="ok"` (lowercase). Normalise here so all callers
    compare against lowercase consistently.
    """
    return (s or "error").lower()


def _decode_direct_logon_resp(payload: bytes) -> DirectLogonResp:
    try:
        root = etree.fromstring(payload, parser=_XML_PARSER)
    except etree.XMLSyntaxError as e:
        raise ProtocolError(f"invalid logon response xml: {e}") from e
    if root.tag != "direct-logon-response":
        raise ProtocolError(f"expected <direct-logon-response>, got <{root.tag}>")
    return DirectLogonResp(
        state=_norm_state(root.attrib.get("state")),
        session_id=root.attrib.get("sid"),
    )


def _decode_systemconfig_resp(payload: bytes) -> SystemConfigResp:
    """Decode ``<read-systemconfig-response>``.

    XML shape (see ``SystemconfigResp.cs``):
    ``<read-systemconfig-response sid="..."><busconfig type="...">
        <busDevices>
          <busDevice ba="0x35" sv="00" sr="01" cfg="..." did="0x20" />
          ...
        </busDevices>
      </busconfig></read-systemconfig-response>``

    The ``busconfig`` wrapper is mandatory in the C# binding; we tolerate
    its absence (older firmware fragments seen in test captures).
    """
    try:
        root = etree.fromstring(payload, parser=_XML_PARSER)
    except etree.XMLSyntaxError as e:
        raise ProtocolError(f"invalid systemconfig response xml: {e}") from e
    if root.tag != "read-systemconfig-response":
        raise ProtocolError(
            f"expected <read-systemconfig-response>, got <{root.tag}>"
        )
    sid = root.attrib.get("sid", "")
    busconfig = root.find("busconfig")
    if busconfig is None:
        # Tolerate missing wrapper: scan for <busDevice> at any depth.
        bus_type = ""
        bus_devices = list(root.iter("busDevice"))
    else:
        bus_type = busconfig.attrib.get("type", "")
        bus_devices = list(busconfig.iter("busDevice"))
    devices = [
        DiscoveredBusDevice(
            bus_address=elem.attrib.get("ba", ""),
            device_id_hex=elem.attrib.get("did", ""),
            software_version_hex=elem.attrib.get("sv", ""),
            software_revision_hex=elem.attrib.get("sr", ""),
            configuration=elem.attrib.get("cfg", ""),
        )
        for elem in bus_devices
    ]
    return SystemConfigResp(sid=sid, bus_type=bus_type, devices=devices)


def _decode_tgr_bundle_resp(payload: bytes) -> TelegramBundleResp:
    try:
        root = etree.fromstring(payload, parser=_XML_PARSER)
    except etree.XMLSyntaxError as e:
        raise ProtocolError(f"invalid tbres xml: {e}") from e
    if root.tag != "tbres":
        raise ProtocolError(f"expected <tbres>, got <{root.tag}>")

    reads = [
        InfonumberReadResp(
            info_number=int(t.attrib["in"]),
            bus_address=t.attrib.get("ba", ""),
            state=_norm_state(t.attrib.get("st")),
            db_low=t.attrib.get("dl", ""),
            db_high=t.attrib.get("dh", ""),
            seq=t.attrib.get("se", ""),
            service_number=int(t.attrib["snr"])
            if "snr" in t.attrib and t.attrib["snr"].lstrip("-").isdigit()
            else None,
        )
        for t in root.findall("irs")
    ]
    writes = [
        InfonumberWriteResp(
            info_number=int(t.attrib["in"]),
            bus_address=t.attrib.get("ba", ""),
            state=_norm_state(t.attrib.get("st")),
            db_low=t.attrib.get("dl", ""),
            db_high=t.attrib.get("dh", ""),
            seq=t.attrib.get("se", ""),
            service_number=int(t.attrib["snr"])
            if "snr" in t.attrib and t.attrib["snr"].lstrip("-").isdigit()
            else None,
        )
        for t in root.findall("iac")
    ]
    return TelegramBundleResp(
        bundle_id=root.attrib.get("bn", ""),
        state=_norm_state(root.attrib.get("st")),
        timestamp=root.attrib.get("ts", ""),
        gateway_id=root.attrib.get("gw", ""),
        error_msg=root.attrib.get("emsg"),
        read_telegrams=reads,
        write_telegrams=writes,
    )
