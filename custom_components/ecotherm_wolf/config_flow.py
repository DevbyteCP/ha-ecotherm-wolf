# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Config flow for the EcoTherm Wolf integration."""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client_factory import create_client
from .const import (
    CONF_DEVICES,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SELECTED_PARAMETERS,
    DEFAULT_PORT,
    DOMAIN,
    RESOURCE_DIR,
)
from .definitions.catalog import Catalog
from .discovery import DiscoveredDevice, SubmoduleCandidate
from .ism7.exceptions import AuthenticationError, Ism7Error
from .resources import BUNDLED_CLIENT_CERT, missing_files, resolve_cert

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry, OptionsFlow

_LOGGER = logging.getLogger(__name__)

PRESETS = ["minimal", "standard", "all", "manual"]

BUNDLED_DEFINITIONS_DIR = Path(__file__).parent / "definitions" / "data" / "devices"

# Bus addresses are hex (``0x35``, ``0x8``). Accept upper- or lower-case
# for the prefix AND the digits (the duplicate check lowercases for
# comparison, so ``0X8`` must validate the same as ``0x8``); reject
# anything that's not a hex byte/short.
_HEX_RE = re.compile(r"^0[xX][0-9A-Fa-f]+$")


def _submodule_value(c: SubmoduleCandidate) -> str:
    """Stable option value for a sub-module candidate (parsed back on submit)."""
    return f"{c.device_type}@{c.bus_address}"


def _submodule_label(c: SubmoduleCandidate) -> str:
    """Human-readable row label for a sub-module candidate in add_more."""
    status = "am Bus erkannt" if c.present else "nicht erkannt"
    return (
        f"{c.device_type} @ {c.bus_address} "
        f"(gehört zu {c.parent_device_type} – {status})"
    )


