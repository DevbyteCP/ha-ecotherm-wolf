# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Service handlers for the EcoTherm Wolf integration.

Five user-facing services:
- ``browse_property``: read one property's current value without creating an entity
- ``list_properties``: dump all available properties for a device (or all devices)
- ``rediscover_devices``: re-run discovery -- useful after the user adds new modules
- ``reload_definitions``: re-read XML definitions from disk and report a diff
- ``diagnose``: snapshot internal state for bug reports

All are response-only (``SupportsResponse.ONLY``); callers must use
``return_response=True`` to see the result.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN

if TYPE_CHECKING:
    from .runtime import EcothermWolfRuntime

_LOGGER = logging.getLogger(__name__)

BROWSE_SCHEMA = vol.Schema({
    vol.Required("device"): vol.Any(int, str),
    vol.Required("property"): vol.Any(int, str),
})

LIST_SCHEMA = vol.Schema({
    vol.Optional("device"): vol.Any(int, str),
})


def _all_runtimes(hass: HomeAssistant) -> list[EcothermWolfRuntime]:
    """Return runtime data for all configured entries (typically just one)."""
    return [
        e.runtime_data
        for e in hass.config_entries.async_entries(DOMAIN)
        if hasattr(e, "runtime_data") and e.runtime_data is not None
    ]


def _resolve_device(
    runtime: EcothermWolfRuntime, device: str | int
) -> tuple[int, str, str]:
    """Resolve a device-spec to (device_id, device_type, read_address).

    Accepts either a numeric device ID (``1``, ``"1"``) or a device-type
    string (``"CHA"``). The address returned is the device's *read* bus
    address -- the only service consuming it (``browse_property``) issues
    a read.
    """
    coordinator = runtime.coordinator
    if coordinator is None:
        raise ServiceValidationError("integration not fully initialised")
    for dev_id, (dev_type, read_address, _write) in coordinator.device_map.items():
        if str(device) == str(dev_id) or str(device) == dev_type:
            return dev_id, dev_type, read_address
    raise ServiceValidationError(f"unknown device: {device}")


def _resolve_property(
    runtime: EcothermWolfRuntime, device_type: str, prop: str | int
) -> int:
    """Resolve a property-spec to a property_id integer.

    Accepts a hex string (``"0x010C"``), an integer, or a property name
    (``"vorlauftemp"``).
    """
    if isinstance(prop, int):
        return prop
    if isinstance(prop, str) and prop.startswith("0x"):
        return int(prop, 16)
    dev = runtime.catalog.get_device(device_type)
    for p in dev.parameters.values():
        if p.name == prop:
            return p.property_id
    raise ServiceValidationError(f"unknown property: {prop}")


async def _browse_property(call: ServiceCall) -> ServiceResponse:
    """Read one property's current value without creating an entity.

    Delegates the read + decode to the coordinator's ``read_once`` so the
    result is identical to what a live entity would surface (multi-telegram
    converters included). The previous in-service codec fallback could
    disagree with the entity for parameters where the Wolf converter
    overrides a default codec.
    """
    runtimes = _all_runtimes(call.hass)
    if not runtimes:
        raise ServiceValidationError("no EcoTherm Wolf entry configured")
    runtime = runtimes[0]
    coord = runtime.coordinator
    if coord is None:
        raise ServiceValidationError("coordinator not ready")

    dev_id, dev_type, bus_address = _resolve_device(runtime, call.data["device"])
    prop_id = _resolve_property(runtime, dev_type, call.data["property"])
    param = runtime.catalog.get_parameter(dev_type, prop_id)

    value: Any = None
    if bus_address:
        try:
            results = await coord.read_once([(dev_id, prop_id)])
        except Exception:
            _LOGGER.exception(
                "browse_property request failed for %s/0x%04X", dev_type, prop_id,
            )
        else:
            value = results.get((dev_id, prop_id))

    return {
        "device_id": dev_id,
        "device_type": dev_type,
        "property_id": f"0x{prop_id:04X}",
        "name": param.name,
        "display_name": param.display_name,
        "unit": getattr(param.codec, "unit", None),
        "writable": param.codec.writable,
        "current_value": value,
    }


