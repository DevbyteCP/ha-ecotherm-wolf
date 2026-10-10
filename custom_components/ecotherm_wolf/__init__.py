# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
# Derived in part from zivillian/ism7mqtt (GPL-3.0-or-later)
"""The EcoTherm Wolf integration."""
from __future__ import annotations

import logging
from functools import partial
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .client_factory import create_client
from .const import (
    CONF_DEVICES,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_REBOOT_AFTER_FAILURES,
    CONF_REBOOT_HOST,
    CONF_REBOOT_NOTIFY_SERVICE,
    CONF_REBOOT_PASSWORD,
    CONF_REBOOT_USERNAME,
    CONF_SELECTED_PARAMETERS,
    DEFAULT_ACTIVE_RESET_ON_STALE,
    DEFAULT_DEBUG_LOGGING,
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_REBOOT_AFTER_FAILURES,
    DEFAULT_REBOOT_USERNAME,
    DEFAULT_STALE_THRESHOLD_SECONDS,
    DOMAIN,
    RESOURCE_DIR,
    USER_OVERRIDE_DIR,
)
from .coordinator import EcothermWolfCoordinator, SubscriptionKey
from .definitions.catalog import (
    Catalog,
    UnknownDeviceError,
    UnknownParameterError,
)
from .definitions.loader import load_catalog_from_dir
from .definitions.wolf_loader import load_wolf_catalog
from .ism7.exceptions import AuthenticationError, Ism7Error
from .resources import (
    BUNDLED_CLIENT_CERT,
    CURATED_DATA_DIR,
    MissingResourcesError,
    resolve_cert,
    resolve_wolf_dir,
)
from .resources import BUNDLED_WOLF_DIR as _RESOURCES_BUNDLED_WOLF_DIR
from .runtime import EcothermWolfRuntime

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.BINARY_SENSOR,
]

# Production reads Wolf's real XMLs from ``<config>/ecotherm_wolf/``, falling
# back to BUNDLED_WOLF_DIR for installs that still carry them in the package.
# Tests that patch ``BUNDLED_DEFINITIONS_DIR`` to a fixtures directory still
# drive the legacy synthetic loader (back-compat -- see _load_combined_catalog).
BUNDLED_DEFINITIONS_DIR = Path(__file__).parent / "definitions" / "data" / "devices"
BUNDLED_WOLF_DIR = _RESOURCES_BUNDLED_WOLF_DIR


def _resource_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(RESOURCE_DIR))


async def _load_combined_catalog(
    hass: HomeAssistant,
    *,
    bundled_dir: Path | None = None,
    wolf_dir: Path | None = None,
) -> Catalog:
    """Load bundled Wolf XML definitions, optionally overlay user-override directory.

    Reads Wolf's real ``device.xml`` + ``parameter.xml`` from
    ``<config>/ecotherm_wolf/`` (fallback: the bundled
    ``definitions/data/wolf``; raises ``MissingResourcesError`` if neither
    has them), then optionally layers
    ``<config_dir>/ecotherm_wolf_definitions`` on top so users can
    override or add device definitions without forking the integration.

    ``bundled_dir`` / ``wolf_dir`` default to this module's globals; the
    config flow passes its own module globals so its test patch points
    (``config_flow.BUNDLED_DEFINITIONS_DIR``) keep working.

    Back-compat: if the test suite patches ``BUNDLED_DEFINITIONS_DIR`` to
    point at a synthetic fixtures directory (one with ``*.xml`` device
    files), we use that instead -- the synthetic loader is still imported.

    The override loader still uses the synthetic XML format so existing
    overrides keep working. A broken override directory creates a Repair
    Issue but does not abort the load -- the bundled definitions remain
    authoritative. On a successful override load the issue is cleared.
    """
    # Defaults resolved at call time (not in the signature) so tests can
    # patch the module globals.
    if bundled_dir is None:
        bundled_dir = BUNDLED_DEFINITIONS_DIR
    # Prefer the legacy synthetic loader if the bundled dir contains any
    # *.xml files (this is how the test fixtures set themselves up).
    use_legacy = await hass.async_add_executor_job(_has_xml_files, bundled_dir)
    if use_legacy:
        catalog = await hass.async_add_executor_job(
            load_catalog_from_dir, bundled_dir,
        )
    elif wolf_dir is not None:
        catalog = await hass.async_add_executor_job(load_wolf_catalog, wolf_dir)
    else:
        resolved = await hass.async_add_executor_job(
            partial(
                resolve_wolf_dir, _resource_dir(hass),
                bundled_wolf_dir=BUNDLED_WOLF_DIR,
                bundled_cert=BUNDLED_CLIENT_CERT,
            ),
        )
        catalog = await hass.async_add_executor_job(
            partial(load_wolf_catalog, resolved, curated_dir=CURATED_DATA_DIR),
        )
    override_path = Path(hass.config.path(USER_OVERRIDE_DIR))
    override_exists = await hass.async_add_executor_job(override_path.exists)
    if override_exists:
        # Lazy import: ``issue_registry`` lives in HA core; we want this module
        # to import cleanly in non-HA test contexts that exercise pure-logic.
        from homeassistant.helpers import issue_registry as ir
        try:
            override_cat = await hass.async_add_executor_job(
                load_catalog_from_dir, override_path,
            )
        except Exception:
            _LOGGER.exception(
                "Failed to load override definitions from %s", override_path,
            )
            ir.async_create_issue(
                hass, DOMAIN, "override_load_failed",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="override_load_failed",
            )
        else:
            for k, v in override_cat.devices.items():
                catalog.devices[k] = v
            _LOGGER.info(
                "Loaded %d device(s) from override directory %s",
                len(override_cat.devices), override_path,
            )
            # Clear any prior override-failure issue on success.
            ir.async_delete_issue(hass, DOMAIN, "override_load_failed")
    return catalog


