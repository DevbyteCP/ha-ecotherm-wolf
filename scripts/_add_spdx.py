# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
"""One-shot helper -- prepend SPDX headers to Python files.

Used during the GPL-3.0 compliance pass (W7). Not exercised at runtime.
"""
from __future__ import annotations

from pathlib import Path

HEADER_PURE = (
    "# SPDX-License-Identifier: GPL-3.0-or-later\n"
    "# Copyright (C) 2026 DevbyteCP\n"
)
HEADER_DERIVED = HEADER_PURE + (
    "# Derived in part from zivillian/ism7mqtt (GPL-3.0-or-later)\n"
)

# Files derived in part from zivillian/ism7mqtt (protocol layer + Wolf XML).
DERIVED: set[str] = {
    "custom_components/ecotherm_wolf/ism7/__init__.py",
    "custom_components/ecotherm_wolf/ism7/client.py",
    "custom_components/ecotherm_wolf/ism7/exceptions.py",
    "custom_components/ecotherm_wolf/ism7/operations.py",
    "custom_components/ecotherm_wolf/ism7/protocol.py",
    "custom_components/ecotherm_wolf/definitions/__init__.py",
    "custom_components/ecotherm_wolf/definitions/catalog.py",
    "custom_components/ecotherm_wolf/definitions/codecs.py",
    "custom_components/ecotherm_wolf/definitions/converters.py",
    "custom_components/ecotherm_wolf/definitions/loader.py",
    "custom_components/ecotherm_wolf/definitions/wolf_loader.py",
}


def already_has_header(text: str) -> bool:
    lines = text.splitlines()
    if not lines:
        return False
    return "SPDX-License-Identifier" in lines[0]


def add_header(path: Path, repo_root: Path) -> bool:
    rel = path.relative_to(repo_root).as_posix()
    text = path.read_text(encoding="utf-8")
    if already_has_header(text):
        return False
    header = HEADER_DERIVED if rel in DERIVED else HEADER_PURE
    path.write_text(header + text, encoding="utf-8")
    return True


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    component = repo_root / "custom_components" / "ecotherm_wolf"
    scripts = repo_root / "scripts"
    changed: list[str] = []
    for base in (component, scripts):
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            if add_header(p, repo_root):
                changed.append(str(p.relative_to(repo_root).as_posix()))
    print(f"Added headers to {len(changed)} file(s):")
    for c in changed:
        print(f"  {c}")


if __name__ == "__main__":
    main()
