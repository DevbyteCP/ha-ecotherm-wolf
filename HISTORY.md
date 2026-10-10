# Changelog

All notable changes to the **EcoTherm Wolf** integration.

## 2.0.0

### Fixed
- **Multi-telegram values (32-bit counters) no longer freeze in live
  operation.** ``Verdichterstarts``, operating-hour counters and the
  Wärmemengen meters are assembled from TWO bus telegrams (low + high
  word). The ISM7's push frames usually carry only the telegram whose
  value changed, so the two halves almost never arrived in the same frame
  -- the decoder required exactly that, and the value silently froze until
  the next reconnect's full pull snapshot (observed live: compressor-start
  counter only advancing on reconnects, sometimes catching up +2 at once).
  The coordinator now keeps a per-connection **telegram cache** across push
  frames and decodes against the merged view, combining a freshly pushed
  half with the last-seen other half -- the same approach zivillian's
  ``NumericConverter32Template`` uses with its persistent ``_low``/``_high``
  fields. The cache is cleared on every reconnect/reactivate (session
  boundary); the fresh initial pull repopulates it in one sweep.
  Single-telegram values are unaffected.

  This also matters beyond cosmetics: EcoTherm Core derives its
  "WP started today" gate from the compressor-start counter -- a frozen
  counter could have allowed a second compressor start per day.

## 1.0.6

Maintenance release -- fixes from a full-codebase audit.

### Fixed
- **Diagnostics no longer leak the reboot password.** `entry.options`
  (which carries the ISM7 web-UI credentials for the reboot watchdog since
  1.0.5) is now redacted in the diagnostics download, same as the ISM7
  password in `entry.data`.
- **Client reuse after disconnect.** `Ism7Client.connect()` now resets the
  internal stop flag, so a disconnect → connect cycle gets a live reconnect
  supervisor again (previously the supervisor exited immediately and the
  connection would never auto-reconnect; not reachable through the UI, but a
  landmine for programmatic use of `coordinator.async_set_active`).
- **"Write Errors 24h" sensor is now actually fed.** Every failed write
  (gateway timeout, rejected/unacknowledged telegram, connection error,
  vanished parameter) stamps the rolling 24h window; previously the counter
  could never leave 0. README wording fixed accordingly (writes are verified
  against the gateway's per-telegram ACK, not by read-back).
- **Clean error toasts for two more write failure modes:** a parameter that
  vanished from the catalog (`reload_definitions` after an override edit) and
  an unencodable value now surface as proper Home Assistant errors instead of
  "unknown error".
- **Legacy device-map entries.** A pre-T21d plain-string device entry no
  longer derives its device type from the string's first character during
  the startup selection filter.

### Changed
- **Robustness:** frame decoding rejects absurd payload lengths (> 1 MiB
  sanity cap) instead of buffering gigabytes on a desynced stream; the
  client's orphan-response queue is bounded (64 entries, oldest dropped) and
  drained on every disconnect.
- **UI step for decimal setpoints:** number entities backed by a scaled
  codec now step in the codec's resolution (e.g. 0.1 °C) instead of HA's
  integer default -- half-degree setpoints are reachable from the UI again.
- **Setup wizard sees definition overrides:** the config flow now loads the
  same combined catalog as a running entry, so devices from
  `<config>/ecotherm_wolf_definitions/` appear in the manual-add dropdown.
- **Services raise `ServiceValidationError`** for runtime problems (unknown
  device/property, no entry configured) -- HA renders a clean validation
  message instead of a generic failure.
- README: removed the never-offered `extended` preset from the docs; the
  flow offers `minimal` / `standard` / `all` / `manual`.

## 1.0.5

### Added
- **ISM7 reboot watchdog.** When the auto-reconnect can't re-establish the
  link after *N* consecutive attempts (configurable, default **5**), the
  integration power-cycles the ISM7 gateway once over its HTTP firmware
  endpoint (`POST /protect/reboot.htm`, HTTP basic auth), then keeps
  reconnecting. A cooldown (default 10 min) prevents a reboot loop while the
  gateway boots. Replaces the old external "reboot when the BM-2 system clock
  drifts" HA automation, which broke once the integration stopped exposing a
  BM-2 system-time sensor.
  - New **"ISM7-Reboot-Watchdog"** tab in the integration options: failure
    threshold, reboot host (defaults to the gateway host), reboot username
    (default `admin`), reboot password, and an optional notify service.
  - **Opt-in / portable:** the reboot stays inactive until a reboot password
    is entered, so the integration ships without any host-specific
    dependency (no `configuration.yaml` `shell_command` required).
  - The client now tracks `consecutive_connect_failures`; each triggered
    reboot is logged at WARNING and can fire a configurable notify service.
  - New **"ISM7 Auto-Reboot"** switch on the Health device — a runtime
    on/off for the auto-reboot (the normal reconnect keeps running). Persists
    across restarts via `RestoreEntity`, so toggling it does not reload the
    entry or drop the live connection.

## 1.0.4

### Added
- **Icons, device classes and state classes for every entity.** Each entity
  the integration generates now carries a meaningful icon plus a matching
  `device_class` / `state_class` where Home Assistant accepts the unit, so
  nothing falls back to the generic sensor glyph. A `device_class` is only
  assigned when HA's `DEVICE_CLASS_UNITS` accepts the exact unit; everything
  else gets an icon only, so an entity can never fail to load over a
  class/unit mismatch. Entity IDs, names and unique IDs are unchanged.

## 1.0.3

### Fixed
- **Reconnect supervisor.** A peer reset (`ConnectionResetError` when the
  ISM7 drops its single TLS session) no longer escapes and kills the
  supervisor — it is caught and falls through to the reconnect backoff.

### Added
- **"Kritischer Fehler (Connect nicht möglich)"** binary sensor on the Health
  device — turns on during a sustained outage so a notification can fire.
