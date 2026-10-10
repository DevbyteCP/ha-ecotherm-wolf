# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
# Derived in part from zivillian/ism7mqtt (GPL-3.0-or-later)
"""Auto-discover the Wolf modules attached to an ISM7 gateway.

Probes the gateway with a ``SystemConfigReq`` (see ``ism7.protocol``) and
maps each reported ``busDevice`` row to a catalog ``DeviceDefinition`` so
the config flow can present friendly names. The wire response contains:

- ``ba`` -- the bus address (hex string).
- ``did`` -- the WRSDeviceId byte. Matched against
  ``DeviceDefinition.wrs_device_id``.
- ``sv`` + ``sr`` -- software major/minor; combined as
  ``major*100 + minor`` they reproduce ``DeviceTemplate/SoftwareNumber`` and
  disambiguate templates that share a WRS-id.

If no template matches we return a placeholder ``DiscoveredDevice`` with
``device_type = "unknown_dtid_<n>"`` so the user can still see the device
and decide whether to add it manually.

If the WRS-id matches one (or more) templates but the on-wire software
number does not agree, we tag the row ``device_type = "ambiguous_dtid_<wrs>"``
so the config flow can hand the user a dropdown to resolve it. Live
example: the author's ISM7 reports DID=0x15 with sw=0xFFFF; the catalog
only carries SM1 (sw=0xE3) at that WRS-id, but the device on the wire is
actually a CWL whose catalog template has ``WRSDeviceIds=0x0``.

Note: This is reverse-engineered from zivillian/ism7mqtt's
``SystemconfigResp.cs`` (which exposes the same fields) and Wolf's
``device.xml`` (the WRSDeviceIds / SoftwareNumber attributes). The
proprietary LuCon library used by Wolf's own software does the final
DTID resolution; our matcher is best-effort.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .const import DEFAULT_GATEWAY_ID
from .definitions.catalog import Catalog, DeviceDefinition
from .ism7.client import Ism7Client
from .ism7.protocol import (
    BundleType,
    InfoRead,
    TelegramBundleReq,
    TelegramBundleResp,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DiscoveredDevice:
    """One row from the ISM7's bus-device list, enriched from the catalog."""

    bus_address: str          # "0x35"
    device_type: str          # "CHA", "unknown_dtid_<id>", or "ambiguous_dtid_<wrs>"
    dtid: int                 # 270000, or 0 for unknowns / ambiguous
    friendly_name: str        # "CHA -- Wärmepumpe" or similar
    wrs_device_id: str = ""   # "0x29"
    software_number: str = "" # "0xF001"
    firmware_version: str = "" # "00.01"
    serial: str = ""          # currently always "" -- not in the wire frame
    # Populated only for ``ambiguous_dtid_*`` rows. Lists the device_types the
    # user could plausibly resolve to: WRS-id candidates first, then catalog
    # templates whose WRSDeviceIds is empty/0x0 (so the user can pick a CWL
    # at DID 0x15 etc.). Empty for unambiguous rows.
    candidates: list[str] = field(default_factory=list)

    # Back-compat alias for older callers that read ``device_id``.
    @property
    def device_id(self) -> int:
        return self.dtid


@dataclass(slots=True)
class SubmoduleCandidate:
    """A logical sub-module the config flow may offer to add.

    Logical sub-modules (warm-water system, mixer circuit, solar control)
    don't get their own ``busDevice`` row in the SystemConfigResp -- they
    share their parent's physical module, addressed by a different bus
    address and/or eBus service. ``probe_submodules`` builds one of these
    per (discovered parent, curated sub-module) pair; ``present`` is
    ``True`` when a one-shot bus probe actually got an answer.
    """

    device_type: str          # "WWSystem_BM-2"
    dtid: int                 # 350000
    bus_address: str          # "0x35" -- resolved read address to probe/use
    parent_device_type: str   # "BM-2" -- the discovered module it hangs off
    parent_bus_address: str   # "0x35" -- the parent's bus (stable join key)
    present: bool             # bus probe got an st="ok" answer