def _has_xml_files(directory: Path) -> bool:
    """True if ``directory`` exists and contains at least one ``*.xml`` file.

    Used to detect when the test suite has patched ``BUNDLED_DEFINITIONS_DIR``
    to a synthetic-fixtures directory.
    """
    return directory.exists() and any(directory.glob("*.xml"))


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options are updated.

    Registered via ``entry.add_update_listener(...)`` in ``async_setup_entry``.
    Triggers ``async_unload_entry`` + ``async_setup_entry`` again so the new
    options are applied (heartbeat, stale threshold, debug logging, host/port).
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the EcoTherm Wolf integration from a config entry."""
    from homeassistant.helpers import issue_registry as ir

    try:
        catalog = await _load_combined_catalog(hass)
        cert_path = await hass.async_add_executor_job(
            partial(
                resolve_cert, _resource_dir(hass),
                bundled_wolf_dir=BUNDLED_WOLF_DIR,
                bundled_cert=BUNDLED_CLIENT_CERT,
            ),
        )
    except MissingResourcesError as e:
        # Not fatal: HA retries setup with backoff, so the entry comes up on
        # its own once the user has copied the files.
        ir.async_create_issue(
            hass, DOMAIN, "missing_wolf_files",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="missing_wolf_files",
            translation_placeholders={
                "path": str(e.user_dir), "files": ", ".join(e.missing),
            },
        )
        raise ConfigEntryNotReady(str(e)) from e
    ir.async_delete_issue(hass, DOMAIN, "missing_wolf_files")

    # Apply debug-logging setting (impacts the entire ``ecotherm_wolf`` logger
    # tree) before the first client/coordinator log line so user toggles take
    # effect on reload, not just on HA restart.
    _apply_debug_logging(entry.options.get("debug_logging", DEFAULT_DEBUG_LOGGING))

    client = create_client(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        password=entry.data[CONF_PASSWORD],
        cert_path=cert_path,
    )
    is_active = bool(entry.options.get("is_active", True))
    if is_active:
        try:
            await client.connect()
        except AuthenticationError as e:
            # Wrong password -- ask HA to launch the re-auth flow.
            _LOGGER.error("Authentication failed for ISM7: %s", e)
            raise ConfigEntryAuthFailed(str(e)) from e
        except (ConnectionRefusedError, TimeoutError, OSError, Ism7Error) as e:
            # Transient: HA will retry with backoff.
            _LOGGER.warning("Failed to connect to ISM7 (will retry): %s", e)
            raise ConfigEntryNotReady(str(e)) from e
    else:
        _LOGGER.info(
            "EcoTherm Wolf entry %s loaded paused -- skipping connect "
            "(turn the Active switch on to resume)", entry.entry_id,
        )

    # ``CONF_DEVICES`` may carry the new (device_type, bus_address) shape
    # (stored as a 2-list for JSON compat) or the legacy plain-string shape
    # (pre-T21d). The coordinator normalises both.
    device_map: dict[int, Any] = {
        int(k): v for k, v in entry.data.get(CONF_DEVICES, {}).items()
    }
    raw_selected: list[SubscriptionKey] = [
        (int(d), int(p, 16) if isinstance(p, str) else int(p))
        for d, p in entry.options.get(CONF_SELECTED_PARAMETERS, [])
    ]
    # Catalog-dedup can drop a PTID a stored entry still references (an old
    # entry from before dedup, or a hand-picked redundant variant). Filter
    # those out here so the entity platforms -- which look the parameter up
    # without a guard -- never see a now-missing PTID. Unknown devices are
    # skipped the same way. Both are logged so the user can re-pick if needed.
    selected: list[SubscriptionKey] = []
    for device_id, property_id in raw_selected:
        # Derive the device_type shape-tolerantly: current entries are
        # 3-lists ``[type, read, write]``, legacy ones 2-lists or a plain
        # type string (indexing a bare string would yield its first CHAR).
        raw_entry = device_map.get(device_id)
        if isinstance(raw_entry, tuple | list) and raw_entry:
            device_type: str | None = str(raw_entry[0])
        elif isinstance(raw_entry, str):
            device_type = raw_entry
        else:
            device_type = None
        if device_type is None:
            _LOGGER.warning(
                "Dropping selected parameter for unknown device id %s "
                "(0x%04X) -- re-check the integration options",
                device_id, property_id,
            )
            continue
        try:
            catalog.get_parameter(device_type, property_id)
        except (UnknownDeviceError, UnknownParameterError):
            _LOGGER.warning(
                "Dropping selected parameter %s/0x%04X -- no longer in the "
                "catalog (likely deduplicated); re-check the integration "
                "options to pick the surviving value",
                device_type, property_id,
            )
            continue
        selected.append((device_id, property_id))

    heartbeat = int(
        entry.options.get("heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS),
    )
    stale_threshold = int(
        entry.options.get(
            "stale_threshold_seconds", DEFAULT_STALE_THRESHOLD_SECONDS,
        ),
    )

    # ISM7 reboot watchdog (Options Flow). The reboot host defaults to the
    # gateway host -- the firmware reboot endpoint lives on the same device.
    # It stays inert until a reboot password is entered (see _reboot_enabled).
    reboot_after_failures = int(
        entry.options.get(
            CONF_REBOOT_AFTER_FAILURES, DEFAULT_REBOOT_AFTER_FAILURES,
        ),
    )
    reboot_host = (
        entry.options.get(CONF_REBOOT_HOST, "") or entry.data[CONF_HOST]
    )
    reboot_username = entry.options.get(
        CONF_REBOOT_USERNAME, DEFAULT_REBOOT_USERNAME,
    )
    reboot_password = entry.options.get(CONF_REBOOT_PASSWORD, "")
    reboot_notify_service = entry.options.get(CONF_REBOOT_NOTIFY_SERVICE, "")

    coordinator = EcothermWolfCoordinator(
        hass=hass,
        client=client,
        catalog=catalog,
        device_map=device_map,
        selected=selected,
        heartbeat_seconds=heartbeat,
        stale_threshold_seconds=stale_threshold,
        is_active=is_active,
        reboot_after_failures=reboot_after_failures,
        reboot_host=reboot_host,
        reboot_username=reboot_username,
        reboot_password=reboot_password,
        reboot_notify_service=reboot_notify_service,
    )
    # Wire optional tuning flags from the Options Flow.
    coordinator.set_active_reset_enabled(
        entry.options.get("active_reset_on_stale", DEFAULT_ACTIVE_RESET_ON_STALE)
    )
    # Any failure between here and async_forward_entry_setups would leak the
    # connected client (HA only invokes async_unload_entry once runtime_data
    # is set + platforms forwarded). Tear it down explicitly on the way out.
    try:
        await coordinator.async_setup()
        entry.runtime_data = EcothermWolfRuntime(
            client=client, catalog=catalog, coordinator=coordinator
        )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await coordinator.async_shutdown()
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 -- best-effort cleanup
            _LOGGER.debug("client disconnect raised during setup unwind", exc_info=True)
        raise

    # Reload the entry whenever the user changes options (heartbeat / stale /
    # debug / host:port:password). Without this, edits in the Options Flow
    # would only take effect at the next HA restart.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Register integration-wide services. Idempotent across multiple entries;
    # imported lazily to avoid circular-import issues at module load.
    from .services import async_register as _register_services
    _register_services(hass)
    return True


def _apply_debug_logging(enabled: bool) -> None:
    """Toggle the integration's logger between DEBUG and NOTSET (inherit)."""
    logger = logging.getLogger("custom_components.ecotherm_wolf")
    logger.setLevel(logging.DEBUG if enabled else logging.NOTSET)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an EcoTherm Wolf config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and entry.runtime_data:
        runtime = entry.runtime_data
        if runtime.coordinator is not None:
            await runtime.coordinator.async_shutdown()
        await runtime.client.disconnect()
    # If this was the last entry, unregister services.
    if not hass.config_entries.async_entries(DOMAIN):
        from .services import async_unregister as _unregister_services
        _unregister_services(hass)
    return unload_ok