class EcothermWolfConfigFlow(ConfigFlow, domain=DOMAIN):
    """Setup wizard.

    Happy path: ``user`` (connect) -> ``devices`` (auto-discover) ->
    ``select_discovered`` -> ``preset`` -> (``select``).

    Fallback when discovery fails or finds nothing: ``user`` -> ``devices``
    -> ``add_device`` (repeat until user clicks "Fertig") -> ``preset``.
    """

    VERSION = 1

    def __init__(self) -> None:
        self._host: str = ""
        self._port: int = DEFAULT_PORT
        self._password: str = ""
        # device_id -> (device_type, read_address, write_address). Wolf
        # modules can listen for writes on a different bus address than the
        # one they report reads on, so both are tracked per device.
        self._device_map: dict[int, tuple[str, str, str]] = {}
        self._preset: str = "standard"
        self._catalog: Catalog | None = None
        # (device_id, property_id, name, display_name)
        self._available: list[tuple[int, int, str, str]] = []
        # Auto-discovery results (cached for the duration of the flow).
        self._discovered: list[DiscoveredDevice] = []
        # Logical sub-modules (warm-water system, mixer circuit, ...) probed
        # off the discovered devices during ``_run_discovery`` -- on the same
        # connection -- and offered in async_step_add_more as a pre-checked
        # multi-select.
        self._submodule_candidates: list[SubmoduleCandidate] = []
        # Background task driving the discovery progress dialog (connect +
        # SystemConfigReq + sub-module probe). ``async_show_progress`` owns
        # its lifetime: HA cancels it if the user closes the dialog and
        # re-invokes the step when it finishes.
        self._discovery_task: asyncio.Task[None] | None = None
        # Outcome of the discovery task: "ok" routes onward,
        # "invalid_auth" / "cannot_connect" bounce back to step 1.
        self._discovery_outcome: str = "ok"
        # Manual entries collected via async_step_add_device. After
        # select_discovered chose some discovered rows and ``add_more`` is
        # confirmed, these append to ``_device_map`` with sequential IDs
        # following the discovery range. Each tuple is
        # (device_type, read_address, write_address). Cleared the moment the
        # merge into ``_device_map`` happens so ``_format_added_list`` can't
        # count the same entry twice.
        self._manual: list[tuple[str, str, str]] = []
        # How many of the leading ``_device_map`` slots came from
        # auto-discovery. Manual entries occupy the slots after that. Lets
        # ``_format_added_list`` label each row's origin correctly even after
        # the manual entries have been merged into ``_device_map``.
        self._discovered_count: int = 0
        # Client certificate resolved in step 1 (user dir or bundled).
        self._cert_path: Path = BUNDLED_CLIENT_CERT
        # Resolved type for each ambiguous_dtid_* row in ``_discovered``.
        # Keyed by index into ``_discovered``. Populated in
        # async_step_resolve_ambiguous before select_discovered runs.
        self._ambiguous_resolved: dict[int, str] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: connection -- host, port, password.

        We deliberately do NOT validate the connection here. The real ISM7
        rejects a second TLS handshake within ~1s of the first close
        (single-connection limit + recovery time), so opening + closing a
        probe socket here makes ``async_setup_entry``'s subsequent
        ``client.connect()`` time out. Invalid host/password is caught later
        by ``async_setup_entry`` and surfaces as a "Setup error" banner.

        Single-connection guard: ISM7 supports exactly one active TLS session,
        so we set the unique_id to ``host:port`` and abort if the user tries
        to add the same gateway twice.
        """
        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._port = user_input[CONF_PORT]
            self._password = user_input[CONF_PASSWORD]
            await self.async_set_unique_id(f"{self._host}:{self._port}")
            self._abort_if_unique_id_configured()
            # The Wolf XMLs + certificate are not shipped with the
            # integration; stop here with a clear hint instead of failing
            # somewhere deep in discovery.
            resource_dir = Path(self.hass.config.path(RESOURCE_DIR))
            missing = await self.hass.async_add_executor_job(
                missing_files, resource_dir,
            )
            if missing:
                return self.async_show_form(
                    step_id="user",
                    data_schema=self.add_suggested_values_to_schema(
                        self._user_schema(), user_input,
                    ),
                    errors={"base": "missing_wolf_files"},
                    description_placeholders={
                        "path": str(resource_dir), "files": ", ".join(missing),
                    },
                )
            self._cert_path = await self.hass.async_add_executor_job(
                resolve_cert, resource_dir,
            )
            return await self.async_step_devices()

        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(),
        )

    def _user_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )

    def _show_user_form_with_error(
        self, error_field: str, error_key: str,
    ) -> ConfigFlowResult:
        """Re-show step 1 with an error, keeping the already-typed values.

        Used when the discovery connect fails in a way the user has to fix
        on this form (wrong password, unreachable host). Without the
        suggested values the form would come back empty and the user would
        have to re-type everything to fix a one-character typo.
        """
        suggested = self.add_suggested_values_to_schema(
            self._user_schema(),
            {
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_PASSWORD: self._password,
            },
        )
        return self.async_show_form(
            step_id="user",
            data_schema=suggested,
            errors={error_field: error_key},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Re-authentication, started by HA when setup raises
        ``ConfigEntryAuthFailed`` (the ISM7 answered ``invalidcredentials``).

        Without this step HA crashes the reauth attempt with ``UnknownStep``
        and the user only ever sees a generic setup error. Routes straight
        to the confirm form -- the entry to fix comes from the flow context.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Ask for the new ISM7 password and reload the entry with it.

        Deliberately does NOT probe the connection with the new password
        (mirrors ``async_step_user``): the ISM7 allows a single TLS session
        and refuses rapid reconnects, so a probe here would make the
        subsequent reload's ``connect()`` fail spuriously. A still-wrong
        password simply fails the reload with ``ConfigEntryAuthFailed``
        again and HA re-prompts.

        Note: ``invalidcredentials`` is also what the ISM7 answers while
        *another* client occupies its single connection slot (e.g. the
        ism7mqtt addon) -- the form text reminds the user of that.
        """
        entry = self._get_reauth_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(
                entry,
                data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]},
            )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            description_placeholders={
                "host": str(entry.data.get(CONF_HOST, "?")),
            },
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: auto-discovery behind a progress dialog.

        The connect + ``SystemConfigReq`` round trip takes up to ~15 s
        (TLS handshake, gateway poll, fallback timeouts). Running it inline
        froze the wizard on step 1 with nothing but HA's generic spinner --
        a first-time user can't tell whether anything is happening. The
        work now runs as a background task wrapped in
        ``async_show_progress``; HA re-invokes this step when it finishes
        (and cancels the task if the user closes the dialog), then
        ``devices_done`` routes on the stored outcome.
        """
        # local import (avoid cycle in tests)
        from .discovery import discover_devices

        discover_impl = getattr(discover_devices, "implemented", False)
        if not discover_impl:
            # Discovery stub -- skip the probe and go straight to manual.
            return await self.async_step_add_device()

        if self._discovery_task is None:
            self._discovery_task = self.hass.async_create_task(
                self._run_discovery(),
            )
            if self._discovery_task.done():
                # Eager-start completed synchronously (mocked client in
                # tests). Skip the progress dialog entirely.
                self._discovery_task = None
                return await self.async_step_devices_done()
        if not self._discovery_task.done():
            return self.async_show_progress(
                step_id="devices",
                progress_action="discovering",
                progress_task=self._discovery_task,
            )
        self._discovery_task = None
        return self.async_show_progress_done(next_step_id="devices_done")

    async def _run_discovery(self) -> None:
        """Background task: connect, discover, probe sub-modules, disconnect.

        Never raises (a raising progress task would strand the flow): the
        outcome lands in ``_discovery_outcome`` / ``_discovered`` /
        ``_submodule_candidates`` and ``async_step_devices_done`` routes on
        it.

        Crucially the sub-module probe runs HERE, on the SAME open
        connection as discovery -- not later via a second connection. The
        ISM7 allows a single TLS session and is slow to free it, so opening
        a fresh socket mid-flow stalls (it froze the wizard on real
        hardware). One connect, one disconnect. The probe uses each
        device's best-guess type, so it works even while parents are still
        ambiguous (resolved only in the later resolve step).
        """
        from .discovery import discover_devices, probe_submodules

        self._discovery_outcome = "ok"
        self._discovered = []
        self._submodule_candidates = []
        catalog = await self._load_catalog()
        client = create_client(
            host=self._host, port=self._port, password=self._password,
            cert_path=self._cert_path,
        )
        try:
            try:
                await client.connect()
            except AuthenticationError as e:
                # Wrong password (or another client holds the ISM7's single
                # connection slot -- the firmware answers invalidcredentials
                # for both). Falling through to manual entry would let a
                # first-time user finish the whole wizard on credentials
                # that can never connect.
                _LOGGER.info("Discovery login rejected (%s); back to step 1", e)
                self._discovery_outcome = "invalid_auth"
                return
            except (OSError, TimeoutError, Ism7Error) as e:
                # Host unreachable / TLS refused / dropped during login.
                # An entry configured against an unreachable gateway is
                # useless -- let the user fix the host now.
                _LOGGER.info("Discovery connect failed (%s); back to step 1", e)
                self._discovery_outcome = "cannot_connect"
                return

            try:
                self._discovered = await discover_devices(client, catalog)
            except (Ism7Error, TimeoutError) as e:
                _LOGGER.info(
                    "Discovery probe failed (%s); falling back to manual", e,
                )
                self._discovered = []
            except Exception:  # noqa: BLE001 -- unexpected -> manual fallback
                _LOGGER.exception(
                    "Unexpected discovery error; falling back to manual",
                )
                self._discovered = []

            # Sub-module probe on the SAME live connection. Best-effort:
            # any failure just yields no pre-checked suggestions (the user
            # can still add sub-modules manually in add_more).
            if self._discovered:
                try:
                    self._submodule_candidates = await probe_submodules(
                        client, catalog, self._discovered,
                    )
                except Exception:  # noqa: BLE001 -- best-effort
                    _LOGGER.exception(
                        "Sub-module probe failed; continuing without it",
                    )
                    self._submodule_candidates = []
        finally:
            await self._safe_disconnect(client)

    async def async_step_devices_done(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Route on the discovery task's outcome (after the progress dialog).

        Discovery already ran the sub-module probe on its connection (see
        ``_run_discovery``); ``add_more`` just renders those results.
        """
        if self._discovery_outcome == "invalid_auth":
            return self._show_user_form_with_error(CONF_PASSWORD, "invalid_auth")
        if self._discovery_outcome == "cannot_connect":
            return self._show_user_form_with_error("base", "cannot_connect")

        if not self._discovered:
            return await self.async_step_add_device()
        # If any row is ambiguous, ask the user to resolve it before the
        # multi-select. Otherwise jump straight to select_discovered.
        if any(d.device_type.startswith("ambiguous_dtid_") for d in self._discovered):
            return await self.async_step_resolve_ambiguous()
        return await self.async_step_select_discovered()

    # Max number of ambiguous rows we render with localized per-row labels.
    # The translation files declare placeholders ``resolve_0``..``resolve_<N-1>``
    # with ``{bus_<i>}/{did_<i>}/{fw_<i>}`` substitutions. Real installs cap
    # out at ~3 ambiguous modules (reference installation: 3 of 4 are ambiguous),
    # so 4 named slots cover all realistic setups. Anything beyond falls back
    # to a generic field label.
    _AMBIGUOUS_RESOLVE_SLOTS = 4

    async def async_step_resolve_ambiguous(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 2 (was: ambiguous resolve): pick a device_type for each
        unresolved row.

        Ambiguous rows are produced by ``discover_devices`` when the WRS-id
        match exists but the on-wire SoftwareNumber disagrees with every
        catalog template at that WRS-id (or several templates compete).
        Live example: DID=0x15, sw=0xFFFF -- our catalog only has SM1 there,
        the physical device is a CWL (which has WRSDeviceIds=0x0 in Wolf's
        XML and so is unreachable by auto-match).

        The form shows one dropdown per ambiguous row. Each dropdown is
        seeded with the discovery's best-guess candidates plus all
        non-WRS-indexed catalog devices. The dropdown field label is
        localized via ``description_placeholders`` -- the translation file
        declares ``resolve_<i>: "Modul auf Bus {bus_<i>} (DID {did_<i>}, ...)"``
        and we substitute the bus/DID/firmware here.
        """
        ambiguous = [
            (idx, dev) for idx, dev in enumerate(self._discovered)
            if dev.device_type.startswith("ambiguous_dtid_")
        ]
        if not ambiguous:
            return await self.async_step_select_discovered()

        catalog = await self._load_catalog()

        if user_input is not None:
            # Stash the chosen device_type for each ambiguous row, then
            # rewrite the entry in ``_discovered`` so downstream steps see a
            # concrete device_type. Iterate by position so the key naming
            # stays in sync with the form we showed.
            for slot, (idx, _) in enumerate(ambiguous):
                key = self._resolve_key(slot)
                chosen = user_input.get(key)
                if not chosen:
                    continue
                self._ambiguous_resolved[idx] = chosen
                old = self._discovered[idx]
                try:
                    target = catalog.get_device(chosen)
                except KeyError:
                    continue
                # Replace the placeholder with the user's resolution. Keep
                # bus_address / wrs_device_id for diagnostics.
                self._discovered[idx] = DiscoveredDevice(
                    bus_address=old.bus_address,
                    device_type=target.device_type,
                    dtid=target.dtid,
                    friendly_name=(
                        f"{target.device_type} -- {target.display_name}"
                        if target.display_name
                        and target.display_name != target.device_type
                        else target.device_type
                    ),
                    wrs_device_id=old.wrs_device_id,
                    software_number=old.software_number,
                    firmware_version=old.firmware_version,
                )
            return await self.async_step_select_discovered()

        # Build one dropdown per ambiguous row.
        schema_dict: dict[Any, Any] = {}
        placeholders: dict[str, str] = {}
        for slot, (_idx, dev) in enumerate(ambiguous):
            options = self._ambiguous_options(dev, catalog)
            if not options:
                # No catalog templates at all -- shouldn't normally happen but
                # would crash schema construction. Skip the row; downstream
                # select_discovered will surface it as the ambiguous label so
                # the user can decide to drop it via the multi-select.
                continue
            valid_values = {opt["value"] for opt in options}
            # Pick the first candidate that actually exists in the dropdown.
            default = next(
                (c for c in dev.candidates if c in valid_values),
                options[0]["value"],
            )
            key = self._resolve_key(slot)
            schema_dict[vol.Required(key, default=default)] = SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
            placeholders[f"bus_{slot}"] = dev.bus_address or "?"
            placeholders[f"did_{slot}"] = dev.wrs_device_id or "?"
            placeholders[f"fw_{slot}"] = dev.firmware_version or "?"
        return self.async_show_form(
            step_id="resolve_ambiguous",
            data_schema=vol.Schema(schema_dict),
            description_placeholders=placeholders,
        )

    @classmethod
    def _resolve_key(cls, slot: int) -> str:
        """Return the form-field key for the ``slot``-th ambiguous row.

        Slots 0..N-1 (where N == ``_AMBIGUOUS_RESOLVE_SLOTS``) get the
        localized keys ``resolve_0``..``resolve_<N-1>`` -- the translation
        files declare friendly per-row labels for these. Anything beyond
        falls back to a generic ``resolve_extra_<slot>`` key; the UI will
        render it with the bare key name, which is rare in practice.
        """
        if slot < cls._AMBIGUOUS_RESOLVE_SLOTS:
            return f"resolve_{slot}"
        return f"resolve_extra_{slot}"

    def _ambiguous_options(
        self, dev: DiscoveredDevice, catalog: Catalog,
    ) -> list[SelectOptionDict]:
        """Build the dropdown options for an ambiguous discovery row.

        Discovery's best guesses (``dev.candidates``) come first; the rest of
        the catalog follows alphabetically so the user can pick something
        unrelated (CWL at DID 0x15 etc.).
        """
        seen: set[str] = set()
        options: list[SelectOptionDict] = []
        for dt in dev.candidates:
            try:
                target = catalog.get_device(dt)
            except KeyError:
                continue
            options.append(SelectOptionDict(
                value=dt,
                label=(
                    f"{target.device_type} -- {target.display_name}"
                    if target.display_name
                    and target.display_name != target.device_type
                    else target.device_type
                ),
            ))
            seen.add(dt)
        # Rest of catalog, sorted by label for readability.
        rest = sorted(
            (
                SelectOptionDict(
                    value=d.device_type,
                    label=(
                        f"{d.device_type} -- {d.display_name}"
                        if d.display_name and d.display_name != d.device_type
                        else d.device_type
                    ),
                )
                for d in catalog.devices.values()
                if d.device_type not in seen
            ),
            key=lambda x: x["label"].lower(),
        )
        return [*options, *rest]

    async def async_step_select_discovered(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 3a: multi-select checkbox of discovered devices.

        The discovered ``bus_address`` is the device's READ address. The
        write address is pre-filled from the curated DTID -> write-address
        map (known values of the reference installation); modules whose DTID isn't in that map
        default to write == read. The user reviews / corrects every write
        address in the follow-up ``write_addresses`` step.
        """
        if user_input is not None:
            chosen_labels = user_input.get("selected", [])
            chosen_set = set(chosen_labels)
            catalog = await self._load_catalog()
            self._device_map = {}
            next_id = 1
            for dev in self._discovered:
                label = self._discovered_label(dev)
                if label not in chosen_set:
                    continue
                write_address = self._default_write_address(
                    catalog, dev.dtid, dev.bus_address,
                )
                self._device_map[next_id] = (
                    dev.device_type, dev.bus_address, write_address,
                )
                next_id += 1
            # Everything in _device_map at this point is a discovery pick;
            # manual entries (if any) get merged in after add_device.
            self._discovered_count = len(self._device_map)
            if not self._device_map:
                # User unticked everything -- ask again.
                return self.async_show_form(
                    step_id="select_discovered",
                    data_schema=self._select_discovered_schema(),
                    errors={"base": "no_devices_selected"},
                )
            return await self.async_step_add_more()

        return self.async_show_form(
            step_id="select_discovered",
            data_schema=self._select_discovered_schema(),
        )

    @staticmethod
    def _default_write_address(
        catalog: Catalog, dtid: int, read_address: str,
    ) -> str:
        """Pick the default write bus address for a device.

        Returns the author's curated write address when the device's
        ``DeviceTemplateId`` is in ``Catalog.curated_write_addresses``,
        otherwise the read address (write == read for most Wolf modules).
        """
        return catalog.curated_write_addresses.get(dtid, read_address)

    def _resolve_manual_write_address(
        self, dev_type: str, read_address: str,
    ) -> str:
        """Write address for a manually added device when the user left the
        write field empty.

        An explicit write-address field always wins (resolved by the
        caller). When it's empty we still try the curated DTID default --
        so picking ``WWSystem_BM-2`` auto-fills ``0x30`` -- before falling
        back to ``write == read``.
        """
        if self._catalog is None:
            return read_address
        try:
            dtid = self._catalog.get_device(dev_type).dtid
        except Exception:  # noqa: BLE001 -- unknown device type -> read addr
            return read_address
        return self._default_write_address(self._catalog, dtid, read_address)

    async def async_step_add_more(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 4: offer the bus-detected sub-modules, then ask about manual.

        Wolf's bus reports physical modules. Logical sub-modules (e.g.
        ``WWSystem_BM-2`` on bus 0x35, ``MK_BM-2`` on bus 0x85) don't surface
        as their own busDevice row. ``_run_discovery`` already probed for
        them on the discovery connection; here we render those candidates --
        filtered to the parents the user actually kept -- as a checkbox
        list, pre-checked for the ones the bus confirmed. No typing of
        addresses, no extra connection. The user can also tick "add more
        manually" for anything exotic the probe doesn't know.

        Discovery IDs keep slots 1..N; picked sub-modules / manuals follow
        at N+1..M (``_merge_manual_into_device_map``).
        """
        candidates = self._relevant_submodules()

        if user_input is not None:
            # Queue the ticked sub-modules as pending manual entries (they
            # carry curated write addresses via _resolve_manual_write_address).
            for value in user_input.get("submodules", []):
                cand = next(
                    (c for c in candidates if _submodule_value(c) == value),
                    None,
                )
                if cand is None or self._is_duplicate(
                    cand.device_type, cand.bus_address,
                ):
                    continue
                self._manual.append((
                    cand.device_type,
                    cand.bus_address,
                    self._resolve_manual_write_address(
                        cand.device_type, cand.bus_address,
                    ),
                ))
            if user_input.get("add_more"):
                return await self.async_step_add_device()
            # No further manual entries -- merge what we have and advance.
            self._merge_manual_into_device_map()
            return await self.async_step_write_addresses()

        schema_dict: dict[Any, Any] = {}
        if candidates:
            options = [
                SelectOptionDict(
                    value=_submodule_value(c),
                    label=_submodule_label(c),
                )
                for c in candidates
            ]
            default = [
                _submodule_value(c) for c in candidates if c.present
            ]
            schema_dict[
                vol.Optional("submodules", default=default)
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        schema_dict[vol.Required("add_more", default=False)] = bool

        if candidates:
            probe_note = (
                "Oben stehen die am Bus gefundenen Sub-Module -- die "
                "bestätigten sind schon angehakt. Passt das, einfach auf OK."
            )
        else:
            probe_note = (
                "Es wurden keine Sub-Module automatisch gefunden. Falls du "
                "welche hast (Warmwasser, Mischerkreis), unten „weitere "
                "manuell hinzufügen“ anhaken."
            )

        return self.async_show_form(
            step_id="add_more",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "picked": self._format_picked() or "(noch keine)",
                "probe_note": probe_note,
            },
        )

    def _relevant_submodules(self) -> list[SubmoduleCandidate]:
        """Probed sub-modules whose parent the user actually kept.

        The probe (in discovery) ran for every discovered device; the user
        may have unticked some in select_discovered. Match on the parent's
        bus address (stable across ambiguous resolution) and drop any
        sub-module already in the device map.
        """
        parent_buses = {
            read.lower()
            for _t, read, _w in self._device_map.values()
            if read
        }
        out: list[SubmoduleCandidate] = []
        for c in self._submodule_candidates:
            if c.parent_bus_address.lower() not in parent_buses:
                continue
            if self._is_duplicate(c.device_type, c.bus_address):
                continue
            out.append(c)
        return out

    def _merge_manual_into_device_map(self) -> None:
        """Append the pending manual entries to ``_device_map`` and clear them.

        Discovery picks keep slots 1..N (``_discovered_count``); manual /
        sub-module entries follow at N+1.. Clearing ``_manual`` on merge
        keeps ``_format_added_list`` from counting an entry twice. Safe to
        call when ``_manual`` is already empty -- it's a no-op then.
        """
        if not self._manual:
            return
        base = dict(self._device_map)
        next_id = (max(base) + 1) if base else 1
        for entry in self._manual:
            base[next_id] = entry
            next_id += 1
        self._device_map = base
        self._manual = []

    def _format_picked(self) -> str:
        """Render the currently-selected modules as a comma list.

        Used by the ``add_more`` step where the device origin (discovery vs.
        manual) doesn't matter -- the user wants a quick reminder of what
        they've already picked.
        """
        return ", ".join(
            f"{t}@{read}" for t, read, _write in self._device_map.values()
        )

    def _select_discovered_schema(self) -> vol.Schema:
        labels = [self._discovered_label(d) for d in self._discovered]
        return vol.Schema(
            {
                vol.Required("selected", default=labels): SelectSelector(
                    SelectSelectorConfig(
                        options=labels,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

    @staticmethod
    def _discovered_label(dev: DiscoveredDevice) -> str:
        # ``"CHA -- Wärmepumpe (Bus 0x29)"`` -- unique per row.
        return f"{dev.friendly_name} (Bus {dev.bus_address})"

    async def async_step_add_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 4b: manually add a module.

        Two entrypoints:

        - Pure manual fallback (no discovery): ``_device_map`` is empty,
          ``done`` requires at least one manual entry, then the assembled
          manuals become ``_device_map`` with sequential IDs 1..N.
        - Mix with discovery: ``_device_map`` already contains 1..N from
          ``select_discovered``; manuals append at N+1..M.

        Loops on itself -- after submit we re-show the form so the user can
        add more modules. The ``done`` checkbox advances to ``preset``.

        Duplicate guard: a module entered twice (same device_type and bus
        address) surfaces ``duplicate_device`` on the form so the user
        notices instead of silently creating a second slot for the same
        physical module.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            done = user_input.get("done", False)
            dev_type = (user_input.get("device_type") or "").strip()
            bus_address = (user_input.get("bus_address") or "").strip()
            write_address = (user_input.get("write_address") or "").strip()

            # An empty write address means "same as the read address". A
            # filled one must still be valid hex. The effective write
            # address is resolved once both the read field and (optional)
            # write field have validated.
            write_invalid = bool(write_address) and not _HEX_RE.match(
                write_address,
            )

            if done:
                # If the user filled the form AND ticked "done" in the same
                # submit, don't silently drop the pending entry. Three cases:
                # - both fields empty: nothing pending, advance with what's
                #   already in _manual / _device_map.
                # - both fields valid: save the entry, then advance.
                # - one field filled, the other empty/invalid: surface an
                #   error in the same step so the user sees what's missing
                #   (mirrors the live bug hit -- ticking done with
                #   a half-filled form would lose the entry).
                form_empty = not dev_type and not bus_address
                form_complete = (
                    bool(dev_type)
                    and bool(bus_address)
                    and bool(_HEX_RE.match(bus_address))
                    and not write_invalid
                )
                if form_empty:
                    pass  # fall through to merge-and-advance below
                elif form_complete:
                    if self._is_duplicate(dev_type, bus_address):
                        errors["base"] = "duplicate_device"
                    else:
                        self._manual.append((
                            dev_type, bus_address,
                            write_address
                            or self._resolve_manual_write_address(
                                dev_type, bus_address,
                            ),
                        ))
                else:
                    if not dev_type:
                        errors["device_type"] = "required"
                    elif not bus_address or not _HEX_RE.match(bus_address):
                        errors["bus_address"] = "invalid_hex"
                    elif write_invalid:
                        errors["write_address"] = "invalid_hex"

                if not errors:
                    if not self._device_map and not self._manual:
                        errors["base"] = "no_devices_added"
                    else:
                        self._merge_manual_into_device_map()
                        return await self.async_step_write_addresses()
            elif not dev_type:
                errors["device_type"] = "required"
            elif not bus_address or not _HEX_RE.match(bus_address):
                errors["bus_address"] = "invalid_hex"
            elif write_invalid:
                errors["write_address"] = "invalid_hex"
            elif self._is_duplicate(dev_type, bus_address):
                errors["base"] = "duplicate_device"
            else:
                self._manual.append((
                    dev_type, bus_address,
                    write_address
                    or self._resolve_manual_write_address(
                        dev_type, bus_address,
                    ),
                ))

        return self.async_show_form(
            step_id="add_device",
            data_schema=await self._add_device_schema(),
            errors=errors,
            description_placeholders={
                "added": self._format_added_list(),
            },
        )

    def _is_duplicate(self, device_type: str, bus_address: str) -> bool:
        """Has this (device_type, bus_address) already been registered?

        Checks both discovered entries already in ``_device_map`` and pending
        manuals queued in ``_manual``. Bus addresses are normalised to lower
        case for the comparison so ``0x35`` and ``0X35`` count as the same.
        """
        bus_norm = bus_address.lower()
        if any(
            t == device_type and read.lower() == bus_norm
            for t, read, _write in self._device_map.values()
        ):
            return True
        return any(
            t == device_type and read.lower() == bus_norm
            for t, read, _write in self._manual
        )

    def _format_added_list(self) -> str:
        """Render the running list of modules for the add_device step.

        Numbered list with origin markers so the user can see which entries
        came from auto-discovery vs. were entered manually.

        Two call sites, two states of ``_device_map``:

        - ``add_device`` step: ``_device_map`` holds only the discovery picks
          (slots 1..``_discovered_count``); ``_manual`` holds the manuals the
          user has entered so far but not yet merged.
        - ``write_addresses`` step: the manuals have been merged into
          ``_device_map`` (slots ``_discovered_count``+1..) and ``_manual``
          is empty.

        ``_discovered_count`` tells the two apart, so a row is labelled
        ``(aus Auto-Discovery)`` only while its slot is within the discovery
        range -- and ``_manual`` being cleared on merge means no row is ever
        counted twice.
        """
        lines: list[str] = []
        idx = 1
        for dev_type, read, write in self._device_map.values():
            suffix = "" if write == read else f" (Schreiben: {write})"
            origin = (
                "aus Auto-Discovery"
                if idx <= self._discovered_count
                else "manuell"
            )
            lines.append(f"{idx}. {dev_type} @ {read}{suffix} ({origin})")
            idx += 1
        for dev_type, read, write in self._manual:
            suffix = "" if write == read else f" (Schreiben: {write})"
            lines.append(f"{idx}. {dev_type} @ {read}{suffix} (manuell)")
            idx += 1
        return "\n".join(lines) if lines else "(noch keine)"

    async def _add_device_schema(self) -> vol.Schema:
        catalog = await self._load_catalog()
        # Build a sorted dropdown ``device_type -- display_name``.
        options: list[SelectOptionDict] = sorted(
            [
                SelectOptionDict(
                    value=dev.device_type,
                    label=(
                        f"{dev.device_type} -- {dev.display_name}"
                        if dev.display_name and dev.display_name != dev.device_type
                        else dev.device_type
                    ),
                )
                for dev in catalog.devices.values()
            ],
            key=lambda x: x["label"].lower(),
        )
        return vol.Schema(
            {
                vol.Optional("device_type"): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional("bus_address", default=""): str,
                vol.Optional("write_address", default=""): str,
                vol.Optional("done", default=False): bool,
            }
        )

    @staticmethod
    def _write_address_key(device_id: int) -> str:
        """Form-field key for a device's write-address field."""
        return f"write_{device_id}"

    async def async_step_write_addresses(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 4c: review the write bus address of every selected module.

        Wolf modules can listen for write commands on a different bus
        address than the one they report reads on (the BM-2 and CHA do).
        The SystemConfigResp only carries the read address, so this step
        shows one pre-filled write-address field per module:

        - Discovery rows are seeded from the curated DTID -> write-address
          map (known values of the reference installation); unknown DTIDs default to the read
          address.
        - Manual rows keep whatever the user entered on the add step (which
          itself defaults to the read address when left blank).

        The user can correct any field; an empty field means "same as the
        read address". This step is skipped entirely when no module is
        configured (which can't normally happen -- the earlier steps
        guarantee at least one).
        """
        if not self._device_map:
            return await self.async_step_preset()

        if user_input is not None:
            errors: dict[str, str] = {}
            updated: dict[int, tuple[str, str, str]] = {}
            for device_id, (dev_type, read, write) in self._device_map.items():
                key = self._write_address_key(device_id)
                if key not in user_input:
                    # Field absent from the submission -- keep the current
                    # (pre-filled) write address untouched.
                    updated[device_id] = (dev_type, read, write)
                    continue
                raw = (user_input.get(key) or "").strip()
                if not raw:
                    # Field explicitly cleared -> write == read.
                    updated[device_id] = (dev_type, read, read)
                    continue
                if not _HEX_RE.match(raw):
                    errors[key] = "invalid_hex"
                    updated[device_id] = (dev_type, read, write)
                    continue
                updated[device_id] = (dev_type, read, raw)
            if errors:
                # Re-show with the user's (partially valid) edits preserved.
                self._device_map = updated
                return self.async_show_form(
                    step_id="write_addresses",
                    data_schema=self._write_addresses_schema(),
                    errors=errors,
                    description_placeholders={
                        "modules": self._format_added_list(),
                        **self._write_addresses_placeholders(),
                    },
                )
            self._device_map = updated
            return await self.async_step_preset()

        return self.async_show_form(
            step_id="write_addresses",
            data_schema=self._write_addresses_schema(),
            description_placeholders={
                "modules": self._format_added_list(),
                **self._write_addresses_placeholders(),
            },
        )

    # Number of write-address fields with localized labels. The translation
    # files declare ``write_1``..``write_<N>`` with a ``{mod_<i>}``
    # placeholder each, so the form shows "Schreib-Adresse: CHA @ 0x8"
    # instead of the bare field key ``write_1``. Device ids beyond N (rare;
    # >8 modules on one gateway) fall back to the raw key.
    _WRITE_LABEL_SLOTS = 8

    def _write_addresses_placeholders(self) -> dict[str, str]:
        """``{mod_<device_id>: "CHA @ 0x8", ...}`` for the field labels."""
        out: dict[str, str] = {}
        for device_id, (dev_type, read, _write) in self._device_map.items():
            if device_id <= self._WRITE_LABEL_SLOTS:
                out[f"mod_{device_id}"] = f"{dev_type} @ {read}"
        return out

    def _write_addresses_schema(self) -> vol.Schema:
        """One pre-filled write-address text field per configured module.

        The field key is ``write_<device_id>``; device ids are sequential
        from 1, so the keys line up with the localized ``write_1``..
        ``write_8`` labels declared in the translation files (each carrying
        the module identity via its ``{mod_<i>}`` placeholder).
        """
        schema_dict: dict[Any, Any] = {}
        for device_id, (_dev_type, read, write) in self._device_map.items():
            key = self._write_address_key(device_id)
            schema_dict[
                vol.Optional(key, default=write or read)
            ] = TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            )
        return vol.Schema(schema_dict)

    async def async_step_preset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 5: pick a preset (or 'manual' to hand-pick parameters).

        Annotated labels for the LIST selector -- the step description
        carries the long explanation, the per-option label just adds a hint
        next to the radio button (e.g. ``Standard (empfohlen)``). Values are
        unchanged from ``PRESETS`` so config-entry storage stays the same.
        """
        if user_input is not None:
            self._preset = user_input["preset"]
            await self._build_available_list()
            if self._preset == "manual":
                return await self.async_step_select()
            # Auto-pick by preset, skip the per-parameter selection step.
            return self._finish_with(self._preset_default_selection())

        return self.async_show_form(
            step_id="preset",
            data_schema=vol.Schema(
                {
                    vol.Required("preset", default="standard"): SelectSelector(
                        SelectSelectorConfig(
                            options=self._preset_options(),
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    @staticmethod
    def _preset_options() -> list[SelectOptionDict]:
        """Pretty per-option labels for the preset radio list.

        German first (matches the rest of the flow); the option ``value``
        stays in lowercase English so backend code remains untouched.
        """
        return [
            SelectOptionDict(value="minimal", label="Minimal"),
            SelectOptionDict(value="standard", label="Standard (empfohlen)"),
            SelectOptionDict(value="all", label="Alle Werte"),
            SelectOptionDict(value="manual", label="Manuell (Power-User)"),
        ]

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 6 (manual mode only): per-parameter checklist.

        Labels are prefixed with the device type (``CHA: Vorlauftemperatur``)
        so the user can tell which module a value belongs to. Sort key is
        ``(device_id, property_id)`` so values from the same module stay
        grouped. The selector uses DROPDOWN mode so HA shows a search box --
        with all parameters of 4-6 modules the LIST mode becomes hundreds
        of rows and unscrollable.
        """
        if user_input is not None:
            chosen = user_input.get("selected", [])
            selected = [self._key_from_label(label) for label in chosen]
            return self._finish_with(selected)

        labels = [self._label_for(item) for item in self._available]
        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema(
                {
                    vol.Required("selected", default=labels): SelectSelector(
                        SelectSelectorConfig(
                            options=labels,
                            multiple=True,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    async def _safe_disconnect(client: Any) -> None:
        # Bound the disconnect: writer.wait_closed() has no timeout of its
        # own and can hang on a half-open socket, which would freeze the
        # progress dialog even after the work is done.
        try:
            async with asyncio.timeout(10):
                await client.disconnect()
        except Exception:  # noqa: BLE001 -- shutdown best-effort (incl. TimeoutError)
            _LOGGER.debug("Client disconnect raised during config flow", exc_info=True)

    async def _load_catalog(self) -> Catalog:
        """Load the catalog once per flow (caches in ``self._catalog``).

        Delegates to ``_load_combined_catalog`` so the setup wizard sees the
        SAME catalog as a running entry -- including any user overrides from
        ``<config>/ecotherm_wolf_definitions`` (previously the wizard's
        device dropdown silently ignored them). The bundled dir is
        passed from THIS module's globals so tests patching
        ``config_flow.BUNDLED_DEFINITIONS_DIR`` keep working.
        """
        if self._catalog is not None:
            return self._catalog
        # Lazy import -- avoids a cycle at module import time.
        from . import _load_combined_catalog

        catalog = await _load_combined_catalog(
            self.hass, bundled_dir=BUNDLED_DEFINITIONS_DIR,
        )
        self._catalog = catalog
        return catalog

    async def _build_available_list(self) -> None:
        """Flatten parameters of selected devices into ``_available``."""
        catalog = await self._load_catalog()
        out: list[tuple[int, int, str, str]] = []
        for dev_id, (dev_type, _read, _write) in self._device_map.items():
            try:
                dev = catalog.get_device(dev_type)
            except KeyError:
                _LOGGER.warning("No definitions for device type %s", dev_type)
                continue
            for p in dev.parameters.values():
                out.append((dev_id, p.property_id, p.name, p.display_name))
        self._available = out

    def _preset_default_selection(self) -> list[tuple[int, int]]:
        """Pick the parameters that match the chosen preset for each device."""
        assert self._catalog is not None, "_build_available_list must run first"
        out: list[tuple[int, int]] = []
        for dev_id, (dev_type, _read, _write) in self._device_map.items():
            try:
                params = self._catalog.parameters_for_preset(dev_type, self._preset)
            except KeyError:
                continue
            for p in params:
                out.append((dev_id, p.property_id))
        return out

    def _label_for(self, item: tuple[int, int, str, str]) -> str:
        """``"1/0080 -- CHA: Vorlauftemperatur"``.

        The head before ``" -- "`` is the machine-readable ``dev_id/prop_id``
        pair that ``_key_from_label`` parses back; the suffix is the
        device-type prefix plus the parameter display name -- the prefix
        helps users distinguish identical parameter names across modules
        (e.g. ``Vorlauftemperatur`` on CHA vs. MK_BM-2).
        """
        dev_id, prop_id, _name, display = item
        dev_type = self._device_map.get(dev_id, ("?", "", ""))[0]
        return f"{dev_id}/{prop_id:04X} -- {dev_type}: {display}"

    def _key_from_label(self, label: str) -> tuple[int, int]:
        head = label.split(" -- ", 1)[0]
        dev, prop = head.split("/", 1)
        return (int(dev), int(prop, 16))

    def _finish_with(
        self, selected: list[tuple[int, int]]
    ) -> ConfigFlowResult:
        """Create the config entry.

        ``data`` holds connection + devices, ``options`` holds the parameter
        selection (so user can edit it later). ``CONF_DEVICES`` stores the
        shape ``{"1": ["CHA", "0x29", "0x3"]}`` (a 3-list of
        ``[device_type, read_address, write_address]``, since JSON has no
        tuples). Older 2-element entries still load -- the coordinator's
        ``_normalise_device_map`` defaults the write address to the read
        address for those.
        """
        return self.async_create_entry(
            title=f"EcoTherm Wolf {self._host}",
            data={
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_PASSWORD: self._password,
                CONF_DEVICES: {
                    str(k): [v[0], v[1], v[2]]
                    for k, v in self._device_map.items()
                },
            },
            options={
                CONF_SELECTED_PARAMETERS: [
                    [str(d), f"0x{p:04X}"] for d, p in selected
                ],
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for this config entry."""
        from .options_flow import EcothermWolfOptionsFlow

        return EcothermWolfOptionsFlow(config_entry)
