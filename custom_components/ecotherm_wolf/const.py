# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Constants for the EcoTherm Wolf integration."""
from __future__ import annotations

DOMAIN = "ecotherm_wolf"

# Config keys
CONF_HOST = "host"
CONF_PORT = "port"
CONF_PASSWORD = "password"
CONF_SELECTED_PARAMETERS = "selected_parameters"
CONF_DEVICES = "devices"

# Config keys -- ISM7 reboot watchdog (Options Flow, stored in entry.options)
CONF_REBOOT_AFTER_FAILURES = "reboot_after_failures"
CONF_REBOOT_HOST = "reboot_host"
CONF_REBOOT_USERNAME = "reboot_username"
CONF_REBOOT_PASSWORD = "reboot_password"
CONF_REBOOT_NOTIFY_SERVICE = "reboot_notify_service"

# Defaults
DEFAULT_PORT = 9092
DEFAULT_HEARTBEAT_SECONDS = 60
# Bounds + step for the Health-device heartbeat Number entity. Below 30 s
# the gateway floods (KeepAlive cadence is also 60 s); above 300 s entity
# states feel stale even when the integration is healthy.
DEFAULT_HEARTBEAT_MIN_SECONDS = 30
DEFAULT_HEARTBEAT_MAX_SECONDS = 300
DEFAULT_HEARTBEAT_STEP_SECONDS = 10
DEFAULT_STALE_THRESHOLD_SECONDS = 240          # 4 minutes
DEFAULT_RECONNECT_REPAIR_SECONDS = 300         # 5 minutes
DEFAULT_STALE_REPAIR_SECONDS = 600             # 10 minutes
# The ISM7 runs a full eBus write transaction (translate the iwr, push it
# on the bus, await the module's ack) before answering -- a cold first
# write regularly needs well over 5 s. zivillian uses no write timeout at
# all; this is a generous backstop (a dead socket still fails fast because
# the client cancels every pending bundle on disconnect).
DEFAULT_WRITE_TIMEOUT_SECONDS = 20.0
# Hard cap on the TCP+TLS connect. asyncio.open_connection has NO built-in
# timeout -- if the ISM7 accepts the TCP SYN but stalls the TLS handshake
# (e.g. its single connection slot is still occupied / recovering), the
# connect hangs forever. Bound it so connect() fails cleanly instead.
CONNECT_TIMEOUT_SECONDS = 15.0
DEFAULT_ACTIVE_RESET_ON_STALE = True
DEFAULT_DEBUG_LOGGING = False

# ISM7 reboot watchdog. When the auto-reconnect can't re-establish the link
# after this many *consecutive* failed attempts, the integration reboots the
# ISM7 gateway once over its HTTP firmware endpoint, then keeps reconnecting.
# 0 disables it. It also stays disabled until a reboot password is configured
# -- the endpoint is HTTP-basic-auth protected, so without credentials there
# is nothing to call (keeps the feature opt-in / portable for HACS users).
DEFAULT_REBOOT_AFTER_FAILURES = 5
# Don't reboot again within this window. The gateway needs ~1-2 min to boot,
# during which reconnects keep failing -- without a cooldown the climbing
# failure count would trigger reboot after reboot (a reboot loop).
DEFAULT_REBOOT_COOLDOWN_SECONDS = 600          # 10 minutes
DEFAULT_REBOOT_USERNAME = "admin"
# The ISM7 firmware reboot endpoint. Mirrors the curl that used to live in a
# HA ``shell_command`` (``curl -u user:pass -d name=reboKickoff -d rebo=init
# http://<host>/protect/reboot.htm``): an HTTP-basic-auth, form-urlencoded
# POST. Credentials are NOT stored here -- they come from the Options Flow.
REBOOT_PATH = "/protect/reboot.htm"
REBOOT_FORM_DATA = {"name": "reboKickoff", "rebo": "init"}
REBOOT_HTTP_TIMEOUT_SECONDS = 15.0

# Reconnect backoff sequence (seconds)
RECONNECT_BACKOFF = (1, 2, 4, 8, 16, 30, 60)

# Storage keys
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.devices"

# Override directory (relative to HA config)
USER_OVERRIDE_DIR = "ecotherm_wolf_definitions"

# Directory for the Wolf XMLs + client certificate (relative to HA config).
# Outside custom_components/ so HACS updates never delete the files.
RESOURCE_DIR = "ecotherm_wolf"

# Device-registry presentation. All devices (the Wolf modules and the
# Health pseudo-device) are shown under this manufacturer, matching the
# EcoTherm Core integration. Keep SW_VERSION in sync with manifest.json.
MANUFACTURER = "DevbyteCP"
SW_VERSION = "2.1.0"

# Gateway-id used in ISM7 wire requests (``sid`` attribute on
# ``SystemConfigReq`` and ``gw`` attribute on ``TelegramBundleReq``).
# Zivillians ``TelegrBundleRequestFactory`` hardcodes ``GatewayId="1"`` and
# the live ISM7 firmware closes the TCP connection on a SystemConfigReq with
# an empty ``sid``, so we always send "1" until/unless a future
# DirectLogonResp carries a usable session id.
DEFAULT_GATEWAY_ID = "1"