# Curated parent -> sub-module map. Keyed by the *parent* DeviceTemplate
# id; each entry is ``(sub_module_dtid, bus_rule)`` where ``bus_rule`` is
# either the literal ``"parent"`` (the sub-module sits on the parent's own
# bus address, distinguished only by its eBus service) or an explicit hex
# bus address. Sourced from the author's ism7-parameters-Wolf.json and the
# add_more step's documented typical addresses.
_SUBMODULE_CANDIDATES: dict[int, list[tuple[int, str]]] = {
    220000: [  # BM-2
        (350000, "parent"),  # WWSystem_BM-2 -- DHW control, parent's bus
        (360000, "0x85"),    # MK_BM-2 -- mixer circuit
        (260000, "parent"),  # ZirkSol_BM-2 -- solar control, parent's bus
    ],
}


@dataclass(slots=True)
class _MatchKey:
    """Helper -- normalise hex strings for comparison."""

    wrs: int
    sw_number: int | None  # None == any (no software disambiguation)


async def discover_devices(
    client: Ism7Client,
    catalog: Catalog,
    *,
    timeout: float = 10.0,  # noqa: ASYNC109
) -> list[DiscoveredDevice]:
    """Query the ISM7 for the bus-device list and map to catalog templates."""
    resp = await client.request_system_config(
        sid=DEFAULT_GATEWAY_ID, timeout=timeout,
    )
    result = _map_response(resp.devices, catalog)
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "Discovery: sid=%r bus_type=%r raw=%d mapped=%s",
            resp.sid, resp.bus_type, len(resp.devices),
            [(r.device_type, r.bus_address, r.wrs_device_id, r.software_number)
             for r in result],
        )
    return result


# Marker so config flow can detect that discovery is wired up. The stub
# version raised NotImplementedError and was guarded by this attribute.
discover_devices.implemented = True  # type: ignore[attr-defined]


def _effective_parent_type(dev: DiscoveredDevice) -> str | None:
    """The device_type to look up sub-module candidates for.

    For a cleanly-resolved row that's just ``device_type``. For an
    *ambiguous* row (``ambiguous_dtid_*`` / ``unknown_dtid_*``) the real
    type isn't picked until the resolve step -- but its WRS-id best guess
    (``candidates[0]``, e.g. "BM-2") is almost always right and is enough
    to know which sub-modules to probe. This is what lets the probe run
    *during discovery* (parents still ambiguous) instead of needing a
    second connection after resolution.
    """
    if dev.device_type.startswith(("ambiguous_dtid_", "unknown_dtid_")):
        return dev.candidates[0] if dev.candidates else None
    return dev.device_type


