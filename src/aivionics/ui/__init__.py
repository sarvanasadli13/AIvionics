"""AIvionics desktop shell (PLAN Phase 4).

Run with ``python -m aivionics.ui``.

The Qt-dependent entry points are resolved lazily so that the pure modules
(`theme`, `auth`, `printing`, `store`, `adjudicator`) can be imported — and
tested — without pulling in the widget layer.
"""
from __future__ import annotations

__all__ = ["AppContext", "MainWindow", "build_context", "main"]


def __getattr__(name: str):
    if name in __all__:
        from . import app
        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
