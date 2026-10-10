# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""W10 + W11 -- override XML loading and duplicate-PTID detection.

The user-override directory replaces device templates wholesale (no per-
parameter merging). A duplicate PTID inside a single override file must
surface a ``DefinitionLoadError`` so the Repair Issue is posted; partial
loads with overwritten parameters are too easy to miss.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.ecotherm_wolf.definitions.loader import (
    DefinitionLoadError,
    load_catalog_from_dir,
)

OVERRIDE_DOC = """\
<device type="OV-1" displayName="Override device">
  <parameter id="0x010C" name="vorlauftemp" displayName="Vorlauf">
    <codec type="int16" unit="°C">
      <min>0</min>
      <max>100</max>
    </codec>
  </parameter>
</device>
"""


def test_override_replaces_bundled_template(tmp_path: Path) -> None:
    """A single override file produces a single-parameter device template.

    Replace-not-merge semantics: only the parameters declared in the override
    are present. (The bundled CHA template with hundreds of parameters is
    NOT consulted at all once an override matches the device_type.)
    """
    (tmp_path / "ov1.xml").write_text(OVERRIDE_DOC, encoding="utf-8")
    catalog = load_catalog_from_dir(tmp_path)
    dev = catalog.get_device("OV-1")
    assert set(dev.parameters.keys()) == {0x010C}
    assert dev.parameters[0x010C].name == "vorlauftemp"


def test_duplicate_ptid_in_override_raises(tmp_path: Path) -> None:
    """Two <parameter id="0x010C" .../> in the same file -> hard error.

    Without this guard, a duplicate would silently overwrite the first
    parameter and the user would be confused why their override "didn't
    take". W10 spec: clear error message.
    """
    body = """\
<device type="OV-1" displayName="Override">
  <parameter id="0x010C" name="first" displayName="First">
    <codec type="int16"/>
  </parameter>
  <parameter id="0x010C" name="second" displayName="Second">
    <codec type="int16"/>
  </parameter>
</device>
"""
    (tmp_path / "ov1.xml").write_text(body, encoding="utf-8")
    with pytest.raises(DefinitionLoadError) as exc:
        load_catalog_from_dir(tmp_path)
    msg = str(exc.value).lower()
    assert "duplicate" in msg
    assert "0x010c" in msg or "0x010c".upper() in str(exc.value)
