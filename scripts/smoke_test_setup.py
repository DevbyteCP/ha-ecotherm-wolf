# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Smoke-test phase 2 v2: full 2-stage flow against real ISM7.

Tests T21e's improvements over the failed smoke-test 2:
- ConverterTemplate-based info_numbers (multi-telegram aware)
- Bundle-Builder with max 20 reads/bundle, parameter-grouping
- Sequential bundle IDs ("1", "2", ...) and Seq-IDs
- AbortOnError=false for pull
- KeepAlive in parallel
- Optional --subscribe to test push subscriptions for ~90s

Reads 5 parameters from the author's parameter.json — DTID 270000 (CHA, BusAddress 0x8).

Usage:
    python scripts/smoke_test_setup.py <host> <password>
    python scripts/smoke_test_setup.py <host> <password> --subscribe
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

from custom_components.ecotherm_wolf.coordinator import build_bundles  # noqa: E402
from custom_components.ecotherm_wolf.definitions.wolf_loader import load_wolf_catalog  # noqa: E402
from custom_components.ecotherm_wolf.ism7.client import Ism7Client  # noqa: E402
from custom_components.ecotherm_wolf.ism7.protocol import (  # noqa: E402
    KeepAliveReq,
    TelegramBundleReq,
    TelegramBundleResp,
)

# The author's parameter.json — DTID 270000 (CHA), BusAddress 0x8
DEVICE_ID = 1  # arbitrary local id (not on the wire)
BUS_ADDRESS = "0x8"
DTID = 270000

# Konservatives Test-Set: 5 Parameter aus der 44-PTID-Liste des Autors
TEST_PTIDS = [270010, 270017, 270025, 270026, 270049]


async def run_keepalive(client: Ism7Client, stop_event: asyncio.Event) -> None:
    """Send KeepAlive every 30s until stop_event is set."""
    seq = 0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30.0)
            return  # stop_event was set
        except TimeoutError:
            pass  # 30s elapsed, send keepalive
        if not client.connected:
            continue
        seq = (seq + 1) % 32767
        log.info(f"KeepAlive seq={seq}")
        try:
            await client.send(KeepAliveReq(seq=seq))
        except Exception:
            log.exception("KeepAlive send failed")


def decode_collected(test_device, bus_addr: str, all_telegrams: list, ptids: list[int]) -> dict:
    """Group telegrams by info_number, decode each parameter via its converter."""
    by_info_nr: dict[int, tuple[int, int]] = {}
    for t in all_telegrams:
        if t.state.lower() != "ok" or t.bus_address != bus_addr:
            continue
        try:
            dl = int(t.db_low, 16) if t.db_low else 0
            dh = int(t.db_high, 16) if t.db_high else 0
        except ValueError:
            continue
        by_info_nr[t.info_number] = (dl, dh)

    decoded: dict[int, tuple] = {}
    for ptid in ptids:
        param = test_device.parameters.get(ptid)
        if param is None or param.converter is None:
            continue
        if not all(n in by_info_nr for n in param.info_numbers):
            continue
        try:
            value = param.converter.decode(by_info_nr)
            decoded[ptid] = (param, value)
        except Exception as e:
            log.warning(f"decode failed for PTID {ptid}: {type(e).__name__}: {e}")
    return decoded


