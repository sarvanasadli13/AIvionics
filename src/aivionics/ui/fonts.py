"""Resolve font families against what is actually installed.

Qt's QSS `font-family` does not fall back through a comma list the way CSS
does — an unavailable first choice can leave the whole declaration
unresolved and the app renders in whatever the platform default happens to
be. So the family is picked here, once, against the real font database and
injected into the stylesheet as a single name.
"""
from __future__ import annotations

from PySide6.QtGui import QFontDatabase

from . import theme as T

_cache: dict[str, str] = {}


def _first_available(candidates: list[str], fallback: str) -> str:
    families = set(QFontDatabase.families())
    for name in candidates:
        if name in families:
            return name
    return fallback


def ui_family() -> str:
    if "ui" not in _cache:
        _cache["ui"] = _first_available(T.UI_FAMILIES, "Arial")
    return _cache["ui"]


def mono_family() -> str:
    if "mono" not in _cache:
        _cache["mono"] = _first_available(T.MONO_FAMILIES, "Courier New")
    return _cache["mono"]


def qss(theme: str) -> str:
    """The stylesheet for `theme`, with resolved family names."""
    return T.build_qss(theme, ui_family=ui_family(), mono_family=mono_family())


def reset_cache() -> None:
    _cache.clear()
