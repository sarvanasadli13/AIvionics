"""AI Validation — the gold review as an ordinary page of the application.

This is a `Page` in the main window's stack, not a second application shell.
The questionnaire itself lives in `ui.goldpanel` and is shared with the
standalone `AdjudicatorWindow`, so there is one implementation of the rules
and one of the screens.

Authorisation is re-checked here on every entry rather than trusted from the
rail: hiding a navigation item is a courtesy, not a control.
"""
from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from ... import goldreview as G
from ..goldpanel import GoldReviewPanel
from .base import Page


class ValidationPage(Page):
    title = "AI Validation"
    permission = G.GOLD_REVIEW_PERMISSION

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.panel = GoldReviewPanel(
            ctx.con, getattr(ctx, "user", None), self.theme_name, self)
        lay.addWidget(self.panel)
        self._started = False

    # ── authority ─────────────────────────────────────────────────────
    def may_open(self) -> bool:
        """Asked by `MainWindow.navigate` before the page is ever shown."""
        con = getattr(self.ctx, "con", None)
        return G.may_review(con, getattr(self.ctx, "user", None))

    def can_leave(self) -> bool:
        """The page-leave contract. Delegated whole to the panel, which is
        the only thing that knows whether the form has unsaved edits."""
        return self.panel.can_leave()

    def on_shown(self) -> None:
        # The user can change between construction and first use (sign out and
        # back in), so the service is re-pointed rather than assumed.
        self.panel.service.user = getattr(self.ctx, "user", None)
        self.panel.start()
        self._started = True
