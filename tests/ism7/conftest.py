"""Fixtures for the protocol-layer tests.

The client / mock-server tests speak REAL localhost TCP (that's the point
-- they exercise framing, login, reconnect against a live socket pair).
The pytest-homeassistant plugin blocks all sockets by default, so on
HA-capable environments (WSL/CI) every one of these tests would die with
``SocketBlockedError``. Re-enable sockets for this directory only via the
plugin's ``socket_enabled`` fixture; on bare Windows the plugin isn't
installed, sockets aren't blocked, and a no-op stands in.
"""
from __future__ import annotations

import importlib.util

import pytest

if importlib.util.find_spec("pytest_homeassistant_custom_component") is not None:
    @pytest.fixture(autouse=True)
    def _allow_local_sockets(socket_enabled: None) -> None:
        """Lift the HA plugin's socket block for mock-server tests."""
        return
else:
    @pytest.fixture(autouse=True)
    def _allow_local_sockets() -> None:
        """No HA plugin -> sockets aren't blocked; nothing to lift."""
        return