async def probe_submodules(
    client: Ism7Client,
    catalog: Catalog,
    discovered: list[DiscoveredDevice],
    *,
    timeout: float = 8.0,  # noqa: ASYNC109
) -> list[SubmoduleCandidate]:
    """Probe the bus for logical sub-modules of the discovered devices.

    Runs on the SAME open connection as discovery (the caller hands us the
    live ``client`` right after ``discover_devices``) -- no second connect.
    That matters: the ISM7 allows a single TLS session and is slow to free
    it, so opening a fresh connection mid-flow stalls. Reusing the
    discovery connection sidesteps that entirely.

    Each discovered physical device is mapped to its sub-module candidates
    via ``_effective_parent_type`` (best-guess for still-ambiguous rows),
    then ``_SUBMODULE_CANDIDATES``. Sub-modules don't get their own
    ``busDevice`` row; for each we send a one-shot read of one of its
    telegrams and treat ``st="ok"`` as "present" (the gateway answers
    ``st="to"`` -- eBus timeout -- when nothing lives there).

    Best-effort: any failure leaves ``present=False`` so the flow still
    offers the candidate (unchecked). Never raises. De-duplicated on
    ``(device_type, bus_address)``.
    """
    out: list[SubmoduleCandidate] = []
    seen: set[tuple[str, str]] = set()
    probe_no = 0
    for dev in discovered:
        parent_type = _effective_parent_type(dev)
        if parent_type is None:
            continue
        parent_dev = catalog.devices.get(parent_type)
        if parent_dev is None:
            continue
        parent_bus = dev.bus_address
        for sub_dtid, bus_rule in _SUBMODULE_CANDIDATES.get(
            parent_dev.dtid, [],
        ):
            sub = _device_by_dtid(catalog, sub_dtid)
            if sub is None:
                continue
            sub_bus = parent_bus if bus_rule == "parent" else bus_rule
            if not sub_bus:
                continue
            key = (sub.device_type, sub_bus)
            if key in seen:
                continue
            seen.add(key)
            probe_no += 1
            # Numeric bundle id -- the firmware only provably accepts
            # stringified ints (same as every other bundle on the wire).
            # The discovery connection hasn't used any bundle id yet
            # (SystemConfigReq is type-correlated), so 1.. can't collide.
            present = await _probe_one(
                client, sub, sub_bus, str(probe_no), timeout,
            )
            _LOGGER.debug(
                "Sub-module probe %s@%s (parent %s@%s): present=%s",
                sub.device_type, sub_bus, parent_type, parent_bus, present,
            )
            out.append(SubmoduleCandidate(
                device_type=sub.device_type,
                dtid=sub.dtid,
                bus_address=sub_bus,
                parent_device_type=parent_type,
                parent_bus_address=parent_bus,
                present=present,
            ))
    return out


