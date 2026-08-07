"""Shared page scaffolding."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QScrollArea, QVBoxLayout,
                               QWidget)

from .. import theme as T
from ..widgets import Placard, ProvenanceLine, SectionHeader, ui_font


class Page(QWidget):
    """Base page: owns a theme and propagates it to any themed child.

    `refresh_theme` walks the widget tree once rather than each page keeping
    its own registry, so a new themed widget is picked up automatically.
    """

    title = ""

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctx = ctx
        self.theme_name = ctx.theme_name if ctx else T.DEFAULT_THEME

    def refresh_theme(self, theme: str) -> None:
        self.theme_name = theme
        for child in self.findChildren(QWidget):
            fn = getattr(child, "refresh_theme", None)
            if callable(fn) and child is not self:
                fn(theme)

    def on_shown(self) -> None:
        """Called each time the page becomes visible. Override to refresh data."""


def scroll_host(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(inner)
    return area


def column(*widgets, spacing: int = 0, margins=(0, 0, 0, 0)) -> QWidget:
    host = QWidget()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        else:
            lay.addWidget(w)
    return host


def row(*widgets, spacing: int = 10, margins=(0, 0, 0, 0)) -> QWidget:
    host = QWidget()
    lay = QHBoxLayout(host)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        else:
            lay.addWidget(w)
    return host


def caption(text: str, obj: str = "Muted", size: int = 9) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(obj)
    lbl.setFont(ui_font(size))
    lbl.setWordWrap(True)
    return lbl


def heading(text: str, size: int = 12) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(ui_font(size, QFont.Weight.DemiBold))
    return lbl


__all__ = ["Page", "scroll_host", "column", "row", "caption", "heading",
           "Placard", "ProvenanceLine", "SectionHeader"]
