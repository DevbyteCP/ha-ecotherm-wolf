# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Locate the Wolf resource files (XML definitions + client certificate).

The three Wolf XMLs and the ISM7 client certificate are not part of the
published integration. Users place them in ``<config>/ecotherm_wolf/`` --
a directory HACS never touches, so the files survive every update.

For existing installations that still carry the files inside the
integration folder, the bundled location is used as a fallback.

Pure module (no Home Assistant imports) so it can be unit-tested directly.
"""
from __future__ import annotations

from pathlib import Path

WOLF_XML_FILES: tuple[str, ...] = ("device.xml", "parameter.xml", "converter.xml")
CLIENT_CERT_FILE = "client.pfx"

_PACKAGE_DIR = Path(__file__).parent
BUNDLED_WOLF_DIR = _PACKAGE_DIR / "definitions" / "data" / "wolf"
BUNDLED_CLIENT_CERT = _PACKAGE_DIR / "ism7" / CLIENT_CERT_FILE
# Curated presets / write addresses are our own files and always ship with
# the integration, independent of where the Wolf XMLs live.
CURATED_DATA_DIR = _PACKAGE_DIR / "definitions" / "data"


class MissingResourcesError(Exception):
    """Raised when Wolf files are neither in the user nor the bundled dir."""

    def __init__(self, user_dir: Path, missing: list[str]) -> None:
        self.user_dir = user_dir
        self.missing = missing
        super().__init__(
            f"Wolf files missing in {user_dir}: {', '.join(missing)}"
        )


def _has_all(directory: Path, names: tuple[str, ...]) -> bool:
    return all((directory / name).is_file() for name in names)


def missing_files(
    user_dir: Path,
    *,
    bundled_wolf_dir: Path = BUNDLED_WOLF_DIR,
    bundled_cert: Path = BUNDLED_CLIENT_CERT,
) -> list[str]:
    """Names of the files the user still has to copy into ``user_dir``.

    Empty when everything is available (from ``user_dir`` or the bundled
    fallback). Blocking file I/O -- run in an executor.
    """
    missing: list[str] = []
    if not _has_all(user_dir, WOLF_XML_FILES) and not _has_all(
        bundled_wolf_dir, WOLF_XML_FILES,
    ):
        missing += [n for n in WOLF_XML_FILES if not (user_dir / n).is_file()]
    if not (user_dir / CLIENT_CERT_FILE).is_file() and not bundled_cert.is_file():
        missing.append(CLIENT_CERT_FILE)
    return missing


def resolve_wolf_dir(
    user_dir: Path,
    *,
    bundled_wolf_dir: Path = BUNDLED_WOLF_DIR,
    bundled_cert: Path = BUNDLED_CLIENT_CERT,
) -> Path:
    """Directory holding the three Wolf XMLs, preferring ``user_dir``.

    The XMLs are taken as one set (all from ``user_dir`` or all from the
    bundled directory) so a partial copy never mixes two Wolf versions.
    """
    if _has_all(user_dir, WOLF_XML_FILES):
        return user_dir
    if _has_all(bundled_wolf_dir, WOLF_XML_FILES):
        return bundled_wolf_dir
    raise MissingResourcesError(
        user_dir,
        missing_files(
            user_dir, bundled_wolf_dir=bundled_wolf_dir, bundled_cert=bundled_cert,
        ),
    )


def resolve_cert(
    user_dir: Path,
    *,
    bundled_wolf_dir: Path = BUNDLED_WOLF_DIR,
    bundled_cert: Path = BUNDLED_CLIENT_CERT,
) -> Path:
    """Path of the ISM7 client certificate, preferring ``user_dir``."""
    if (user_dir / CLIENT_CERT_FILE).is_file():
        return user_dir / CLIENT_CERT_FILE
    if bundled_cert.is_file():
        return bundled_cert
    raise MissingResourcesError(
        user_dir,
        missing_files(
            user_dir, bundled_wolf_dir=bundled_wolf_dir, bundled_cert=bundled_cert,
        ),
    )
