"""Standalone window around the gold-review questionnaire.

This is a *host*, not an implementation. It supplies window chrome — frameless
frame, title bar, size grip — and embeds `GoldReviewPanel`, which is the same
widget the `AI Validation` page uses inside the main window. There is one
questionnaire UI and one persistence path; this file adds a window around
them and nothing else.

Kept for development and recovery. The supported workflow is the in-app page.
"""
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QSizeGrip, QVBoxLayout, QWidget)

from . import nativewindow
from . import theme as T
from .goldpanel import GoldReviewPanel
from .widgets import ShellBackground, TitleBar

# Re-exported so existing importers keep working; the questionnaire's own
# vocabulary now lives in the domain module.
from ..goldreview import VERDICT_KEYS, VERDICT_LABELS      # noqa: F401


class AdjudicatorWindow(QWidget):
    """A window whose entire content is `GoldReviewPanel`."""

    def __init__(self, con: sqlite3.Connection, theme: str = T.DEFAULT_THEME,
                 user=None):
        super().__init__()
        self.theme_name = theme

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowTitle("AIvionics — AI Validation")
        self.resize(1340, 880)
        self.setMinimumSize(1100, 700)
        nativewindow.restore_native_frame(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.frame = ShellBackground(theme, self)
        root.addWidget(self.frame)
        lay = QVBoxLayout(self.frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.titlebar = TitleBar(theme)
        self.titlebar.set_context("AI Validation  ·  held-out gold set")
        self.titlebar.set_badge("adjudicating")
        self.titlebar.minimise_requested.connect(self.showMinimized)
        self.titlebar.maximise_requested.connect(
            lambda: self.showNormal() if self.isMaximized() else self.showMaximized())
        self.titlebar.close_requested.connect(self.close)
        self.titlebar.theme_changed.connect(self.set_theme)
        lay.addWidget(self.titlebar)

        # The one questionnaire implementation, embedded.
        self.panel = GoldReviewPanel(con, user, theme, self)
        lay.addWidget(self.panel, 1)

        grip_row = QWidget()
        gl = QHBoxLayout(grip_row)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.addStretch(1)
        gl.addWidget(QSizeGrip(grip_row))
        grip_row.setFixedHeight(14)
        lay.addWidget(grip_row)

        self.set_theme(theme)
        self.panel.start()

    def closeEvent(self, event):
        """Honour the same unsaved-work contract the main window uses."""
        if not self.panel.can_leave():
            event.ignore()
            return
        super().closeEvent(event)

    def set_theme(self, theme: str) -> None:
        self.theme_name = theme
        from . import fonts
        self.setStyleSheet(fonts.qss(theme))
        self.frame.refresh_theme(theme)
        self.titlebar.refresh_theme(theme)
        self.panel.refresh_theme(theme)

    # `nativewindow` reasserts the frame styles Qt drops on some flag changes
    def showEvent(self, event):
        super().showEvent(event)
        nativewindow.restore_native_frame(self)

    def nativeEvent(self, event_type, message):
        handled, result = nativewindow.handle_native_event(self, event_type, message)
        if handled:
            return True, result
        return super().nativeEvent(event_type, message)
