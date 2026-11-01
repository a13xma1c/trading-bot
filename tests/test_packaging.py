"""Packaging guards: the web UI assets must be part of the installed package (non-editable installs, containers)."""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_static_assets_are_declared_as_package_data() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = pyproject["tool"]["setuptools"]["package-data"]["trading_bot.web"]
    static_root = ROOT / "trading_bot" / "web"
    assets = [p.relative_to(static_root).as_posix() for p in (static_root / "static").rglob("*") if p.is_file()]
    assert assets, "no static assets found"
    for asset in assets:
        assert any(fnmatch.fnmatch(asset, pattern) for pattern in patterns), \
            f"{asset} is not covered by [tool.setuptools.package-data]; it would be missing from the wheel"


def test_version_is_consistent() -> None:
    from trading_bot import __version__

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__