async def _probe_one(
    client: Ism7Client,
    sub: DeviceDefinition,
    bus_address: str,
    bundle_id: str,
    timeout: float,  # noqa: ASYNC109
) -> bool:
    """Send one read for a ``sub`` telegram; True if the bus answers ``st=ok``."""
    probe = _probe_inforead(sub, bus_address)
    if probe is None:
        return False
    try:
        resp = await client.request(
            TelegramBundleReq(
                bundle_id=bundle_id,
                bundle_type=BundleType.PULL,
                abort_on_error=False,
                info_reads=[probe],
            ),
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001 -- probe is best-effort, never raise
        _LOGGER.debug(
            "Sub-module probe %s@%s failed: %s", sub.device_type, bus_address, e,
        )
        return False
    if not isinstance(resp, TelegramBundleResp):
        return False
    # The gateway answers st="to" (eBus timeout) when nothing lives at the
    # address/service. A real module answers st="ok" -- even if the value
    # itself is 0xFFFF (uninitialised), the device is present.
    return any(t.state == "ok" for t in resp.read_telegrams)


def _probe_inforead(
    sub: DeviceDefinition, bus_address: str,
) -> InfoRead | None:
    """Pick one telegram of ``sub`` to probe with (first converter-backed param).

    Carries the parameter's ``ServiceReadNumber`` as ``snr`` -- without it
    the gateway answers 0xFFFF for service-gated telegrams even when the
    module is present, which would make the probe a false negative.
    """
    for param in sub.parameters.values():
        converter = param.converter
        if converter is None or not converter.info_numbers:
            continue
        snr = converter.service_read_number
        return InfoRead(
            info_number=converter.info_numbers[0],
            bus_address=bus_address,
            service_number=snr if snr is not None else -1,
        )
    return None


def _device_by_dtid(catalog: Catalog, dtid: int) -> DeviceDefinition | None:
    """Find a catalog device by its DeviceTemplate id (no index -- linear)."""
    return next(
        (d for d in catalog.devices.values() if d.dtid == dtid), None,
    )


def _map_response(
    bus_devices: list,  # type: ignore[type-arg]
    catalog: Catalog,
) -> list[DiscoveredDevice]:
    """Translate ``DiscoveredBusDevice`` rows into ``DiscoveredDevice``.

    Public for tests; ``discover_devices`` is the only intended caller.
    """
    index = _build_wrs_index(catalog)
    unindexed = _unindexed_devices(catalog, index)
    out: list[DiscoveredDevice] = []
    for raw in bus_devices:
        bus_address = raw.bus_address
        did_hex = raw.device_id_hex or ""
        sv = raw.software_version_hex
        sr = raw.software_revision_hex
        sw_number = raw.software_number  # e.g. "0xF001"
        match, ambiguous = _lookup(index, did_hex, sw_number)
        if match is None:
            wrs_int = _safe_int(did_hex)
            dt_label = (
                f"unknown_dtid_{wrs_int:#x}" if wrs_int
                else f"unknown_dtid_{did_hex or '0'}"
            )
            friendly = (
                f"Unbekannt (DID {did_hex})" if did_hex else "Unbekannt"
            )
            out.append(
                DiscoveredDevice(
                    bus_address=bus_address,
                    device_type=dt_label,
                    dtid=0,
                    friendly_name=friendly,
                    wrs_device_id=did_hex,
                    software_number=sw_number,
                    firmware_version=_format_fw(sv, sr),
                )
            )
            continue

        if ambiguous:
            wrs_int = _safe_int(did_hex)
            dt_label = (
                f"ambiguous_dtid_{wrs_int:#x}" if wrs_int
                else f"ambiguous_dtid_{did_hex or '0'}"
            )
            # Candidates: WRS-id matches first (our best guesses), then
            # unindexed templates so the user can pick a CWL etc.
            # De-duplicated; catalog order preserved within each bucket.
            wrs_candidates = [d.device_type for d in index.by_wrs.get(
                _safe_int(did_hex) or 0, [],
            )]
            seen: set[str] = set(wrs_candidates)
            extra = [d.device_type for d in unindexed if d.device_type not in seen]
            candidate_list = [*wrs_candidates, *extra]
            friendly = (
                f"Mehrdeutig: {match.device_type} oder Variante? "
                f"(DID {did_hex}, sw {sw_number})"
            )
            # A CWL ventilation unit reports as DID 0x15 with its firmware
            # blanked to 0xFF/0xFF -- the resolve-step would otherwise
            # default to SM1 (the WRS-id match). Promote a CWL template to
            # the front of the candidates so the dropdown defaults to it.
            if _looks_like_cwl(did_hex, sv, sr):
                cwl = next(
                    (c for c in candidate_list if c == "CWL"),
                    next(
                        (c for c in candidate_list if c.startswith("CWL")),
                        None,
                    ),
                )
                if cwl is not None:
                    candidate_list = [
                        cwl, *(c for c in candidate_list if c != cwl),
                    ]
                    friendly = (
                        f"Vermutlich CWL – bitte bestätigen "
                        f"(DID {did_hex}, Firmware nicht gemeldet)"
                    )
            out.append(
                DiscoveredDevice(
                    bus_address=bus_address,
                    device_type=dt_label,
                    dtid=0,
                    friendly_name=friendly,
                    wrs_device_id=did_hex,
                    software_number=sw_number,
                    firmware_version=_format_fw(sv, sr),
                    candidates=candidate_list,
                )
            )
            continue

        out.append(
            DiscoveredDevice(
                bus_address=bus_address,
                device_type=match.device_type,
                dtid=match.dtid,
                friendly_name=_format_friendly(match),
                wrs_device_id=did_hex,
                software_number=sw_number,
                firmware_version=_format_fw(sv, sr),
            )
        )
    return out


@dataclass(slots=True)
class _CatalogIndex:
    """WRS-id keyed lookup table. One entry per template, since Wolf's
    catalog occasionally reuses a WRS-id across variants."""

    by_wrs: dict[int, list[DeviceDefinition]] = field(default_factory=dict)


def _build_wrs_index(catalog: Catalog) -> _CatalogIndex:
    index = _CatalogIndex()
    for dev in catalog.devices.values():
        if not dev.wrs_device_id:
            continue
        wrs = _safe_int(dev.wrs_device_id)
        # Treat 0x0 as "not declared" -- many CWL/CWL-2 templates carry that
        # placeholder and would otherwise pollute the index.
        if wrs is None or wrs == 0:
            continue
        index.by_wrs.setdefault(wrs, []).append(dev)
    return index


def _unindexed_devices(
    catalog: Catalog, index: _CatalogIndex,
) -> list[DeviceDefinition]:
    """Catalog devices with no usable WRSDeviceIds entry.

    These can never be auto-discovered -- CWL/CWL-2/CWL-Excellent, mixer and
    warm-water submodules all live here. We expose them to the user as
    candidates whenever discovery flags an ambiguous row.
    """
    indexed_dtids: set[int] = {
        d.dtid for cands in index.by_wrs.values() for d in cands
    }
    return [
        d for d in catalog.devices.values()
        if d.dtid not in indexed_dtids
    ]


def _lookup(
    index: _CatalogIndex,
    did_hex: str,
    sw_number_hex: str,
) -> tuple[DeviceDefinition | None, bool]:
    """Return ``(match, ambiguous)``.

    - ``(None, False)`` -- no template matches the WRS-id at all.
    - ``(dev, False)`` -- exactly one template at the WRS-id, or several with
      one whose ``SoftwareNumber`` agrees with the live value.
    - ``(dev, True)`` -- best-guess template, but software_number disagrees
      (or several templates compete and none matches). The caller surfaces
      this to the user as an "ambiguous_dtid_*" row.
    """
    wrs = _safe_int(did_hex)
    if wrs is None:
        return None, False
    candidates = index.by_wrs.get(wrs, [])
    if not candidates:
        return None, False
    target = _safe_int(sw_number_hex)
    if len(candidates) == 1:
        only = candidates[0]
        only_sw = _safe_int(only.software_number)
        # Single candidate: trust the WRS-id match when either side has no
        # software_number declared, or when both sides agree. Otherwise the
        # match is at best an educated guess -- flag as ambiguous so the user
        # can pick a different module type (e.g. CWL at DID 0x15).
        if only_sw in (None, 0) or target in (None, 0) or only_sw == target:
            return only, False
        return only, True
    # Several templates at the same WRS-id. Tie-break by software_number.
    if target is not None and target != 0:
        for c in candidates:
            cs = _safe_int(c.software_number)
            if cs is not None and cs == target:
                return c, False
    # No software-number tie-break possible -- caller treats it as ambiguous.
    return candidates[0], True


def _looks_like_cwl(did_hex: str, sv: str, sr: str) -> bool:
    """True for the on-wire signature of a Wolf CWL ventilation unit.

    A CWL reports as DID 0x15 -- the WRS-id Wolf assigns to the SM1 solar
    module -- but with its firmware fields blanked to 0xFF/0xFF. A real
    SM1 reports an actual version, so "DID 0x15 + firmware not reported"
    is a reliable "this is a CWL, not an SM1" tell. The author's CWL on
    bus 0x7C is exactly this case.
    """
    return (
        _safe_int(did_hex) == 0x15
        and _safe_int(sv) == 0xFF
        and _safe_int(sr) == 0xFF
    )


def _safe_int(s: str | None) -> int | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return int(s, 16)
    except ValueError:
        try:
            return int(s)
        except ValueError:
            return None


def _format_friendly(dev: DeviceDefinition) -> str:
    """``"CHA -- Wärmepumpe"`` (display_name is often == device_type)."""
    if dev.display_name and dev.display_name != dev.device_type:
        return f"{dev.device_type} -- {dev.display_name}"
    return dev.device_type


def _format_fw(sv: str, sr: str) -> str:
    """Format the software version as ``"<major>.<minor>"``."""
    major = _safe_int(sv)
    minor = _safe_int(sr)
    if major is None and minor is None:
        return ""
    return f"{major or 0}.{minor or 0:02d}"
