"""Shared pytest fixtures.

The HA plugin (``pytest_homeassistant_custom_component``) depends on ``fcntl``,
which doesn't exist on Windows. To keep the pure-Python unit tests
(protocol / codecs / wolf loader / mock-server) runnable on Windows we make
the HA plugin autouse fixture optional:

- If both ``homeassistant`` AND ``pytest_homeassistant_custom_component`` are
  importable, autouse ``enable_custom_integrations`` for every test.
- Otherwise install a no-op autouse fixture so the pure unit tests still run.
  Every test file that needs HA opens with ``pytest.importorskip("homeassistant")``
  / ``pytest.importorskip("pytest_homeassistant_custom_component")`` so it
  skips cleanly on platforms without the plugin.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).parent.parent / "custom_components" / "ecotherm_wolf"
WOLF_FILES_PRESENT = all(
    p.is_file()
    for p in (
        _PACKAGE / "definitions" / "data" / "wolf" / "device.xml",
        _PACKAGE / "definitions" / "data" / "wolf" / "parameter.xml",
        _PACKAGE / "definitions" / "data" / "wolf" / "converter.xml",
        _PACKAGE / "ism7" / "client.pfx",
    )
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_wolf_files: needs Wolf's XMLs + client certificate in the "
        "package (fetch with scripts/fetch_wolf_files.py --bundled)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item],
) -> None:
    if WOLF_FILES_PRESENT:
        return
    skip = pytest.mark.skip(
        reason="Wolf files missing -- run scripts/fetch_wolf_files.py --bundled",
    )
    for item in items:
        if "requires_wolf_files" in item.keywords:
            item.add_marker(skip)

HA_PLUGIN_AVAILABLE = (
    importlib.util.find_spec("homeassistant") is not None
    and importlib.util.find_spec("pytest_homeassistant_custom_component")
    is not None
)


if HA_PLUGIN_AVAILABLE:
    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Enable loading of custom integrations in all HA-aware tests."""
        yield
else:
    # No-op fixture so HA tests can still be collected (and skip via
    # ``pytest.importorskip``) even when the HA plugin isn't installed.
    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations():
        yield
