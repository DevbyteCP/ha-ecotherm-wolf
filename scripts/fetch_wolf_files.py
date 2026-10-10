# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Download the Wolf files from zivillian/ism7mqtt and verify them.

EcoTherm Wolf does not ship Wolf's XML definitions or the ISM7 client
certificate. This script fetches exactly the versions the integration is
tested with (pinned upstream commit, SHA-256 checked).

    python3 fetch_wolf_files.py /config/ecotherm_wolf
    python3 scripts/fetch_wolf_files.py --bundled   # test checkouts / CI
    python3 fetch_wolf_files.py /config/ecotherm_wolf --from /path/to/copy

``--from`` takes the files from a local directory instead (any copy of
ism7mqtt, e.g. if the upstream repository is gone); they are checked
against the same SHA-256 values.

Standard library only, so it also runs in the Home Assistant terminal.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

UPSTREAM_COMMIT = "3aa40b1ad28eff8c112ad6a0d17c21800a7476b9"
UPSTREAM_RAW = f"https://raw.githubusercontent.com/zivillian/ism7mqtt/{UPSTREAM_COMMIT}"

# name -> (upstream path, sha256)
FILES: dict[str, tuple[str, str]] = {
    "device.xml": (
        "src/ism7mqtt/Resources/device.xml",
        "2862f271d6230aa012b133513db8ef1471d642efa13676f68bed8b0f84c315c6",
    ),
    "parameter.xml": (
        "src/ism7mqtt/Resources/parameter.xml",
        "b3115ee7587ccc6b265b1b962878f914479b18c888e50b725093584f68722f69",
    ),
    "converter.xml": (
        "src/ism7mqtt/Resources/converter.xml",
        "3689736e3220f887745e8b6da5c82ee73964935ac082cec2cac91cca33fe84d7",
    ),
    "client.pfx": (
        "src/ism7ssl/Resources/client.pfx",
        "0d6b9efa3fa0a152e01f0f6a638b97823966a75b871bd51ab44ae751ab52b8a7",
    ),
}

_PACKAGE = Path(__file__).resolve().parent.parent / "custom_components" / "ecotherm_wolf"
BUNDLED_TARGETS = {
    "device.xml": _PACKAGE / "definitions" / "data" / "wolf",
    "parameter.xml": _PACKAGE / "definitions" / "data" / "wolf",
    "converter.xml": _PACKAGE / "definitions" / "data" / "wolf",
    "client.pfx": _PACKAGE / "ism7",
}


def fetch(targets: dict[str, Path], source: Path | None = None) -> None:
    # Download and verify everything first, so a bad file never leaves a
    # half-updated set behind.
    downloaded: dict[str, bytes] = {}
    for name, (upstream_path, sha256) in FILES.items():
        if source is not None:
            matches = sorted(p for p in source.rglob(name) if p.is_file())
            if not matches:
                raise SystemExit(f"{name}: not found below {source}, nothing written")
            # A copy may hold several versions; take the one we test with.
            data = matches[0].read_bytes()
            for match in matches:
                candidate = match.read_bytes()
                if hashlib.sha256(candidate).hexdigest() == sha256:
                    data = candidate
                    break
        else:
            with urllib.request.urlopen(f"{UPSTREAM_RAW}/{upstream_path}", timeout=60) as r:
                data = r.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha256:
            raise SystemExit(f"{name}: SHA-256 mismatch ({digest}), nothing written")
        downloaded[name] = data
    for name, data in downloaded.items():
        targets[name].mkdir(parents=True, exist_ok=True)
        (targets[name] / name).write_bytes(data)
        print(f"{name}: ok -> {targets[name] / name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("target", nargs="?", type=Path, help="e.g. /config/ecotherm_wolf")
    group.add_argument(
        "--bundled", action="store_true",
        help="place the files inside the integration package (tests/CI)",
    )
    parser.add_argument(
        "--from", dest="source", type=Path,
        help="copy from a local directory instead of downloading",
    )
    args = parser.parse_args()
    targets = BUNDLED_TARGETS if args.bundled else dict.fromkeys(FILES, args.target)
    fetch(targets, args.source)


if __name__ == "__main__":
    sys.exit(main())