async def main(host: str, password: str, do_subscribe: bool) -> int:
    # ---- Load catalog ----
    wolf_dir = (
        Path(__file__).parent.parent
        / "custom_components" / "ecotherm_wolf" / "definitions" / "data" / "wolf"
    )
    log.info("Loading Wolf catalog...")
    catalog = await asyncio.get_event_loop().run_in_executor(
        None, load_wolf_catalog, wolf_dir
    )
    log.info(f"Catalog: {len(catalog.devices)} devices")

    test_device = next((d for d in catalog.devices.values() if d.dtid == DTID), None)
    if test_device is None:
        log.error(f"DTID {DTID} not in catalog — abort")
        return 1
    log.info(f"Test device: {test_device.device_type} (DTID {DTID})")

    for ptid in TEST_PTIDS:
        param = test_device.parameters.get(ptid)
        if param is None:
            log.warning(f"  PTID {ptid}: NOT in catalog (skipped)")
        elif param.converter is None:
            log.warning(f"  PTID {ptid}: '{param.display_name}' — no converter (skipped)")
        else:
            log.info(
                f"  PTID {ptid}: '{param.display_name}' "
                f"conv={type(param.converter).__name__} "
                f"info_numbers={param.info_numbers}"
            )

    # ---- Build bundles via real coordinator code ----
    selected = [(DEVICE_ID, ptid) for ptid in TEST_PTIDS]
    device_map = {DEVICE_ID: (test_device.device_type, BUS_ADDRESS)}
    bundles = build_bundles(selected, catalog, device_map)
    log.info(f"Built {len(bundles)} bundle(s):")
    for idx, bundle in enumerate(bundles, start=1):
        log.info(f"  Bundle '{idx}': {len(bundle)} info_reads")

    # ---- Connect ----
    client = Ism7Client(host=host, port=9092, password=password, use_tls=True)
    log.info(f"Connecting to {host}:9092 (TLS)...")
    try:
        await asyncio.wait_for(client.connect(), timeout=15.0)
    except Exception as e:
        log.error(f"connect FAILED: {type(e).__name__}: {e}", exc_info=True)
        return 2
    log.info(f"Connected: {client.connected}")

    # ---- Start KeepAlive in parallel ----
    stop_event = asyncio.Event()
    keepalive_task = asyncio.create_task(run_keepalive(client, stop_event))

    seq_counter = 0

    def next_seq() -> str:
        nonlocal seq_counter
        seq_counter += 1
        return str(seq_counter)

    try:
        # ---- Stage 1: initial pull ----
        log.info("=== Stage 1: initial pull (AbortOnError=False) ===")
        all_reads: list = []
        for bundle_idx, info_reads in enumerate(bundles, start=1):
            bundle_id = str(bundle_idx)
            for ir in info_reads:
                ir.seq = next_seq()
            req = TelegramBundleReq(
                bundle_id=bundle_id,
                bundle_type="pull",
                abort_on_error=False,
                info_reads=info_reads,
            )
            log.info(f"  → pull bundle '{bundle_id}' ({len(info_reads)} reads)")
            try:
                resp = await asyncio.wait_for(client.request(req, timeout=10.0), timeout=15.0)
            except TimeoutError:
                log.error(f"  TIMEOUT on bundle '{bundle_id}'")
                return 3
            except Exception as e:
                log.error(f"  pull FAILED: {type(e).__name__}: {e}", exc_info=True)
                return 4

            if not isinstance(resp, TelegramBundleResp):
                log.error(f"  unexpected response type: {type(resp).__name__}")
                return 5

            log.info(
                f"  ← state={resp.state} bn={resp.bundle_id} "
                f"reads={len(resp.read_telegrams)} err={resp.error_msg or '-'}"
            )
            all_reads.extend(resp.read_telegrams)
            for t in resp.read_telegrams:
                log.info(
                    f"      irs ba={t.bus_address} in={t.info_number} "
                    f"st={t.state} dl='{t.db_low}' dh='{t.db_high}'"
                )

        # ---- Decode ----
        log.info("=== Decoded values from initial pull ===")
        decoded = decode_collected(test_device, BUS_ADDRESS, all_reads, TEST_PTIDS)
        if not decoded:
            log.warning("  No values decoded — something is off")
        else:
            for _ptid, (param, value) in decoded.items():
                unit = (param.codec.unit or "") if hasattr(param.codec, "unit") else ""
                log.info(f"  ✓ {param.display_name} = {value} {unit}".rstrip())

        # ---- Optional Stage 2: subscribe + listen ----
        if do_subscribe:
            log.info("=== Stage 2: subscribe (push, Intervall=60) — listening 90s ===")

            push_count = 0

            def on_push(resp: TelegramBundleResp) -> None:
                nonlocal push_count
                push_count += 1
                log.info(
                    f"  PUSH #{push_count}: bn={resp.bundle_id} "
                    f"reads={len(resp.read_telegrams)}"
                )
                pushed = decode_collected(test_device, BUS_ADDRESS, resp.read_telegrams, TEST_PTIDS)
                for _ptid, (param, value) in pushed.items():
                    log.info(f"    ↑ {param.display_name} = {value}")

            client.on_update(on_push)

            for bundle_idx, info_reads in enumerate(bundles, start=1):
                bundle_id = str(bundle_idx + len(bundles))
                for ir in info_reads:
                    ir.seq = next_seq()
                    ir.interval = 60
                req = TelegramBundleReq(
                    bundle_id=bundle_id,
                    bundle_type="push",
                    abort_on_error=False,
                    info_reads=info_reads,
                )
                log.info(f"  → subscribe bundle '{bundle_id}'")
                try:
                    await client.send(req)
                except Exception as e:
                    log.error(f"  subscribe send FAILED: {type(e).__name__}: {e}")
                    return 6

            log.info("  Listening for pushes for 90 seconds...")
            await asyncio.sleep(90)
            log.info(f"  Received {push_count} push update(s)")

    finally:
        log.info("Stopping keepalive + disconnecting...")
        stop_event.set()
        keepalive_task.cancel()
        try:
            await keepalive_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5.0)
        except Exception:
            pass

    log.info("=== DONE ===")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    do_subscribe = "--subscribe" in args
    args = [a for a in args if a != "--subscribe"]
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    sys.exit(asyncio.run(main(args[0], args[1], do_subscribe)))
