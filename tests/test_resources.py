"""Lookup of the Wolf XMLs + client certificate (``resources.py``)."""
from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.ecotherm_wolf.resources import (
    CLIENT_CERT_FILE,
    WOLF_XML_FILES,
    MissingResourcesError,
    missing_files,
    resolve_cert,
    resolve_wolf_dir,
)


def _fill(directory: Path, names: tuple[str, ...] | list[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text("x")
    return directory


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    user = tmp_path / "config" / "ecotherm_wolf"
    bundled = tmp_path / "bundled" / "wolf"
    cert = tmp_path / "bundled" / CLIENT_CERT_FILE
    return user, bundled, cert


def test_user_dir_wins_over_bundled(dirs: tuple[Path, Path, Path]) -> None:
    user, bundled, cert = dirs
    _fill(user, [*WOLF_XML_FILES, CLIENT_CERT_FILE])
    _fill(bundled, WOLF_XML_FILES)
    cert.write_text("x")
    kw = {"bundled_wolf_dir": bundled, "bundled_cert": cert}
    assert resolve_wolf_dir(user, **kw) == user
    assert resolve_cert(user, **kw) == user / CLIENT_CERT_FILE
    assert missing_files(user, **kw) == []


def test_falls_back_to_bundled(dirs: tuple[Path, Path, Path]) -> None:
    user, bundled, cert = dirs
    _fill(bundled, WOLF_XML_FILES)
    cert.write_text("x")
    kw = {"bundled_wolf_dir": bundled, "bundled_cert": cert}
    assert resolve_wolf_dir(user, **kw) == bundled
    assert resolve_cert(user, **kw) == cert
    assert missing_files(user, **kw) == []


def test_partial_user_copy_does_not_mix_with_bundled(
    dirs: tuple[Path, Path, Path],
) -> None:
    """Only a complete XML set in the user dir counts."""
    user, bundled, cert = dirs
    _fill(user, ["device.xml"])
    _fill(bundled, WOLF_XML_FILES)
    assert resolve_wolf_dir(
        user, bundled_wolf_dir=bundled, bundled_cert=cert,
    ) == bundled


def test_missing_everything_lists_all_files(dirs: tuple[Path, Path, Path]) -> None:
    user, bundled, cert = dirs
    kw = {"bundled_wolf_dir": bundled, "bundled_cert": cert}
    expected = [*WOLF_XML_FILES, CLIENT_CERT_FILE]
    assert missing_files(user, **kw) == expected
    with pytest.raises(MissingResourcesError) as exc:
        resolve_wolf_dir(user, **kw)
    assert exc.value.user_dir == user
    assert exc.value.missing == expected
    with pytest.raises(MissingResourcesError):
        resolve_cert(user, **kw)


def test_missing_lists_only_what_the_user_still_lacks(
    dirs: tuple[Path, Path, Path],
) -> None:
    user, bundled, cert = dirs
    _fill(user, ["device.xml", CLIENT_CERT_FILE])
    assert missing_files(user, bundled_wolf_dir=bundled, bundled_cert=cert) == [
        "parameter.xml", "converter.xml",
    ]
