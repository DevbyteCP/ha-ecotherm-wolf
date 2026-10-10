# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
# Derived in part from zivillian/ism7mqtt (GPL-3.0-or-later)
"""ISM7 protocol exceptions."""
from __future__ import annotations


class Ism7Error(Exception):
    """Base for all ISM7 errors."""


class ProtocolError(Ism7Error):
    """Malformed or unexpected frame."""


class AuthenticationError(Ism7Error):
    """Login refused."""


class ConnectionLost(Ism7Error):
    """TCP layer disconnected."""


class WriteRejected(Ism7Error):
    """ISM7 acknowledged but didn't apply the write."""
