# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Smoke-test phase 2: pull a few real values from the ISM7.

Reads 3 parameters from BusAddress 0x8 (DTID 270000) — the author's config has this device.
Read-only operation, no writes, no subscribe.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)5s] %(name)s: %(message)s",
)
log = logging.getLogger("smoke")

from custom_components.ecotherm_wolf.definitions.wolf_loader import load_wolf_catalog  # noqa: E402
from custom_components.ecotherm_wolf.ism7.client import Ism7Client  # noqa: E402
from custom_components.ecotherm_wolf.ism7.protocol import (  # noqa: E402
    InfoRead,
    TelegramBundleReq,
    TelegramBundleResp,
)

# 3 parameters from the author's config — DTID 270000, BusAddress 0x8
TEST_BUS_ADDRESS = "0x8"
TEST_DTID = 270000
TEST_PARAMS = [270010, 270017, 270025]


async def main(host: str, password: str) -> int:
    # Step 1: load catalog
    wolf_dir = (
        Path(__file__).parent.parent
        / "custom_components"
        / "ecotherm_wolf"
        / "definitions"
        / "data"
        / "wolf"
    )
    log.info(f"Loading Wolf catalog from {wolf_dir}")
    catalog = await asyncio.get_running_loop().run_in_executor(
        None, load_wolf_catalog, wolf_dir
    )
    log.info(f"Catalog loaded: {len(catalog.devices)} device templates")

    # Find device by DTID + look up the test parameters
    test_device = None
    for dev_name, dev in catalog.devices.items():
        if dev.dtid == TEST_DTID:
            test_device = dev
            log.info(f"DTID {TEST_DTID} → device_type='{dev_name}', display='{dev.display_name}'")
            break
    if test_device is None:
        log.error(f"DTID {TEST_DTID} not in catalog — abort")
        return 1

    for ptid in TEST_PARAMS:
        param = test_device.parameters.get(ptid)
        if param is None:
            log.warning(f"  PTID {ptid}: NOT in template '{test_device.device_type}'")
        else:
            log.info(
                f"  PTID {ptid}: name='{param.name}' display='{param.display_name}' "
                f"codec={type(param.codec).__name__}"
            )

    # Step 2: connect
    client = Ism7Client(host=host, port=9092, password=password, use_tls=True)
    log.info(f"Connecting to {host}:9092 ...")
    try:
        await asyncio.wait_for(client.connect(), timeout=15.0)
    except Exception as e:
        log.error(f"connect FAILED: {type(e).__name__}: {e}")
        return 2
    log.info(f"Connected: {client.connected}")

    try:
        # Step 3: pull bundle
        log.info(
            "Sending TgrBundleReq type=pull for %d params on BusAddress %s",
            len(TEST_PARAMS), TEST_BUS_ADDRESS,
        )
        req = TelegramBundleReq(
            bundle_id="smoke_pull_1",
            bundle_type="pull",
            info_reads=[
                InfoRead(info_number=ptid, bus_address=TEST_BUS_ADDRESS)
                for ptid in TEST_PARAMS
            ],
        )
        try:
            resp = await asyncio.wait_for(client.request(req, timeout=10.0), timeout=15.0)
        except TimeoutError:
            log.error("TIMEOUT waiting for TgrBundleResp")
            return 3
        except Exception as e:
            log.error(f"request FAILED: {type(e).__name__}: {e}", exc_info=True)
            return 4

        log.info(f"Response type: {type(resp).__name__}")
        if not isinstance(resp, TelegramBundleResp):
            log.error(f"Expected TelegramBundleResp, got {type(resp).__name__}: {resp!r}")
            return 5

        log.info(
            f"TgrBundleResp: state={resp.state} bundle_id={resp.bundle_id} "
            f"timestamp={resp.timestamp} reads={len(resp.read_telegrams)}"
        )
        if resp.error_msg:
            log.warning(f"  error_msg: {resp.error_msg}")

        # Step 4: decode and display
        for t in resp.read_telegrams:
            param = test_device.parameters.get(t.info_number) if test_device else None
            log.info(
                f"  irs: ba={t.bus_address} in={t.info_number} st={t.state} "
                f"dl='{t.db_low}' dh='{t.db_high}'"
            )
            if t.state != "ok":
                log.warning(f"    state={t.state} — skipping decode")
                continue
            if param is None:
                log.warning("    no param in catalog — skipping decode")
                continue
            try:
                # Wolf wire convention: dh is high byte, dl is low byte
                if hasattr(param.codec, "base"):
                    # ScaledCodec — wire is the base codec's width
                    raw = bytes.fromhex(t.db_high) + bytes.fromhex(t.db_low)
                else:
                    # Heuristic: if codec has uint8/bool feel, single byte; else 2 bytes
                    raw = bytes.fromhex(t.db_high) + bytes.fromhex(t.db_low)
                value = param.codec.decode(raw)
                unit = getattr(param.codec, "unit", None) or ""
                log.info(f"    → {param.display_name} = {value} {unit}")
            except Exception as e:
                log.warning(f"    decode FAILED: {type(e).__name__}: {e}")

    finally:
        log.info("Disconnecting...")
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5.0)
        except Exception:
            pass

    log.info("=== DONE ===")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
