# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Smoke-test: TLS connect + DirectLogon + immediate disconnect against real ISM7.

Usage (from project root, in activated venv):
    python scripts/smoke_test_connect.py <host> <password>

This is read-only — opens TLS, sends DirectLogonReq, reads DirectLogonResp, disconnects.
No subscribe, no write.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Make the package importable
sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)5s] %(name)s: %(message)s",
)
log = logging.getLogger("smoke")

from custom_components.ecotherm_wolf.ism7.client import Ism7Client  # noqa: E402


async def main(host: str, password: str) -> int:
    client = Ism7Client(host=host, port=9092, password=password, use_tls=True)

    log.info("=== Phase 1: TLS handshake + DirectLogon ===")
    log.info("Calling client.connect() with 15s timeout...")
    try:
        await asyncio.wait_for(client.connect(), timeout=15.0)
    except TimeoutError:
        log.error("TIMEOUT during connect — TLS or login hung")
        return 1
    except Exception as e:
        log.error(f"connect FAILED: {type(e).__name__}: {e}", exc_info=True)
        return 2

    log.info(f"connect() returned. client.connected={client.connected}")

    if not client.connected:
        log.error("client.connected is False after connect() returned — unexpected")
        return 3

    log.info("=== Phase 2: brief idle to see if any unsolicited frames arrive ===")
    await asyncio.sleep(2.0)

    log.info("=== Phase 3: clean disconnect ===")
    try:
        await asyncio.wait_for(client.disconnect(), timeout=5.0)
    except Exception as e:
        log.warning(f"disconnect issue (non-fatal): {type(e).__name__}: {e}")

    log.info("=== SUCCESS — TLS + Login + Disconnect cycle completed ===")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    rc = asyncio.run(main(sys.argv[1], sys.argv[2]))
    sys.exit(rc)