async def _list_properties(call: ServiceCall) -> ServiceResponse:
    """Dump all available properties for a device (or every device)."""
    from homeassistant.util.json import JsonValueType

    runtimes = _all_runtimes(call.hass)
    if not runtimes:
        raise ServiceValidationError("no EcoTherm Wolf entry configured")
    runtime = runtimes[0]
    coord = runtime.coordinator
    if coord is None:
        raise ServiceValidationError("coordinator not ready")

    out: list[JsonValueType] = []
    target_device = call.data.get("device")
    for dev_id, (dev_type, _read, _write) in coord.device_map.items():
        if target_device is not None and str(target_device) not in (
            str(dev_id),
            dev_type,
        ):
            continue
        for p in runtime.catalog.get_device(dev_type).parameters.values():
            out.append({
                "device_id": dev_id,
                "device_type": dev_type,
                "property_id": f"0x{p.property_id:04X}",
                "name": p.name,
                "display_name": p.display_name,
                "writable": p.codec.writable,
                "platform": p.codec.ha_platform,
            })
    return {"properties": out}


async def _rediscover_devices(call: ServiceCall) -> ServiceResponse:
    """Re-run device discovery and MERGE the result into the device map.

    Sends a ``SystemConfigReq`` to the ISM7 and maps each reported bus
    device to a catalog template. The merge is strictly additive:

    - An existing entry with the same ``(device_type, read_address)`` is
      left untouched (including a user-customised write address).
    - An existing entry whose type matches but whose read address moved
      gets the new read address in place (same device_id -- entities and
      stored selections keep working). The write address follows the
      curated default only if it previously equalled the old read address;
      a user-set divergent write address is preserved.
    - A genuinely new device appends at the next free device_id.
    - Existing entries are NEVER removed or renumbered. Logical sub-modules
      (WWSystem_BM-2, MK_BM-2, ...) don't surface in the SystemConfigResp
      at all, and entity unique_ids embed the device_id -- a destructive
      replace would orphan their entities and could even shift values onto
      the wrong device. Modules that no longer answer are reported in
      ``missing`` instead.

    Ambiguous rows (``ambiguous_dtid_*``) and unknown rows
    (``unknown_dtid_*``) are never merged -- the user must resolve those in
    the config flow; the response lists them for visibility.
    """
    from homeassistant.util.json import JsonValueType

    runtimes = _all_runtimes(call.hass)
    if not runtimes:
        raise ServiceValidationError("no EcoTherm Wolf entry configured")
    runtime = runtimes[0]
    coord = runtime.coordinator
    if coord is None:
        raise ServiceValidationError("coordinator not ready")

    from .discovery import discover_devices

    discovered = await discover_devices(runtime.client, coord.catalog)

    def _resolved(dev_type: str) -> bool:
        return not (
            dev_type.startswith("ambiguous_dtid_")
            or dev_type.startswith("unknown_dtid_")
        )

    curated_writes = coord.catalog.curated_write_addresses
    new_map: dict[int, tuple[str, str, str]] = dict(coord.device_map)
    added: list[JsonValueType] = []
    updated: list[JsonValueType] = []
    matched_ids: set[int] = set()

    resolved_rows = [d for d in discovered if _resolved(d.device_type)]
    # Pass 1: exact (device_type, read_address) matches -- nothing to do.
    pending = []
    for d in resolved_rows:
        hit = next(
            (
                dev_id for dev_id, (t, read, _w) in new_map.items()
                if dev_id not in matched_ids
                and t == d.device_type
                and read.lower() == d.bus_address.lower()
            ),
            None,
        )
        if hit is not None:
            matched_ids.add(hit)
        else:
            pending.append(d)
    # Pass 2: same type, moved read address -> update in place.
    for d in pending:
        hit = next(
            (
                dev_id for dev_id, (t, _read, _w) in new_map.items()
                if dev_id not in matched_ids and t == d.device_type
            ),
            None,
        )
        if hit is not None:
            matched_ids.add(hit)
            old_type, old_read, old_write = new_map[hit]
            # Follow the curated default only when the user never set a
            # divergent write address (write == old read).
            new_write = (
                curated_writes.get(d.dtid, d.bus_address)
                if old_write.lower() == old_read.lower()
                else old_write
            )
            new_map[hit] = (old_type, d.bus_address, new_write)
            updated.append({
                "device_id": hit,
                "device_type": d.device_type,
                "old_read_address": old_read,
                "read_address": d.bus_address,
                "write_address": new_write,
            })
        else:
            # Pass 3: genuinely new -> append at the next free id.
            next_id = (max(new_map) + 1) if new_map else 1
            write_address = curated_writes.get(d.dtid, d.bus_address)
            new_map[next_id] = (d.device_type, d.bus_address, write_address)
            matched_ids.add(next_id)
            added.append({
                "device_id": next_id,
                "device_type": d.device_type,
                "read_address": d.bus_address,
                "write_address": write_address,
            })

    # Physical modules in the map that discovery no longer reports. Logical
    # sub-modules share their parent's bus and never appear in the
    # SystemConfigResp, so only exact read-address misses of types that
    # discovery COULD see are flagged; we keep them in the map regardless.
    discovered_buses = {d.bus_address.lower() for d in resolved_rows}
    missing: list[JsonValueType] = []
    for dev_id, (t, read, _w) in new_map.items():
        if dev_id not in matched_ids and read.lower() not in discovered_buses:
            missing.append({
                "device_id": dev_id, "device_type": t, "read_address": read,
            })

    if added or updated:
        coord.set_device_map(new_map)
    return {
        "devices": [
            {
                "device_type": d.device_type,
                "display_name": d.friendly_name,
                "bus_address": d.bus_address,
                "dtid": d.dtid,
                "resolved": _resolved(d.device_type),
            }
            for d in discovered
        ],
        "added": added,
        "updated": updated,
        "missing": missing,
    }


