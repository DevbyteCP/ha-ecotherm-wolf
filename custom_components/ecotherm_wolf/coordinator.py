# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
# Derived in part from zivillian/ism7mqtt (GPL-3.0-or-later)
"""EcoTherm Wolf DataUpdateCoordinator -- owns subscriptions, writes, value cache.

T21e introduced the 2-stage init flow used by zivillian/ism7mqtt:

1. ``async_setup`` registers callbacks, then runs the init handshake
   interleaved per bundle (zivillian's ``LoadInitialValuesAsync`` pattern):
   for each bundle, send a ``pull`` (with ``AbortOnError=false``), process
   the snapshot, then immediately ``push``-subscribe the *same* bundle
   before moving to the next one. Every send is ``request``-gated.
2. ``KeepAlive`` is sent every 60 s on its own task to keep the connection
   alive between push frames.
3. Bundle IDs are sequential strings (``"1"``, ``"2"``, ...). InfoReads carry
   a per-connection sequence id (``Seq``) which we increment on every send.
4. Bundles cap at 20 InfoReads and hold telegrams of a *single* bus address
   (``build_bundles`` -- mixing addresses overloads the ISM7's eBus poller).
   A parameter's ``info_numbers`` (1+ telegrams for multi-telegram
   converters) MUST stay within one bundle.

The push response carries the same telegrams; ``_process_telegrams`` groups
them by bus_address and asks the parameter's ``ConverterTemplate`` to decode
the assembled ``{info_number: (dl, dh)}`` dict.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_REBOOT_COOLDOWN_SECONDS,
    DEFAULT_REBOOT_USERNAME,
    DEFAULT_RECONNECT_REPAIR_SECONDS,
    DEFAULT_STALE_REPAIR_SECONDS,
    DEFAULT_STALE_THRESHOLD_SECONDS,
    DEFAULT_WRITE_TIMEOUT_SECONDS,
    DOMAIN,
    REBOOT_FORM_DATA,
    REBOOT_HTTP_TIMEOUT_SECONDS,
    REBOOT_PATH,
)
from .definitions.catalog import (
    Catalog,
    UnknownDeviceError,
    UnknownParameterError,
)
from .definitions.codecs import (
    BoolCodec,
    EnumCodec,
    Int16Codec,
    ScaledCodec,
    TimeOfDayCodec,
    Uint16Codec,
)
from .ism7.client import Ism7Client
from .ism7.exceptions import Ism7Error, WriteRejected
from .ism7.protocol import (
    BundleType,
    InfonumberReadResp,
    InfoRead,
    InfoWrite,
    KeepAliveReq,
    TelegramBundleReq,
    TelegramBundleResp,
)

_LOGGER = logging.getLogger(__name__)

SubscriptionKey = tuple[int, int]   # (device_id, property_id)
# Each device_id maps to (device_type, read_address, write_address). Wolf
# modules can listen for write commands on a different bus address than the
# one they report reads on (BM-2 / CHA do) -- so reads and writes are tracked
# separately. Addresses are hex strings ("0x35", "0x3"); an empty read address
# means "unknown" (legacy entries before T21d). An empty write address means
# "fall back to the read address" (legacy 2-element entries).
DeviceMapValue = tuple[str, str, str]

# Bundles cap at 20 info_reads. Multi-telegram parameters (32-bit counters,
# mixer state) must stay together within one bundle.
MAX_INFO_READS_PER_BUNDLE = 20

# KeepAlive cadence -- zivillian sends one per minute and treats the absence
# of a response within 60s as a hard timeout.
KEEPALIVE_INTERVAL_SECONDS = 60.0


def _normalise_device_map(
    raw: dict[int, Any],
) -> dict[int, DeviceMapValue]:
    """Coerce stored entry data into the (type, read_addr, write_addr) shape.

    Three input shapes are accepted for backward-compat:

    - 3-element ``[type, read, write]`` -- the current shape, used as-is.
    - 2-element ``[type, addr]`` (pre write-address split) -- the single
      address becomes both the read AND the write address. Using ``read``
      as the write default is the safe choice: it preserves the previous
      behaviour exactly for modules where write == read, and for modules
      that actually need a divergent write address the config flow will
      have re-stored a 3-element entry.
    - 1-string ``type`` (pre-T21d, no address known) -- both addresses
      empty; subscribe and write surface the missing address explicitly.
    """
    out: dict[int, DeviceMapValue] = {}
    for k, v in raw.items():
        if isinstance(v, tuple | list) and len(v) == 3:
            out[int(k)] = (str(v[0]), str(v[1]), str(v[2]))
        elif isinstance(v, tuple | list) and len(v) == 2:
            # Legacy 2-element entry: write address defaults to the read
            # address (safe -- matches pre-split behaviour).
            out[int(k)] = (str(v[0]), str(v[1]), str(v[1]))
        elif isinstance(v, str):
            # Legacy: device_type only, no bus_address known.
            out[int(k)] = (v, "", "")
        else:
            _LOGGER.warning("ignoring malformed device_map entry: %r -> %r", k, v)
    return out


def _entry_read_address(entry: Any) -> tuple[str, str]:
    """Return ``(device_type, read_address)`` from a device-map entry.

    Tolerant of both the current 3-tuple
    ``(device_type, read_address, write_address)`` and the legacy 2-tuple
    ``(device_type, bus_address)`` -- ``build_bundles`` is a public helper
    and may be handed an un-normalised map by callers / tests. The write
    address is irrelevant here: every bundle the builder produces carries
    reads, which always go to the read address.
    """
    if isinstance(entry, tuple | list) and len(entry) >= 2:
        return str(entry[0]), str(entry[1])
    # Legacy plain-string entry: device_type only, no address known.
    return str(entry), ""


def _param_service_number(param: Any) -> int:
    """The eBus ServiceReadNumber a parameter's reads must carry as ``snr``.

    Wolf reuses the same TelegramNr under several eBus "services"; the ISM7
    needs the ``snr`` on the ``ird`` to disambiguate, otherwise it answers
    0xFFFF for any non-default-service telegram. Returns ``-1`` (omit
    ``snr``) for synthetic catalogs / converters without a service gate.
    """
    converter = getattr(param, "converter", None)
    if converter is None:
        return -1
    snr = converter.service_read_number
    return snr if snr is not None else -1


def build_bundles(
    selected: list[SubscriptionKey],
    catalog: Catalog,
    device_map: dict[int, DeviceMapValue],
    *,
    max_per_bundle: int = MAX_INFO_READS_PER_BUNDLE,
) -> list[list[InfoRead]]:
    """Group selected (device_id, property_id) pairs into InfoRead bundles.

    Rules (matching zivillian/Ism7Config.GetBundlesForDevice):

    - Every bundle holds telegrams of a *single* bus address. zivillian
      builds bundles per bus address (``GetBundlesForDevice(ba)``) -- a
      bundle that mixes addresses forces the ISM7 to poll several eBus
      devices for one request, which overloads its internal poller and
      makes slow modules (the CWL on ``0x7C``) return 0xFFFF for everything.
    - At most ``max_per_bundle`` InfoReads per bundle (default 20), applied
      *within* each bus-address group.
    - All info_reads belonging to one parameter (multi-telegram converters)
      stay in the same bundle. A multi-telegram parameter has exactly one
      bus address, so this is compatible with the per-bus split.
    - Parameters are processed largest-info-count-first within each group to
      make packing efficient.

    Returns a list of bundles -- each bundle is itself a list of ``InfoRead``
    entries, all sharing one bus address (without ``seq`` set; the
    coordinator stamps that at send time). Bus-address groups are emitted in
    sorted order for deterministic bundle_id assignment. The caller assigns
    sequential bundle_ids ("1", "2", ...).
    """
    # Resolve each selection into its info_reads (skipping entries the
    # catalog/device_map can't satisfy). InfoReads always carry the READ
    # address -- the gateway answers reads on that bus address. They also
    # carry the converter's ServiceReadNumber as ``snr``: Wolf reuses the
    # same TelegramNr under several eBus "services" (the CWL exposes
    # telegram 1 under both 4022 and 4050), and the ISM7 answers 0xFFFF for
    # a service-gated telegram requested without its ``snr``.
    per_param_reads: list[list[InfoRead]] = []
    for device_id, property_id in selected:
        entry = device_map.get(device_id)
        if entry is None:
            continue
        device_type, read_address = _entry_read_address(entry)
        if not read_address:
            continue
        try:
            param = catalog.get_parameter(device_type, property_id)
        except (UnknownDeviceError, UnknownParameterError):
            continue
        if not param.info_numbers:
            continue
        service_number = _param_service_number(param)
        reads = [
            InfoRead(
                info_number=info_nr,
                bus_address=read_address,
                service_number=service_number,
            )
            for info_nr in param.info_numbers
        ]
        per_param_reads.append(reads)

    # Largest first -- gives the bin-packing a fighting chance, and lets
    # multi-telegram parameters claim their telegram nrs before any
    # single-telegram parameter that happens to share one.
    per_param_reads.sort(key=len, reverse=True)

    # Deduplicate by (bus_address, info_number, service_number). The author's
    # curated selection (and Wolf's catalog in general) pairs several
    # parameters with *different* names onto the *same* telegram nr -- the
    # catalog dedup keys on the name, so those slip through. Two reads that
    # agree on all three fields are the *identical* physical read; the
    # decode side feeds every sharing parameter from the one InfoRead. Two
    # reads that share (ba, in) but differ in service_number are NOT the
    # same telegram and MUST both be sent -- zivillian carries the ``snr``
    # on every InfoRead and never collapses them. An earlier build of this
    # integration deduped on (ba, in) alone and silently dropped the
    # 4050-service half, which is exactly what made the CWL read 0xFFFF.
    # Multi-telegram parameters keep their reads grouped (the converter
    # needs them together in one bundle); processing them first means a
    # later single-telegram parameter won't re-add a shared key.
    seen_keys: set[tuple[str, int, int]] = set()
    deduped_reads: list[list[InfoRead]] = []
    for reads in per_param_reads:
        if len(reads) == 1:
            r = reads[0]
            key = (r.bus_address, r.info_number, r.service_number)
            if key in seen_keys:
                continue
            seen_keys.add(key)
        else:
            for r in reads:
                seen_keys.add(
                    (r.bus_address, r.info_number, r.service_number),
                )
        deduped_reads.append(reads)
    per_param_reads = deduped_reads

    # Group the per-parameter read lists by bus address. Each parameter's
    # info_reads all carry the same (read) bus address -- ``reads[0]`` is
    # representative. Bin-packing then runs *inside* each group so no bundle
    # ever mixes addresses (zivillian's GetBundlesForDevice contract).
    by_bus: dict[str, list[list[InfoRead]]] = {}
    for reads in per_param_reads:
        by_bus.setdefault(reads[0].bus_address, []).append(reads)

    bundles: list[list[InfoRead]] = []
    # Sorted bus addresses -> deterministic bundle ordering (and therefore
    # deterministic bundle_id assignment by the caller).
    for bus_address in sorted(by_bus):
        bus_bundles: list[list[InfoRead]] = []
        for reads in by_bus[bus_address]:
            if len(reads) > max_per_bundle:
                _LOGGER.warning(
                    "parameter on bus %s requires %d info_reads, exceeds "
                    "max %d -- skipping",
                    bus_address, len(reads), max_per_bundle,
                )
                continue
            target = next(
                (
                    b for b in bus_bundles
                    if len(b) + len(reads) <= max_per_bundle
                ),
                None,
            )
            if target is None:
                bus_bundles.append(list(reads))
            else:
                target.extend(reads)
        bundles.extend(bus_bundles)
    return bundles


class EcothermWolfCoordinator(DataUpdateCoordinator[dict[SubscriptionKey, Any]]):
    """Owns subscription set, write queue, value cache, and listener fan-out.

    Push-driven: we don't poll. ``async_setup`` does a 2-stage handshake,
    interleaved per bundle (zivillian's ``LoadInitialValuesAsync`` pattern):

    1. ``pull`` snapshot for a bundle (AbortOnError=false) -- waits for the
       response and processes the telegrams immediately.
    2. ``push`` subscribe for the *same* bundle, with ``Intervall`` set so
       the gateway sends updates at that cadence on its own -- then advance
       to the next bundle.

    Every bundle holds telegrams of a single bus address, so the ISM7's
    eBus poller is only ever asked for one device at a time. Then the
    watchdog and KeepAlive background tasks start.
    """

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        client: Ism7Client,
        catalog: Catalog,
        device_map: dict[int, Any],
        selected: list[SubscriptionKey],
        heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
        stale_threshold_seconds: int = DEFAULT_STALE_THRESHOLD_SECONDS,
        is_active: bool = True,
        reboot_after_failures: int = 0,
        reboot_cooldown_seconds: int = DEFAULT_REBOOT_COOLDOWN_SECONDS,
        reboot_host: str = "",
        reboot_username: str = DEFAULT_REBOOT_USERNAME,
        reboot_password: str = "",
        reboot_notify_service: str = "",
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self._client = client
        self._catalog = catalog
        self._device_map: dict[int, DeviceMapValue] = _normalise_device_map(
            device_map,
        )
        self._selected: list[SubscriptionKey] = list(selected)
        # ``_write_lock`` guards every code path that mutates the per-conn
        # ``_bundle_counter`` / ``_sequence_counter`` and sends a Telegram
        # bundle in the same critical section. The initial-pull/subscribe
        # loop in ``_do_initial_pull_and_subscribe`` is single-task and runs
        # before any external entrypoint, so it does NOT take the lock
        # (acquiring it there would serialise an already-sequential path).
        self._write_lock = asyncio.Lock()
        self._timestamps: dict[SubscriptionKey, datetime] = {}
        # Last-seen telegram pairs per READ bus address, keyed on
        # (info_number, service_number) -- persists ACROSS push frames. The
        # ISM7 usually pushes only the telegrams whose value changed, so the
        # two halves of a multi-telegram parameter (32-bit counters:
        # Verdichterstarts, Betriebsstunden, Wärmemengen) rarely share a
        # frame; without this cache such values only ever updated on the
        # reconnect pull snapshot (live-freeze bug, fixed in 2.0.0).
        # zivillian solves the same problem with persistent _low/_high
        # fields on his NumericConverter32Template instances. Cleared on
        # reconnect/reactivate -- the fresh session's initial pull
        # repopulates everything in one sweep.
        self._telegram_cache: dict[
            str, dict[tuple[int, int | None], tuple[int, int]]
        ] = {}
        self._last_push_at: datetime | None = None
        self._reconnect_count: int = 0
        self._stale_threshold = timedelta(seconds=stale_threshold_seconds)
        self._active_reset_enabled: bool = True
        self._stale_minutes: int = 0
        self._watchdog_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._watchdog_interval_seconds: float = 30.0
        self._write_errors_24h: list[datetime] = []
        self._disconnected_since: datetime | None = None
        # ISM7 reboot watchdog config + state. ``_last_reboot_at`` gates the
        # cooldown so the climbing failure count during the gateway's boot
        # can't trigger reboot after reboot. Not persisted: a HA restart does
        # a fresh connect anyway, and a stale cooldown across restarts would
        # only delay a legitimate reboot.
        self._reboot_after_failures: int = reboot_after_failures
        self._reboot_cooldown = timedelta(seconds=reboot_cooldown_seconds)
        self._reboot_host: str = reboot_host
        self._reboot_username: str = reboot_username
        self._reboot_password: str = reboot_password
        self._reboot_notify_service: str = reboot_notify_service
        self._last_reboot_at: datetime | None = None
        # Runtime on/off for the auto-reboot (the "ISM7 Auto-Reboot" Health
        # switch). Defaults on; the switch (a RestoreEntity) restores the
        # user's last choice after a restart and pushes it here. Independent
        # of the Options config -- turning it off suppresses the reboot while
        # leaving the normal reconnect untouched.
        self._reboot_watchdog_enabled: bool = True
        self._bundle_counter: int = 0
        self._sequence_counter: int = 0
        self._heartbeat_seconds: int = heartbeat_seconds
        # N10: consecutive stale-watchdog trips. The first few are logged at
        # INFO (single brief reconnects are routine); after this threshold
        # we escalate to WARNING. Reset when a value-push comes in.
        self._consecutive_stale: int = 0
        # User-facing pause / resume flag (Active switch on the Health
        # device). When False, the supervisor + KeepAlive are stopped and
        # the socket is closed. Writes raise ``WriteRejected`` while paused.
        # Persisted to ``entry.options['is_active']`` via the entity setter
        # so a restart honours the user's choice.
        self._is_active: bool = is_active
        self._activation_lock = asyncio.Lock()
        self.data: dict[SubscriptionKey, Any] = {}

    @property
    def selected(self) -> list[SubscriptionKey]:
        return list(self._selected)

    @property
    def last_push_at(self) -> datetime | None:
        return self._last_push_at

    @property
    def device_map(self) -> dict[int, DeviceMapValue]:
        """Mapping ``device_id -> (device_type, read_address, write_address)``.

        Returns a *copy* so callers can't mutate the underlying state.
        """
        return dict(self._device_map)

    def device_type_for(self, device_id: int) -> str:
        """Return the catalog key for a configured device."""
        return self._device_map[device_id][0]

    def read_address_for(self, device_id: int) -> str:
        """Return the Wolf *read* bus address for a configured device.

        This is the address the gateway answers reads on. Empty string
        means we don't have an address yet -- writes/subscribes will fail
        explicitly until discovery populates it.
        """
        return self._device_map[device_id][1]

    # Back-compat alias: ``bus_address_for`` historically returned the single
    # address (which was used for both reads and writes). It now returns the
    # READ address -- callers that genuinely need the write address must use
    # ``write_address_for``.
    bus_address_for = read_address_for

    def write_address_for(self, device_id: int) -> str:
        """Return the Wolf *write* bus address for a configured device.

        Wolf modules (BM-2, CHA, ...) can listen for write commands on a
        different bus address than the one they report reads on. Empty
        string means no dedicated write address was configured -- callers
        should fall back to the read address (see ``async_write``).
        """
        return self._device_map[device_id][2]

    def set_device_map(self, new_map: dict[int, Any]) -> None:
        """Update device map after rediscovery.

        Caller is responsible for ensuring this is consistent with selected
        parameters (a property removed from a vanished device will start
        failing on subscribe). Accepts the new
        ``(device_type, read_address, write_address)`` 3-tuple shape, the
        legacy 2-element ``(device_type, bus_address)`` shape (write defaults
        to read) and the legacy plain-string form.
        """
        self._device_map = _normalise_device_map(new_map)
        self.async_update_listeners()

    async def set_catalog(self, new_catalog: Catalog) -> None:
        """Swap in a new catalog and re-bundle/re-subscribe if needed.

        Used by the ``reload_definitions`` service after the user edits XML
        overrides. Three steps:

        1. Replace ``_catalog`` with the new object so subsequent decode +
           bundle-build paths see the new schema.
        2. Prune ``_selected`` of any (device_id, ptid) whose parameter is no
           longer in the new catalog. Each removed entry is warn-logged.
        3. If anything was previously subscribed, re-run the apply_selection
           dance (which re-builds bundles + sends a push subscribe). This is
           idempotent -- a no-op when the catalog adds new params but doesn't
           change existing ones.
        """
        self._catalog = new_catalog

        valid: list[SubscriptionKey] = []
        for key in self._selected:
            device_id, property_id = key
            entry = self._device_map.get(device_id)
            if entry is None:
                _LOGGER.warning(
                    "set_catalog: dropping selection (no device): %s/0x%04X",
                    device_id, property_id,
                )
                continue
            device_type, _read, _write = entry
            try:
                new_catalog.get_parameter(device_type, property_id)
            except (UnknownDeviceError, UnknownParameterError):
                _LOGGER.warning(
                    "set_catalog: parameter no longer in catalog: %s/0x%04X",
                    device_type, property_id,
                )
                self.data.pop(key, None)
                self._timestamps.pop(key, None)
                continue
            valid.append(key)

        if valid != self._selected:
            # Apply the pruned selection. ``apply_selection`` is idempotent
            # for unchanged keys and will unsubscribe the dropped ones.
            await self.apply_selection(valid)
        else:
            # Selection unchanged, just notify listeners (so any entity that
            # depends on catalog metadata refreshes).
            self.async_update_listeners()

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def stale_minutes(self) -> int:
        return self._stale_minutes

    @property
    def client_connected(self) -> bool:
        """Whether the underlying ISM7 client is currently connected."""
        return self._client.connected

    @property
    def connect_failed(self) -> bool:
        """True while the ISM7 link can't be re-established.

        The client supervisor auto-reconnects after a peer reset, so a
        normal reset/reconnect cycle recovers in seconds and never trips
        this. It flips ``True`` only once the link has been down longer than
        ``DEFAULT_RECONNECT_REPAIR_SECONDS`` (5 min) -- i.e. reconnect keeps
        failing because the gateway is genuinely unreachable (ISM7 powered
        off, network down). Drives the "Kritischer Fehler (Connect nicht
        moeglich)" binary sensor for notification. Clears the moment the
        link comes back, and stays ``False`` while the connector is paused
        (Active switch off) so a deliberate pause is not flagged as a fault.
        """
        if not self._is_active:
            return False
        if self._disconnected_since is None:
            return False
        return (datetime.now(UTC) - self._disconnected_since) > timedelta(
            seconds=DEFAULT_RECONNECT_REPAIR_SECONDS,
        )

    @property
    def write_errors_count_24h(self) -> int:
        """Rolling count of failed writes in the last 24 hours.

        Fed by ``async_write``: every write that fails (gateway timeout,
        rejected/unacknowledged telegram, connection error, vanished
        parameter) stamps one entry. Drives the "Write Errors 24h" Health
        sensor.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        self._write_errors_24h = [t for t in self._write_errors_24h if t > cutoff]
        return len(self._write_errors_24h)

    def _record_write_error(self) -> None:
        """Stamp a failed write into the rolling 24h window (Health sensor)."""
        self._write_errors_24h.append(datetime.now(UTC))
        self.async_update_listeners()

    def set_active_reset_enabled(self, enabled: bool) -> None:
        """Setter for the stale-watchdog active-reset feature flag."""
        self._active_reset_enabled = enabled

    @property
    def heartbeat_seconds(self) -> int:
        """Current push-update interval (the ``Intervall`` in the subscribe
        bundle and the gap between KeepAlive frames)."""
        return self._heartbeat_seconds

    @property
    def is_active(self) -> bool:
        """Whether the integration is currently subscribed/connected.

        The Health-device Active switch flips this. When False the
        coordinator has shut down its background tasks and disconnected
        the client; writes raise ``WriteRejected``.
        """
        return self._is_active

    async def set_heartbeat_seconds(self, value: int) -> None:
        """Change the push-update interval at runtime.

        Triggers a re-subscribe so the gateway sends updates at the new
        cadence -- without this the ISM7 keeps using the interval from the
        last ``push`` bundle (set during setup or the previous change).
        Persistence is owned by the caller (the Number entity writes the
        new value into ``entry.options['heartbeat_seconds']`` so the next
        restart inherits it).
        """
        new = max(1, int(value))
        if new == self._heartbeat_seconds:
            return
        self._heartbeat_seconds = new
        if not self._is_active or not self._selected:
            return
        # Re-issue the push bundle so the gateway picks up the new
        # interval. _do_subscribe uses self._heartbeat_seconds, so a single
        # call carries the new cadence.
        try:
            async with self._write_lock:
                await self._do_subscribe()
        except Exception:  # noqa: BLE001 -- log and continue
            _LOGGER.exception("re-subscribe with new heartbeat failed")

    async def async_set_active(self, active: bool) -> None:
        """Pause (False) or resume (True) the integration's connection.

        Pausing closes the socket and stops the KeepAlive + stale-watchdog
        tasks. Resuming reconnects, re-runs the 2-stage init, and restarts
        the background tasks. Callers should also persist the new value to
        ``entry.options['is_active']`` so a restart respects the choice.
        Operations are serialised through ``_activation_lock`` so toggling
        rapidly off/on/off can't tear the state.
        """
        async with self._activation_lock:
            if active == self._is_active:
                return
            self._is_active = active
            if active:
                await self._activate_locked()
            else:
                await self._deactivate_locked()
        self.async_update_listeners()

    async def _deactivate_locked(self) -> None:
        """Tear down the connection + background tasks. ``_is_active`` is
        already False by the time we get here so re-entry from
        ``_on_reconnect`` is a no-op (see ``_resubscribe``).
        """
        # Stop background tasks first so they don't try to use a closed
        # socket. Each task is best-effort -- shutdown errors must not
        # leave the coordinator stuck.
        for task in (self._watchdog_task, self._keepalive_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                _LOGGER.exception("background task cancel raised during deactivate")
        self._watchdog_task = None
        self._keepalive_task = None
        try:
            await self._client.disconnect()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("client disconnect raised during deactivate")

    async def _activate_locked(self) -> None:
        """Reverse of ``_deactivate_locked``: reconnect, re-init, restart tasks."""
        try:
            await self._client.connect()
        except Exception:  # noqa: BLE001 -- surface but leave is_active True
            _LOGGER.exception("client connect raised during activate")
            return
        # Counter reset matches the post-reconnect path so a fresh session
        # starts from bundle/seq 1 (and without stale telegram halves).
        async with self._write_lock:
            self._bundle_counter = 0
            self._sequence_counter = 0
            self._telegram_cache.clear()
            if self._selected:
                await self._do_initial_pull_and_subscribe()
        self._watchdog_task = self.hass.async_create_background_task(
            self._watchdog_loop(),
            name=f"{DOMAIN}_stale_watchdog",
        )
        self._keepalive_task = self.hass.async_create_background_task(
            self._keepalive_loop(),
            name=f"{DOMAIN}_keepalive",
        )

    async def async_setup(self) -> None:
        """Register callbacks, do the 2-stage init, start background tasks.

        Honours the persisted ``is_active`` flag: when False, the listeners
        are still registered (so a later Activate toggle wires everything
        up) but the connection is closed and no init/keepalive runs.
        """
        self._client.on_update(self._on_update)
        self._client.on_reconnect(self._on_reconnect)

        if not self._is_active:
            # User paused the integration before restart. Close the socket
            # that __init__.py opened and leave the background tasks unset.
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001 -- shutdown best-effort
                _LOGGER.debug(
                    "client disconnect raised during paused-setup",
                    exc_info=True,
                )
            return

        if self._selected:
            await self._do_initial_pull_and_subscribe()

        self._watchdog_task = self.hass.async_create_background_task(
            self._watchdog_loop(),
            name=f"{DOMAIN}_stale_watchdog",
        )
        self._keepalive_task = self.hass.async_create_background_task(
            self._keepalive_loop(),
            name=f"{DOMAIN}_keepalive",
        )

    async def async_shutdown(self) -> None:
        """Stop the watchdog + keepalive tasks. Called from async_unload_entry."""
        for task in (self._watchdog_task, self._keepalive_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 -- shutdown best-effort
                _LOGGER.exception("background task shutdown failed")
        self._watchdog_task = None
        self._keepalive_task = None

    def _next_bundle_id(self) -> str:
        self._bundle_counter += 1
        return str(self._bundle_counter)

    def _next_seq(self) -> str:
        self._sequence_counter += 1
        return str(self._sequence_counter)

    async def read_once(
        self,
        selected: list[SubscriptionKey],
    ) -> dict[SubscriptionKey, Any]:
        """One-shot pull-read for the given keys.

        Shares the bundle-builder + decode pipeline with
        ``_do_initial_pull_and_subscribe`` / ``_process_telegrams`` so a
        service-level read always agrees with the live entity value.
        Multi-telegram parameters (32-bit counters, mixer state) come
        through unchanged because they're built into a single bundle by
        ``build_bundles``.

        Returns ``{(device_id, ptid): value}`` for every key that decoded
        successfully. Missing keys (timeout, decode failure, missing
        bus_address, multi-telegram halves split across timeouts) are simply
        omitted from the result.
        """
        bundles = build_bundles(
            selected, self._catalog, self._device_map,
        )
        # Group telegrams by bus_address, just like _process_telegrams does
        # for push frames -- keyed on (info_number, service_number) so a
        # TelegramNr that comes back once per eBus service isn't collapsed.
        by_bus: dict[str, dict[tuple[int, int | None], tuple[int, int]]] = {}

        async with self._write_lock:
            for bundle in bundles:
                for ir_entry in bundle:
                    ir_entry.seq = self._next_seq()
                bundle_id = self._next_bundle_id()
                req = TelegramBundleReq(
                    bundle_id=bundle_id,
                    bundle_type=BundleType.PULL,
                    abort_on_error=False,
                    info_reads=list(bundle),
                )
                try:
                    resp = await self._client.request(req, timeout=10.0)
                except Exception as e:  # noqa: BLE001
                    _LOGGER.warning(
                        "read_once bundle %s failed: %s", bundle_id, e,
                    )
                    continue
                if not isinstance(resp, TelegramBundleResp):
                    continue
                for t in resp.read_telegrams:
                    if t.state != "ok":
                        continue
                    try:
                        dl = int(t.db_low or "0", 16)
                        dh = int(t.db_high or "0", 16)
                    except ValueError:
                        continue
                    by_bus.setdefault(t.bus_address, {})[
                        (t.info_number, t.service_number)
                    ] = (dl, dh)

        out: dict[SubscriptionKey, Any] = {}
        for device_id, property_id in selected:
            entry = self._device_map.get(device_id)
            if entry is None:
                continue
            # Reads come back on the READ address -- match telegrams to the
            # parameter by the device's read address.
            device_type, read_addr, _write_addr = entry
            try:
                param = self._catalog.get_parameter(device_type, property_id)
            except (UnknownDeviceError, UnknownParameterError):
                continue
            per_bus = by_bus.get(read_addr)
            if per_bus is None:
                continue
            try:
                value = self._decode_param(param, per_bus)
            except Exception:
                _LOGGER.exception(
                    "read_once decode failed for %s/0x%04X",
                    device_type, property_id,
                )
                continue
            if value is None:
                continue
            out[(device_id, property_id)] = value
        return out

    async def _pull_bundle(self, bundle: list[InfoRead]) -> None:
        """Send one ``pull`` bundle, await the ack, decode the snapshot.

        A single bundle holds telegrams of one bus address (build_bundles
        guarantees this), so the gateway only has to poll one eBus device
        per request.

        Stamps seq onto COPIES of the bundle's InfoReads -- the same objects
        are reused by the follow-up subscribe (interleaved init), and
        mutating shared instances would alias the two requests' payloads.
        """
        reads = [replace(ir, seq=self._next_seq()) for ir in bundle]
        bundle_id = self._next_bundle_id()
        req = TelegramBundleReq(
            bundle_id=bundle_id,
            bundle_type=BundleType.PULL,
            abort_on_error=False,
            info_reads=reads,
        )
        try:
            resp = await self._client.request(req, timeout=10.0)
        except Exception as e:  # noqa: BLE001 -- tolerate any failure, log and skip
            _LOGGER.warning(
                "Initial pull bundle %s failed: %s", bundle_id, e,
            )
            return
        if not isinstance(resp, TelegramBundleResp):
            return
        # N9: if the bundle came back as state="error" but the gateway
        # still attached read_telegrams (some firmware quirks), only
        # decode the entries whose per-telegram state is "ok". A loud
        # info log makes the partial-failure visible without spamming.
        if resp.state != "ok":
            ok_count = sum(1 for t in resp.read_telegrams if t.state == "ok")
            _LOGGER.info(
                "Initial pull bundle %s state=%r emsg=%s -- decoding %d/%d ok rows",
                bundle_id, resp.state, resp.error_msg,
                ok_count, len(resp.read_telegrams),
            )
        self._process_telegrams(resp.read_telegrams)

    async def _subscribe_bundle(self, bundle: list[InfoRead]) -> None:
        """Send one ``push`` subscribe bundle at ``self._heartbeat_seconds``.

        Sent with ``request`` (not ``send``) so we wait for the gateway's
        ack before the caller advances to the next bundle. Firing all
        subscribe bundles at once floods the ISM7's internal eBus poller --
        slow modules like the CWL then report 0xFFFF for everything (and
        the Wolf app sees it too). zivillian interleaves pull+subscribe per
        bundle and only advances on the prior response; awaiting each
        subscribe ack gives the gateway the same breathing room.

        The first ``tbres`` for a push bundle carries the initial values
        and resolves the ``request`` future; subsequent periodic pushes
        reuse the same bundle_id and route to the update listeners (the
        client drops the pending future after the first response).
        """
        interval = self._heartbeat_seconds
        # Copies, not in-place mutation -- see _pull_bundle.
        reads = [
            replace(ir, seq=self._next_seq(), interval=interval)
            for ir in bundle
        ]
        bundle_id = self._next_bundle_id()
        req = TelegramBundleReq(
            bundle_id=bundle_id,
            bundle_type=BundleType.PUSH,
            abort_on_error=False,
            info_reads=reads,
        )
        try:
            resp = await self._client.request(req, timeout=10.0)
        except Exception as e:  # noqa: BLE001 -- log and continue; reconnect will retry
            _LOGGER.warning(
                "Subscribe bundle %s failed: %s", bundle_id, e,
            )
            return
        # The subscribe ack doubles as a fresh snapshot -- decode it so
        # the initial values land without waiting a full interval.
        if isinstance(resp, TelegramBundleResp):
            self._process_telegrams(resp.read_telegrams)

    async def _do_initial_pull_and_subscribe(self) -> None:
        """2-stage init, interleaved per bundle (zivillian's pattern).

        zivillian's ``LoadInitialValuesAsync`` / ``OnInitialValuesAsync``
        pull a bundle, then -- driven by that pull's response -- subscribe
        the *same* bundle before moving on. We mirror that here: build the
        bundle list once (so pull and subscribe operate on identical
        single-bus bundles), then for each bundle do pull -> subscribe ->
        next. Every step is ``request``-gated, so the ISM7's eBus poller is
        never asked for more than one bundle at a time.

        ``_do_subscribe`` is kept as a separate standalone method for the
        re-subscribe-only path (heartbeat interval change), which must not
        re-pull.
        """
        bundles = build_bundles(
            self._selected, self._catalog, self._device_map,
        )
        for bundle in bundles:
            await self._pull_bundle(bundle)
            await self._subscribe_bundle(bundle)

    async def _do_subscribe(self) -> None:
        """Re-issue the ``push`` subscribe bundles at ``self._heartbeat_seconds``.

        Used standalone by ``set_heartbeat_seconds`` to re-subscribe with a
        new interval *without* re-pulling. The live init / reconnect path
        uses ``_do_initial_pull_and_subscribe``, which interleaves the pull
        and subscribe stages per bundle. Each bundle is single-bus and
        ``request``-gated, so the gateway is never asked for more than one
        bundle at a time.
        """
        bundles = build_bundles(
            self._selected, self._catalog, self._device_map,
        )
        for bundle in bundles:
            await self._subscribe_bundle(bundle)

    async def _send_unsubscribe(self, keys: list[SubscriptionKey]) -> None:
        """Send a ``TelegramBundleReq(type="remove")`` per bus address.

        Used when ``apply_selection`` removes parameters from the active
        set. Like the pull/subscribe bundles, a ``remove`` bundle holds
        telegrams of a *single* bus address -- a mixed-address bundle would
        make the ISM7 touch several eBus devices for one request. Unsub is
        less critical than subscribe, but the single-bus rule is kept for
        consistency (and the bundles are typically tiny, so no 20-cap dance
        is needed -- one bundle per address is enough).
        """
        # Group the remove-InfoReads by read address. Unsubscribe cancels a
        # read subscription, so it targets the READ address.
        by_bus: dict[str, list[InfoRead]] = {}
        for d, p in keys:
            entry = self._device_map.get(d)
            if entry is None:
                continue
            device_type, read_address, _write_address = entry
            if not read_address:
                continue
            try:
                param = self._catalog.get_parameter(device_type, p)
            except (UnknownDeviceError, UnknownParameterError):
                continue
            # The remove bundle must carry the same ``snr`` the subscribe
            # carried, or the ISM7 can't match the subscription to cancel.
            service_number = _param_service_number(param)
            for info_nr in param.info_numbers:
                by_bus.setdefault(read_address, []).append(InfoRead(
                    info_number=info_nr,
                    bus_address=read_address,
                    seq=self._next_seq(),
                    interval=-1,
                    service_number=service_number,
                ))
        # Sorted addresses -> deterministic bundle_id assignment.
        for bus_address in sorted(by_bus):
            await self._client.send(TelegramBundleReq(
                bundle_id=self._next_bundle_id(),
                bundle_type=BundleType.REMOVE,
                abort_on_error=False,
                info_reads=by_bus[bus_address],
            ))

    async def apply_selection(self, new_selected: list[SubscriptionKey]) -> None:
        """Diff old vs. new selection and unsubscribe/subscribe accordingly.

        Runs entirely under ``_write_lock`` so the per-connection bundle/seq
        counters cannot be torn between concurrent callers (Options-flow
        save in parallel with a reconnect, for example).
        """
        async with self._write_lock:
            old = set(self._selected)
            new = set(new_selected)
            added = sorted(new - old)
            removed = sorted(old - new)

            if removed:
                await self._send_unsubscribe(removed)
                for key in removed:
                    self.data.pop(key, None)
                    self._timestamps.pop(key, None)

            if added:
                # Subscribe (push) for newly added keys only. Same builder
                # as the init path -> single-bus bundles, 20-cap respected.
                # ``_subscribe_bundle`` uses ``request`` (not ``send``) and
                # awaits each ack, so adding a large selection can't flood
                # the ISM7's eBus poller (consistent with the init flow).
                bundles = build_bundles(
                    added, self._catalog, self._device_map,
                )
                for bundle in bundles:
                    await self._subscribe_bundle(bundle)

            self._selected = sorted(new)
        self.async_update_listeners()

    async def async_write(
        self, *, device_id: int, property_id: int, value: Any
    ) -> None:
        """Encode and send a write; surface failures as HomeAssistantError.

        Every failure mode is mapped to a ``HomeAssistantError`` so HA's
        service layer renders a clean toast instead of a raw "unknown error"
        / HTTP 500:

        - ``TimeoutError`` -- the eBus write transaction outran the request
          timeout. ``request`` raises the bare builtin ``TimeoutError``
          (from ``asyncio.wait_for``), which is *not* an ``Ism7Error``; left
          unwrapped it surfaced as "unknown error". The gateway frequently
          still applies the value, so the message says so.
        - ``UnknownDeviceError`` / ``UnknownParameterError`` -- the catalog
          lost the parameter (e.g. ``reload_definitions`` after an override
          edit) while its entity still exists. ``KeyError`` subclasses, so
          left unwrapped they'd surface as "unknown error".
        - ``Ism7Error`` subclasses (ConnectionLost, WriteRejected,
          ProtocolError, ...) -- the ``ism7.*`` layer's own failures.
        - ``ValueError`` (incl. codec ``ValidationError``) -- an
          unencodable / out-of-range value on the synthetic codec path.
        - ``OSError`` -- a socket error escaping the writer (drain on a
          half-open connection).

        Every failure also stamps the rolling 24h write-error window that
        feeds the "Write Errors 24h" Health sensor.
        """
        try:
            await self._async_write_inner(
                device_id=device_id, property_id=property_id, value=value,
            )
        except TimeoutError as e:
            self._record_write_error()
            raise HomeAssistantError(
                "Zeitüberschreitung beim Schreiben – der ISM7 hat den "
                "eBus-Befehl nicht rechtzeitig bestätigt. Der Wert wurde "
                "möglicherweise trotzdem gesetzt; kurz warten und bei Bedarf "
                "erneut versuchen."
            ) from e
        except (UnknownDeviceError, UnknownParameterError) as e:
            self._record_write_error()
            raise HomeAssistantError(
                f"Schreiben nicht möglich: Parameter {e} ist nicht mehr im "
                "Katalog – Integration neu laden oder die Werte-Auswahl "
                "in den Optionen anpassen."
            ) from e
        except Ism7Error as e:
            self._record_write_error()
            raise HomeAssistantError(str(e)) from e
        except ValueError as e:
            self._record_write_error()
            raise HomeAssistantError(
                f"Ungültiger Wert beim Schreiben: {e}"
            ) from e
        except OSError as e:
            self._record_write_error()
            raise HomeAssistantError(
                f"Verbindungsfehler beim Schreiben: {e}"
            ) from e

    async def _async_write_inner(
        self, *, device_id: int, property_id: int, value: Any
    ) -> None:
        """Encode and send a write under the write lock; optimistic update."""
        if not self._is_active:
            raise WriteRejected(
                "Integration ist pausiert – den Schalter „Verbindung aktiv“ "
                "wieder einschalten, bevor geschrieben wird",
            )
        entry = self._device_map.get(device_id)
        if entry is None:
            # A vanished device id (e.g. the map shrank) must surface as a
            # clean error toast, not a bare KeyError ("unknown error").
            raise WriteRejected(
                f"unbekanntes Gerät {device_id} – die Gerätezuordnung hat "
                "sich geändert; Integration neu laden oder neu einrichten",
            )
        device_type, read_address, write_address = entry
        if not write_address:
            # No dedicated write address configured (legacy 2-element entry
            # for a module that actually needs a divergent address, or a
            # manual entry where the user left the field blank). Fall back
            # to the read address -- better than crashing, and correct for
            # the majority of Wolf modules where write == read.
            if not read_address:
                raise WriteRejected(
                    f"keine Bus-Adresse für Gerät {device_id} – bitte die "
                    "Einrichtung erneut durchlaufen",
                )
            _LOGGER.warning(
                "No write bus address configured for device_id=%d (%s) -- "
                "falling back to the read address %s. If writes to this "
                "module fail, set its write address in the integration "
                "options.",
                device_id, device_type, read_address,
            )
            write_address = read_address
        param = self._catalog.get_parameter(device_type, property_id)
        # Encode outside the lock (CPU work, may raise) -- only the bundle/
        # seq stamping and the send go under the lock so concurrent writes
        # cannot interleave their counter values.
        if param.converter is None:
            # Synthetic catalog (no converter) -- fall back to the codec.
            raw = param.codec.encode(value)
            db_low, db_high = _split_raw_to_dl_dh(raw)
            encoded_pairs: list[tuple[int, str, str]] = [
                (param.info_numbers[0], db_low, db_high),
            ]
        else:
            # The converter only encodes ints; map the Select option
            # string / Switch bool back to the member id first (inverse of
            # the _coerce_to_codec hop on the decode side).
            converter_value = _coerce_from_codec(value, param.codec)
            try:
                encoded = param.converter.encode(converter_value)
            except NotImplementedError as e:
                raise WriteRejected(
                    f"{device_type}/0x{property_id:04X} ist schreibgeschützt",
                ) from e
            # ISM7 expects 0x-prefixed hex on iwr telegrams; a bare "1A"
            # makes the gateway drop the connection (matches zivillian's
            # ``DBLow = $"0x{low:X2}"``).
            encoded_pairs = [
                (info_nr, f"0x{dl:02X}", f"0x{dh:02X}")
                for info_nr, (dl, dh) in encoded.items()
            ]

        async with self._write_lock:
            # zivillian sends iwr telegrams with an empty ``se`` attribute
            # (``result.Seq = ""`` in GetWriteRequest) -- the per-connection
            # sequence counter is only stamped on reads/subscriptions.
            info_writes = [
                InfoWrite(
                    info_number=info_nr,
                    bus_address=write_address,
                    db_low=db_low,
                    db_high=db_high,
                    seq="",
                )
                for info_nr, db_low, db_high in encoded_pairs
            ]
            # Generous timeout -- the eBus write transaction is slow, a cold
            # first write regularly outruns 5 s (see const.py for the full
            # rationale). A dead socket still fails fast: the client cancels
            # every pending bundle the moment the connection drops.
            resp = await self._client.request(
                TelegramBundleReq(
                    bundle_id=self._next_bundle_id(),
                    bundle_type=BundleType.WRITE,
                    abort_on_error=True,
                    info_writes=info_writes,
                ),
                timeout=DEFAULT_WRITE_TIMEOUT_SECONDS,
            )
            if not isinstance(resp, TelegramBundleResp):
                raise WriteRejected(
                    "Schreiben abgelehnt: keine verwertbare Antwort vom Gateway",
                )
            if resp.state != "ok":
                raise WriteRejected(
                    f"Schreiben abgelehnt: {_write_state_reason(resp.state)}",
                )
            if not resp.write_telegrams:
                raise WriteRejected(
                    "Schreiben abgelehnt: das Gateway hat keine Bestätigung "
                    "gesendet",
                )
            for ack in resp.write_telegrams:
                if ack.state != "ok":
                    raise WriteRejected(
                        f"Schreiben abgelehnt: {_write_state_reason(ack.state)}",
                    )

        # Optimistic update.
        self.data[(device_id, property_id)] = value
        self._timestamps[(device_id, property_id)] = datetime.now(UTC)
        self.async_update_listeners()

    def _process_telegrams(
        self, telegrams: list[InfonumberReadResp],
    ) -> None:
        """Merge telegrams into the cache, then decode each parameter via its converter.

        Multi-telegram parameters need ALL their telegrams before the
        converter can produce a value -- but the ISM7's push frames usually
        carry only the telegrams whose value changed, so the halves of a
        32-bit counter almost never share a frame. Incoming telegrams are
        therefore merged into the persistent ``_telegram_cache`` and decode
        runs against the merged per-bus view (fresh halves combined with
        the last-seen ones). Only parameters on a bus that received fresh
        data in THIS frame are re-decoded.

        When a parameter has no ``converter`` attached (synthetic / non-Wolf
        test fixtures), we fall back to the legacy codec path that decodes
        a single telegram by ``property_id == info_number``.
        """
        # Key on (info_number, service_number): the same TelegramNr can come
        # back twice in one frame, once per eBus service (the CWL answers
        # telegram 1 under both 4022 and 4050). A flat info_number dict would
        # drop one half. ``service_number`` is ``None`` when the response
        # carries no ``snr`` -- ``_decode_param`` then matches by info_number
        # alone (zivillian's RunningParameter.ProcessDatapoint contract).
        by_bus: dict[str, dict[tuple[int, int | None], tuple[int, int]]] = {}
        for t in telegrams:
            if t.state != "ok":
                continue
            try:
                dl = int(t.db_low or "0", 16)
                dh = int(t.db_high or "0", 16)
            except ValueError:
                continue
            by_bus.setdefault(t.bus_address, {})[
                (t.info_number, t.service_number)
            ] = (dl, dh)

        if not by_bus:
            return

        # Merge the fresh telegrams into the persistent per-connection cache
        # so multi-telegram parameters can combine a freshly pushed half
        # with the one last seen frames (or hours) earlier.
        for bus_address, entries in by_bus.items():
            self._telegram_cache.setdefault(bus_address, {}).update(entries)

        now = datetime.now(UTC)
        any_decoded = False
        for device_id, property_id in self._selected:
            entry = self._device_map.get(device_id)
            if entry is None:
                continue
            # Push frames carry reads -- match telegrams to the parameter by
            # the device's READ address.
            device_type, read_addr, _write_addr = entry
            try:
                param = self._catalog.get_parameter(device_type, property_id)
            except (UnknownDeviceError, UnknownParameterError):
                continue
            if read_addr not in by_bus:
                # No fresh telegram for this device in THIS frame --
                # re-decoding from the cache alone would only repeat the
                # values (and timestamps) it already has.
                continue
            per_bus = self._telegram_cache.get(read_addr)
            if per_bus is None:
                continue
            try:
                value = self._decode_param(param, per_bus)
            except Exception:
                _LOGGER.exception(
                    "Decode failed for %s/0x%04X",
                    device_type, property_id,
                )
                continue
            if value is None:
                continue
            self.data[(device_id, property_id)] = value
            self._timestamps[(device_id, property_id)] = now
            any_decoded = True

        if any_decoded:
            self._last_push_at = now
            self._consecutive_stale = 0
            ir.async_delete_issue(self.hass, DOMAIN, "long_stale")
            self.async_update_listeners()

    def _decode_param(
        self,
        param: Any,
        per_bus: dict[tuple[int, int | None], tuple[int, int]],
    ) -> Any:
        """Decode a single parameter from the per-bus telegrams.

        ``per_bus`` is keyed on ``(info_number, service_number)`` -- the
        same TelegramNr can appear once per eBus service in a frame.

        Real Wolf parameters carry a ``ConverterTemplate`` and we delegate
        to it. The converter's ``service_read_number`` routes which response
        telegrams it may consume, mirroring zivillian's
        ``RunningParameter.ProcessDatapoint``: a response telegram that
        carries a service number is only fed to the converter whose
        ServiceReadNumber matches; a telegram with no service number matches
        by info_number alone. Synthetic catalogs without converters use the
        legacy codec-based decode keyed on ``property_id`` (== info_number).

        Wolf catalog quirk: ``ListParameterDescriptor`` produces an
        ``EnumCodec`` *and* a ``NumericConverter16`` (CTID == PTID).
        The converter returns a raw int -- the Select platform expects
        the mapped option-string. We post-process the int via the codec's
        members so the value cache stores the option string. Same idea
        for ``BoolCodec`` paired with NumericConverter16: int -> bool.
        """
        if param.converter is not None:
            svc = param.converter.service_read_number
            # Filter the per-bus telegrams down to the ones this converter's
            # service is allowed to consume, then collapse to the plain
            # ``{info_number: (dl, dh)}`` shape the converter expects.
            telegrams: dict[int, tuple[int, int]] = {}
            for (info_nr, resp_snr), pair in per_bus.items():
                if resp_snr is not None and resp_snr != svc:
                    continue
                telegrams[info_nr] = pair
            required = param.converter.info_numbers
            if not required or not all(n in telegrams for n in required):
                return None
            value = param.converter.decode(telegrams)
            return _coerce_to_codec(value, param.codec)
        # Fallback: synthetic / pre-T21e catalog. Use property_id as
        # info_number; synthetic catalogs never carry a service number.
        found = next(
            (
                p for (info_nr, _snr), p in per_bus.items()
                if info_nr == param.property_id
            ),
            None,
        )
        if found is None:
            return None
        dl, dh = found
        codec = param.codec
        base = codec.base if isinstance(codec, ScaledCodec) else codec
        if isinstance(base, Int16Codec | Uint16Codec | TimeOfDayCodec):
            raw = bytes([dh, dl])
        else:
            raw = bytes([dl])
        return codec.decode(raw)

    def _on_update(self, update: TelegramBundleResp) -> None:
        """Called by the read loop with unsolicited push-driven telegrams."""
        self._process_telegrams(update.read_telegrams)

    def _on_reconnect(self) -> None:
        """Triggered when the client reconnects after a connection loss.

        Schedules an async resubscribe; the actual counter reset happens
        inside that coroutine under ``_write_lock`` so it cannot race with a
        concurrent ``async_write`` / ``apply_selection`` that also mutates
        the per-connection counters.
        """
        self._reconnect_count += 1
        self._disconnected_since = None
        ir.async_delete_issue(self.hass, DOMAIN, "long_disconnect")
        self.hass.async_create_task(self._resubscribe())
        self.async_update_listeners()

    async def _resubscribe(self) -> None:
        """After a reconnect: redo the 2-stage handshake.

        Resets the per-connection sequence/bundle counters under the write
        lock so a concurrent ``async_write`` / ``apply_selection`` cannot
        observe partially-reset state. If the user paused the integration
        while a reconnect was in flight, skip the handshake -- the next
        Activate toggle will rebuild the subscription cleanly.
        """
        if not self._is_active:
            return
        try:
            async with self._write_lock:
                self._bundle_counter = 0
                self._sequence_counter = 0
                # Session boundary: drop the previous connection's telegram
                # halves so a fresh push can't be combined with pre-reconnect
                # data. The initial pull below repopulates the cache at once.
                self._telegram_cache.clear()
                if not self._selected:
                    return
                await self._do_initial_pull_and_subscribe()
        except Exception:  # noqa: BLE001 -- defensive; reconnect retries via supervisor
            _LOGGER.exception("resubscribe after reconnect failed")

    async def _keepalive_loop(self) -> None:
        """Background: send a KeepAlive frame every minute.

        Matches zivillian's behaviour. Runs forever; cancelled in
        ``async_shutdown``. Failures are logged but don't tear down the loop.
        """
        seq = 0
        while True:
            try:
                await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return
            if not self._client.connected:
                continue
            seq = (seq + 1) % 0x7FFF
            try:
                await self._client.send(KeepAliveReq(seq=seq))
            except Exception:  # noqa: BLE001 -- log and continue; supervisor reconnects
                _LOGGER.exception("KeepAlive send failed")

    async def _watchdog_loop(self) -> None:
        """Periodic check: stale-watchdog and disconnect tracking.

        Two failure modes are watched:

        1. Stale: TCP up but ISM7 stops pushing values (well-known Wolf
           Link Home failure mode). We track ``_last_push_at`` and
           force-disconnect when the gap exceeds the threshold; the
           supervisor reconnects and ``_on_reconnect`` re-subscribes.
           After ``DEFAULT_STALE_REPAIR_SECONDS`` we additionally fire a
           ``long_stale`` Repair Issue so the user gets a UI prompt.

        2. Disconnect: TCP unreachable. We observe ``client.connected``
           in this loop -- the first iteration after a disconnect sets
           ``_disconnected_since``; once the gap exceeds
           ``DEFAULT_RECONNECT_REPAIR_SECONDS`` we fire ``long_disconnect``.
           Cleared when the connection comes back (here or in
           ``_on_reconnect``).
        """
        while True:
            try:
                await asyncio.sleep(self._watchdog_interval_seconds)
            except asyncio.CancelledError:
                return

            # Disconnect tracking + repair issue.
            if not self._client.connected:
                if self._disconnected_since is None:
                    self._disconnected_since = datetime.now(UTC)
                else:
                    disconnect_age = datetime.now(UTC) - self._disconnected_since
                    if disconnect_age > timedelta(
                        seconds=DEFAULT_RECONNECT_REPAIR_SECONDS,
                    ):
                        ir.async_create_issue(
                            self.hass, DOMAIN, "long_disconnect",
                            is_fixable=False,
                            severity=ir.IssueSeverity.ERROR,
                            translation_key="long_disconnect",
                        )
                # Reboot watchdog: if the reconnect has failed enough times,
                # power-cycle the gateway once (then keep reconnecting).
                await self._maybe_reboot_gateway()
                self.async_update_listeners()
                # Don't run the stale check while disconnected -- the gap
                # would always trip the threshold and would be misleading.
                continue
            else:
                if self._disconnected_since is not None:
                    self._disconnected_since = None
                    ir.async_delete_issue(self.hass, DOMAIN, "long_disconnect")

            # Stale tracking + active reset (T15).
            if self._last_push_at is None:
                # No pushes yet (just connected, or initial dump pending) -- skip.
                continue
            age = datetime.now(UTC) - self._last_push_at
            if age > self._stale_threshold:
                self._stale_minutes = int(age.total_seconds() / 60)
                self._consecutive_stale += 1
                # N10: first 2 trips at INFO; from the 3rd consecutive trip on
                # we escalate to WARNING. Reset by _process_telegrams when a
                # push actually lands. Avoids log spam during brief router /
                # gateway hiccups while still surfacing real outages loudly.
                log_fn = (
                    _LOGGER.warning if self._consecutive_stale >= 3
                    else _LOGGER.info
                )
                log_fn(
                    "ISM7 stale for %s -- last push at %s (n=%d)",
                    age, self._last_push_at, self._consecutive_stale,
                )
                if self._active_reset_enabled:
                    _LOGGER.debug("Triggering active connection reset")
                    await self._client.force_disconnect()
                # Repair issue once the stale gap crosses the longer threshold.
                if age > timedelta(seconds=DEFAULT_STALE_REPAIR_SECONDS):
                    ir.async_create_issue(
                        self.hass, DOMAIN, "long_stale",
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key="long_stale",
                        translation_placeholders={
                            "minutes": str(self._stale_minutes),
                        },
                    )
            else:
                self._stale_minutes = 0
                ir.async_delete_issue(self.hass, DOMAIN, "long_stale")
            self.async_update_listeners()

    # -- ISM7 reboot watchdog ----------------------------------------------

    @property
    def reboot_watchdog_enabled(self) -> bool:
        """Whether the runtime 'ISM7 Auto-Reboot' Health switch allows reboots."""
        return self._reboot_watchdog_enabled

    def set_reboot_watchdog_enabled(self, enabled: bool) -> None:
        """Runtime on/off for the auto-reboot (driven by the Health switch).

        Independent of the Options config: even with a reboot password set
        and the threshold reached, the watchdog won't power-cycle the gateway
        while this is off. The normal reconnect is unaffected either way.
        """
        self._reboot_watchdog_enabled = bool(enabled)
        self.async_update_listeners()

    def _reboot_enabled(self) -> bool:
        """Whether the auto-reboot is armed and may fire.

        Three gates: the runtime Health switch is on, the threshold is
        positive, AND a reboot host + password are configured (the firmware
        endpoint is HTTP-basic-auth protected -- no password means nothing to
        call). The credential gate keeps the feature inert for HACS users who
        haven't entered their gateway's reboot login.
        """
        return (
            self._reboot_watchdog_enabled
            and self._reboot_after_failures > 0
            and bool(self._reboot_host)
            and bool(self._reboot_password)
        )

    async def _maybe_reboot_gateway(self) -> None:
        """Power-cycle the ISM7 once when the reconnect keeps failing.

        Called from the watchdog's disconnect branch. Fires only when the
        client's consecutive reconnect-failure count has reached the
        configured threshold AND we're outside the post-reboot cooldown.
        The cooldown is armed on the *attempt* (not just success): if the
        gateway is so dead its web endpoint is unreachable too, we must not
        re-POST every 30 s watchdog tick -- one try, then wait out the boot
        window before trying again.
        """
        if not self._reboot_enabled():
            return
        failures = self._client.consecutive_connect_failures
        if failures < self._reboot_after_failures:
            return
        now = datetime.now(UTC)
        if (
            self._last_reboot_at is not None
            and (now - self._last_reboot_at) < self._reboot_cooldown
        ):
            return
        self._last_reboot_at = now
        _LOGGER.warning(
            "ISM7 reconnect failed %d× (threshold %d) -- rebooting gateway %s",
            failures, self._reboot_after_failures, self._reboot_host,
        )
        if await self._reboot_gateway_http():
            await self._notify_reboot(failures)

    async def _reboot_gateway_http(self) -> bool:
        """POST the ISM7 firmware reboot endpoint. True if the request went out.

        Replicates the legacy ``shell_command`` curl: an HTTP-basic-auth,
        form-urlencoded POST (``name=reboKickoff&rebo=init``) to
        ``/protect/reboot.htm``. Uses HA's shared aiohttp session. A dead
        gateway may refuse/timeout the HTTP endpoint too, or drop the socket
        mid-reboot -- all logged and reported as "not sent" (no notify), the
        cooldown already guards against hammering.
        """
        session = async_get_clientsession(self.hass)
        url = f"http://{self._reboot_host}{REBOOT_PATH}"
        auth = aiohttp.BasicAuth(self._reboot_username, self._reboot_password)
        try:
            async with asyncio.timeout(REBOOT_HTTP_TIMEOUT_SECONDS):
                async with session.post(
                    url, data=dict(REBOOT_FORM_DATA), auth=auth,
                ) as resp:
                    _LOGGER.info(
                        "ISM7 reboot POST %s -> HTTP %s", url, resp.status,
                    )
                    return True
        except (TimeoutError, aiohttp.ClientError, OSError) as e:
            _LOGGER.warning("ISM7 reboot request to %s failed: %s", url, e)
            return False

    async def _notify_reboot(self, failures: int) -> None:
        """Fire the optional notify service after a reboot was triggered.

        ``_reboot_notify_service`` is a ``notify.<target>`` (or bare
        ``<target>``) string from the Options Flow; empty disables the push.
        A notify failure must never break the watchdog.
        """
        service = self._reboot_notify_service
        if not service:
            return
        domain, _, name = service.partition(".")
        if not name:
            domain, name = "notify", domain
        try:
            await self.hass.services.async_call(
                domain, name,
                {
                    "title": "EcoTherm Wolf",
                    "message": (
                        f"ISM7-Gateway ({self._reboot_host}) nach {failures} "
                        "fehlgeschlagenen Reconnect-Versuchen neu gestartet."
                    ),
                },
                blocking=False,
            )
        except Exception:  # noqa: BLE001 -- a notify failure must not break the watchdog
            _LOGGER.exception("ISM7 reboot notify (%s) failed", service)


def _coerce_to_codec(value: Any, codec: Any) -> Any:
    """Map a converter-decoded value into the codec's typed domain.

    Wolf's ``ListParameterDescriptor`` and Ein/Aus booleans pair a
    semantic codec (``EnumCodec`` / ``BoolCodec``) with a
    ``NumericConverter16`` -- the converter just reads a 16-bit int and
    leaves the enum/bool mapping to us. Without this hop the Select
    platform would see ``int`` and display ``unknown`` (its
    ``current_option`` rejects non-strings).

    Numeric / time-of-day / mixer-state values pass through unchanged.
    Out-of-range enum values produce ``None`` (entity shows "unknown",
    the HA default for selects) -- matches ``EnumCodec.decode``'s
    ``unknown_<n>`` fallback only semantically; we prefer ``None`` here so
    the Select entity stays empty rather than caching a fake option.
    """
    if value is None:
        return None
    if isinstance(codec, EnumCodec):
        if not isinstance(value, int):
            return value
        return codec.members.get(value)
    if isinstance(codec, BoolCodec):
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value != 0
        return value
    return value


def _coerce_from_codec(value: Any, codec: Any) -> Any:
    """Map HA's typed write value back into the int the converter expects.

    Inverse of ``_coerce_to_codec``: the Select platform hands us an
    option string and the Switch platform a bool, but Wolf's
    ``NumericConverter16`` only knows how to encode ints. Without this hop
    ``converter.encode("Reduzierte Lüftung")`` raises
    ``invalid literal for int()``.

    Numeric / scaled / time-of-day values pass through unchanged.
    """
    if isinstance(codec, EnumCodec):
        # ``bool`` is an ``int`` subclass -- guard so a stray bool doesn't
        # masquerade as a member id.
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        for member_id, name in codec.members.items():
            if name == value:
                return member_id
        raise WriteRejected(f"unbekannte Option {value!r}")
    if isinstance(codec, BoolCodec):
        if isinstance(value, str):
            return 1 if value.strip().lower() in ("on", "true", "1") else 0
        return 1 if value else 0
    return value


def _split_raw_to_dl_dh(raw: bytes) -> tuple[str, str]:
    """Split an encoded value (1 or 2 bytes, big-endian) into Wolf's dl/dh hex.

    Wolf's wire format puts the low-order byte in ``dl`` and the high-order
    byte in ``dh``. The ISM7 firmware expects each as a ``0x``-prefixed
    upper-case hex string on ``iwr`` write telegrams -- a bare ``"14"`` makes
    the gateway drop the TCP connection. For an Int16Codec encoding
    20 -> ``b"\x00\x14"`` -> dl="0x14", dh="0x00".

    Used only by the legacy codec-based write path (synthetic catalogs); the
    real Wolf path goes through ``ConverterTemplate.encode``.
    """
    if len(raw) == 1:
        return f"0x{raw[0]:02X}", "0x00"
    if len(raw) == 2:
        return f"0x{raw[1]:02X}", f"0x{raw[0]:02X}"
    raise ValueError(f"unsupported raw length {len(raw)} for InfoWrite")


def _write_state_reason(state: str) -> str:
    """Human-readable reason for a non-OK ISM7 write-telegram state.

    The ISM7 answers each ``iac`` (and the bundle itself) with one of
    OK / TO / BU / ER -- zivillian's ``TelegrResponseState``;
    ``_norm_state`` lower-cases it before it reaches us. ``TO`` is the
    common, benign one: the ISM7 forwarded the write onto the eBus but the
    module never acknowledged it -- typically because that parameter is
    only settable directly on the unit (the CWL's "Luftdurchsatz Stufe 3"
    behaves exactly this way, and zivillian sees the identical TO). A bare
    ``ack state=to`` toast is useless to the user, so we spell it out.
    """
    return {
        "to": (
            "das Gerät hat ihn nicht bestätigt (eBus-Timeout) – dieser Wert "
            "lässt sich möglicherweise nur direkt am Gerät einstellen"
        ),
        "bu": "der ISM7/eBus war beschäftigt – kurz warten und erneut versuchen",
        "er": "der ISM7 hat einen Fehler gemeldet",
    }.get(state, f"unerwarteter Status '{state}'")
