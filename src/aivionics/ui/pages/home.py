"""Home — status hero, world clock, triage feed (PLAN 4.8).

This is the one screen where air is allowed (§4A.1 rule 3). Every other view
is dense by design.

No numeral on this page is invented. Counts come from the corpus database
read-only; when it is absent the tiles say so rather than showing a zero
that could be mistaken for a measurement (standing rule 4).
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QVBoxLayout, QWidget)

from .. import theme as T
from ..widgets import (Card, EmptyState, Placard, ProvenanceLine,
                       SectionHeader, StatusBadge, WorldClock, ui_font)
from .base import Page, caption, scroll_host


class StatTile(QFrame):
    """A hero figure: placard, value, status badge, one line of context.

    The badge is mandatory — a bare number with a colour behind it would
    breach §4A.1 rule 2.
    """

    def __init__(self, label: str, value: str, status: str, note: str,
                 theme: str = T.DEFAULT_THEME, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(17, 14, 17, 15)
        lay.setSpacing(7)

        lay.addWidget(Placard(label))

        value_row = QHBoxLayout()
        value_row.setSpacing(11)
        self.value = QLabel(value)
        self.value.setObjectName("HeroValue")
        self.value.setFont(ui_font(21, QFont.Weight.DemiBold, tabular=True))
        value_row.addWidget(self.value)
        value_row.addStretch(1)
        self.badge = StatusBadge(status, theme=theme)
        value_row.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(value_row)

        self.note = caption(note, "Muted", 8.5)
        self.note.setMinimumHeight(32)
        lay.addWidget(self.note)
        self._theme = theme
        self.update_values(value, note)

    def update_values(self, value: str, note: str) -> None:
        self.value.setText(value)
        self.note.setText(note)
        # An em-dash set at hero weight reads as a redaction bar, not as
        # "no figure". Drop it to the faint tone so absence looks like absence.
        pal = T.THEMES[getattr(self, "_theme", T.DEFAULT_THEME)]
        self.value.setStyleSheet(
            f"color:{pal['txt3'] if value.strip() in ('—', '', '-') else pal['txt']};")

    def refresh_theme(self, theme: str) -> None:
        self._theme = theme
        self.update_values(self.value.text(), self.note.text())


class HomePage(Page):
    title = "Home"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QVBoxLayout()
        body.setContentsMargins(22, 20, 22, 22)
        body.setSpacing(16)

        # ── greeting ──────────────────────────────────────────────────
        self.greeting = QLabel("")
        self.greeting.setFont(ui_font(17, QFont.Weight.DemiBold))
        self.subgreeting = caption("", "Muted", 9.5)
        body.addWidget(self.greeting)
        body.addWidget(self.subgreeting)

        # ── hero tiles ────────────────────────────────────────────────
        grid = QGridLayout()
        grid.setSpacing(14)
        self.tile_fleet = StatTile(
            "Fleet", "—", "unknown",
            "No aircraft registered yet.", self.theme_name)
        self.tile_repeat = StatTile(
            "Repeat-defect alerts", "—", "unknown",
            "Needs a case base — Phase 3.", self.theme_name)
        self.tile_corpus = StatTile(
            "Manual corpus", "—", "unknown",
            "Run the Phase 1 ingest to populate.", self.theme_name)
        for i, tile in enumerate((self.tile_fleet, self.tile_repeat, self.tile_corpus)):
            grid.addWidget(tile, 0, i)
        body.addLayout(grid)

        # ── world clock ───────────────────────────────────────────────
        clock_card = Card()
        cc = QVBoxLayout(clock_card)
        cc.setContentsMargins(4, 6, 4, 6)
        cc.setSpacing(0)
        self.clock = WorldClock(theme=self.theme_name)
        cc.addWidget(self.clock)
        body.addWidget(clock_card)

        # ── triage feed ───────────────────────────────────────────────
        feed = Card()
        fl = QVBoxLayout(feed)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)
        header = SectionHeader("Triage feed", "red = limit breached · ordered by time-to-limit")
        fl.addWidget(header)
        self.feed_provenance = ProvenanceLine(
            "Source system: none imported · no compliance data has been loaded — "
            "verify every clock against the CAMO.")
        self.feed_provenance.setContentsMargins(15, 9, 15, 4)
        fl.addWidget(self.feed_provenance)
        self.feed_empty = EmptyState(
            "mdi6.clipboard-text-clock-outline",
            "No compliance data imported",
            "Checkups, MEL deferrals and AD/SB rows arrive from a CAMO export "
            "(Phase 4B.2). Until then this feed stays empty rather than showing "
            "a clock this application cannot vouch for.",
            theme=self.theme_name)
        fl.addWidget(self.feed_empty, 1)
        body.addWidget(feed, 1)

        host = QWidget()
        host.setLayout(body)
        outer.addWidget(scroll_host(host))
        self.on_shown()

    def on_shown(self) -> None:
        user = getattr(self.ctx, "user", None)
        hour = datetime.now().hour
        part = "Good morning" if hour < 12 else (
            "Good afternoon" if hour < 18 else "Good evening")
        name = getattr(user, "display_name", None) or "engineer"
        role = getattr(user, "role", "")
        self.greeting.setText(f"{part}, {name}.")
        self.subgreeting.setText(
            f"Signed in as {role} · {datetime.now().strftime('%A %d %B %Y')}")

        counts = self.ctx.corpus.counts() if self.ctx else {}
        fleet = counts.get("aircraft", 0)
        tasks = counts.get("tasks", 0)
        self.tile_fleet.update_values(
            str(fleet) if fleet else "—",
            "Aircraft on the register." if fleet else "No aircraft registered yet.")
        if tasks:
            self.tile_corpus.update_values(
                f"{tasks:,}",
                f"{counts.get('amm', 0):,} AMM tasks · "
                f"{counts.get('fim', 0):,} FIM catalogue locators.")
        else:
            self.tile_corpus.update_values(
                "—", "Corpus database not built yet — run the Phase 1 ingest.")
        cases = counts.get("cases", 0)
        self.tile_repeat.update_values(
            "—",
            f"{cases:,} cases indexed; repeat-defect detection arrives in Phase 3."
            if cases else "Needs a case base — Phase 3.")
