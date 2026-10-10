# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""Runtime data attached to a config entry."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .definitions.catalog import Catalog
from .ism7.client import Ism7Client

if TYPE_CHECKING:
    from .coordinator import EcothermWolfCoordinator


@dataclass
class EcothermWolfRuntime:
    client: Ism7Client
    catalog: Catalog
    coordinator: EcothermWolfCoordinator | None
