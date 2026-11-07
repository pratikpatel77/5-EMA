"""Reuse the pattern-scanners data layer instead of duplicating it.

That project already holds the working Fyers and Definedge logins, the shared
daily-bar cache (`.cache/bars.pkl`) and the market-cap universe list. This
module finds it and puts it on `sys.path`; nothing broker-specific is
reimplemented in this app.

Point `EMA5_SCANNERS_DIR` at the folder if it ever moves.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()

_CANDIDATES = (
    APP_ROOT.parent.parent / "API's" / "Scanners" / "pattern-scanners",
    APP_ROOT.parent / "pattern-scanners",
    HOME / "Documents" / "API's" / "Scanners" / "pattern-scanners",
)

_HELP = (
    "Could not find the pattern-scanners project, which supplies the Fyers and "
    "Definedge logins and the shared price cache.\n"
    "Set EMA5_SCANNERS_DIR to its folder, for example:\n"
    "    set EMA5_SCANNERS_DIR=C:\\Users\\You\\Documents\\API's\\Scanners\\pattern-scanners"
)

_root: Path | None = None


def _is_root(path: Path) -> bool:
    return (path / "datalayer" / "__init__.py").is_file()


def scanners_root() -> Path:
    """Folder holding the `datalayer` package."""
    global _root
    if _root is not None:
        return _root
    override = os.environ.get("EMA5_SCANNERS_DIR", "").strip()
    if override:
        path = Path(override).expanduser()
        if not _is_root(path):
            raise RuntimeError(f"EMA5_SCANNERS_DIR={path} has no datalayer folder.\n{_HELP}")
        _root = path
        return _root
    for path in _CANDIDATES:
        if _is_root(path):
            _root = path
            return _root
    raise RuntimeError(_HELP)


def ensure() -> Path:
    """Make `import datalayer` work. Safe to call repeatedly."""
    root = scanners_root()
    text = str(root)
    if text not in sys.path:
        sys.path.insert(0, text)
    return root


def datalayer():
    ensure()
    import datalayer

    return datalayer


def universe_file() -> Path | None:
    """Bundled market-cap-ranked symbol list, largest first."""
    path = scanners_root() / "universe" / "nse_by_market_cap.csv"
    return path if path.is_file() else None