async def _reload_definitions(call: ServiceCall) -> ServiceResponse:
    """Re-read XML definitions and swap them into the runtime catalog.

    Delegates to ``_load_combined_catalog`` (in ``__init__.py``) so the
    bundled+override loading logic is shared with startup. A broken override
    directory creates a Repair Issue but does not abort the reload -- the
    bundled definitions remain authoritative.

    Updating only ``runtime.catalog`` is not enough: the coordinator owns its
    own ``_catalog`` reference for decode + bundle-build. We push the new
    catalog into the coordinator (which prunes selections that disappeared
    and re-subscribes if anything is still active) so subsequent push frames
    are decoded with the new schema.
    """
    runtimes = _all_runtimes(call.hass)
    if not runtimes:
        raise ServiceValidationError("no EcoTherm Wolf entry configured")
    runtime = runtimes[0]

    # Lazy import: avoids a circular import (``__init__.py`` imports services
    # indirectly via the platform modules during entry setup).
    from . import _load_combined_catalog
    new_catalog = await _load_combined_catalog(call.hass)

    diff = runtime.catalog.diff(new_catalog)
    runtime.catalog = new_catalog  # swap in for non-coordinator consumers
    if runtime.coordinator is not None:
        await runtime.coordinator.set_catalog(new_catalog)

    return {
        "devices_loaded": len(new_catalog.devices),
        "added": [
            {"device": d, "property_id": f"0x{p:04X}", "name": n}
            for d, p, n in diff.added
        ],
        "removed": [
            {"device": d, "property_id": f"0x{p:04X}", "name": n}
            for d, p, n in diff.removed
        ],
        "changed": [
            {"device": d, "property_id": f"0x{p:04X}", "name": n}
            for d, p, n in diff.changed
        ],
    }


async def _diagnose(call: ServiceCall) -> ServiceResponse:
    """Return a snapshot of the integration's internal state for bug reports."""
    runtimes = _all_runtimes(call.hass)
    if not runtimes:
        raise ServiceValidationError("no EcoTherm Wolf entry configured")
    runtime = runtimes[0]
    coord = runtime.coordinator
    if coord is None:
        raise ServiceValidationError("coordinator not ready")

    return {
        "connected": runtime.client.connected,
        "selected_count": len(coord.selected),
        "values_cached": len(coord.data),
        "last_push_at": (
            coord.last_push_at.isoformat() if coord.last_push_at else None
        ),
        "stale_minutes": coord.stale_minutes,
        "reconnect_count": coord.reconnect_count,
        "write_errors_24h": coord.write_errors_count_24h,
        "device_map": {
            str(k): {
                "device_type": v[0],
                "read_address": v[1],
                "write_address": v[2],
            }
            for k, v in coord.device_map.items()
        },
        "catalog_devices": list(runtime.catalog.devices.keys()),
    }


def async_register(hass: HomeAssistant) -> None:
    """Register services on first entry. Idempotent.

    HA may call ``async_setup_entry`` multiple times if the user adds another
    entry; the ``has_service`` guard prevents duplicate registration.
    """
    if hass.services.has_service(DOMAIN, "browse_property"):
        return
    hass.services.async_register(
        DOMAIN, "browse_property", _browse_property,
        schema=BROWSE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "list_properties", _list_properties,
        schema=LIST_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "rediscover_devices", _rediscover_devices,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "reload_definitions", _reload_definitions,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "diagnose", _diagnose,
        supports_response=SupportsResponse.ONLY,
    )


def async_unregister(hass: HomeAssistant) -> None:
    """Unregister services. Called when the last entry unloads."""
    for name in (
        "browse_property",
        "list_properties",
        "rediscover_devices",
        "reload_definitions",
        "diagnose",
    ):
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)
