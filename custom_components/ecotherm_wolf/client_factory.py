# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Factory for the ISM7 client -- single seam for test-time substitution."""
from __future__ import annotations

from pathlib import Path

from .ism7.client import CLIENT_CERT_PATH, Ism7Client


def create_client(
    *,
    host: str,
    port: int,
    password: str,
    use_tls: bool = True,
    cert_path: Path = CLIENT_CERT_PATH,
) -> Ism7Client:
    return Ism7Client(
        host=host, port=port, password=password, use_tls=use_tls,
        cert_path=cert_path,
    )
