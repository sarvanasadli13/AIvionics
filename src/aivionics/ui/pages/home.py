"""Home — status hero, world clock, triage feed (PLAN 4.8).

This is the one screen where air is allowed (§4A.1 rule 3). Every other view
is dense by design.

No numeral on this page is invented. Counts come from the corpus database
read-only; when it is absent the tiles say so rather than showing a zero
that could be mistaken for a measurement (standing rule 4).
"""
from __future__ import annotations

from datetime import datetime

import qtawesome as qta
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QToolButton, QVBoxLayout, QWidget)

from ... import config
from .. import store
from .. import theme as T
from ..widgets import (WORLD_CLOCK_ZONES, Card, EmptyState, Placard,
                       ProvenanceLine, SectionHeader, StatusBadge, WorldClock,
                       mono_font, ui_font)
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

    def update_values(self, value: str, note: str,
                      status: str | None = None) -> None:
        """Set the figure, its caption and — importantly — its badge.

        The badge used to be fixed at construction, when every tile is empty.
        A tile that had since been given 8,194 still read NO DATA beside it.
        When no status is passed it is inferred from the figure, so a caller
        cannot forget: a real value is `ok`, an em-dash is `unknown`.
        """
        self.value.setText(value)
        self.note.setText(note)
        if status is None:
            status = "unknown" if value.strip() in ("—", "", "-") else "ok"
        if status != self.badge.kind:
            self.badge.kind = status
            self.badge.refresh_theme(getattr(self, "_theme", T.DEFAULT_THEME))
        # An em-dash set at hero weight reads as a redaction bar, not as
        # "no figure". Drop it to the faint tone so absence looks like absence.
        pal = T.THEMES[getattr(self, "_theme", T.DEFAULT_THEME)]
        self.value.setStyleSheet(
            f"color:{pal['txt3'] if value.strip() in ('—', '', '-') else pal['txt']};")

    def refresh_theme(self, theme: str) -> None:
        self._theme = theme
        self.update_values(self.value.text(), self.note.text())


class FirstRunNotice(QFrame):
    """What Home leads with when there is no corpus behind it.

    An install that opens on three em-dashes and a set of NO DATA badges is
    reporting its state accurately and telling the operator nothing they can
    act on. This says which database was opened, that it is empty, and the
    two ways to change that (BACKLOG item 1). It never appears once a corpus
    is present.
    """

    ROUTES = [
        ("mdi6.folder-open-outline",
         "Point it at a database you already have",
         "Set AIVIONICS_DATA to the folder holding aivionics.db, or start "
         "the application with  --db <path>."),
        ("mdi6.cog-play-outline",
         "Or build one from the source documents",
         "Run scripts/phase1.py to ingest, then scripts/phase2_index.py to "
         "build the retrieval index."),
    ]

    def __init__(self, db_path, theme: str = T.DEFAULT_THEME, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self._icons: list[tuple[QLabel, str]] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 15, 18, 16)
        lay.setSpacing(9)

        head = QHBoxLayout()
        head.setSpacing(9)
        self.head_icon = QLabel()
        self.head_icon.setFixedSize(19, 19)
        head.addWidget(self.head_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        title = QLabel("This install has no data in it yet")
        title.setFont(ui_font(12, QFont.Weight.DemiBold))
        head.addWidget(title)
        head.addStretch(1)
        lay.addLayout(head)

        self.where = QLabel(str(db_path))
        self.where.setFont(mono_font(8.5, QFont.Weight.Normal))
        self.where.setObjectName("Muted")
        self.where.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.where)

        lay.addWidget(caption(
            "Nothing below is broken — the screens are empty because that "
            "file is. Two ways forward:", "Muted", 9))

        for glyph, headline, detail in self.ROUTES:
            row = QHBoxLayout()
            row.setSpacing(10)
            icon = QLabel()
            icon.setFixedSize(16, 16)
            self._icons.append((icon, glyph))
            row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
            text = QVBoxLayout()
            text.setSpacing(1)
            line = QLabel(headline)
            line.setFont(ui_font(9.5, QFont.Weight.DemiBold))
            text.addWidget(line)
            body = caption(detail, "Muted", 9)
            body.setWordWrap(True)
            text.addWidget(body)
            row.addLayout(text, 1)
            lay.addLayout(row)

        self.refresh_theme(theme)

    def set_path(self, db_path) -> None:
        self.where.setText(str(db_path))

    def refresh_theme(self, theme: str) -> None:
        pal = T.THEMES[theme]
        self.head_icon.setPixmap(
            qta.icon("mdi6.information-outline", color=pal["cy"]).pixmap(19, 19))
        for label, glyph in self._icons:
            label.setPixmap(qta.icon(glyph, color=pal["txt3"]).pixmap(16, 16))
        self.where.setStyleSheet(f"color:{pal['txt3']};")


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

        # ── first run ─────────────────────────────────────────────────
        # Above the tiles, not below them: with no corpus the tiles have
        # nothing to say, and the thing to do about that is the news.
        self.first_run = FirstRunNotice(
            getattr(ctx, "db_path", config.DB_PATH), self.theme_name)
        self.first_run.hide()
        body.addWidget(self.first_run)

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

        # Which cities appear was a guess made once, in a file, by somebody
        # who is not the person reading the clock. It is a list now (R7).
        header = QHBoxLayout()
        header.setContentsMargins(13, 2, 13, 2)
        header.addWidget(Placard("World time"))
        header.addStretch(1)
        self.clock_edit = QToolButton()
        self.clock_edit.setObjectName("LinkBtn")
        self.clock_edit.setText("Edit cities")
        self.clock_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clock_edit.setFont(ui_font(8.5, QFont.Weight.DemiBold))
        self.clock_edit.setToolTip("Add, remove or reorder the cities on this strip")
        self.clock_edit.clicked.connect(self.edit_clock_zones)
        header.addWidget(self.clock_edit)
        cc.addLayout(header)

        self.clock = WorldClock(
            zones=store.world_clock_zones(getattr(ctx, "con", None)) or None,
            theme=self.theme_name)
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

    def edit_clock_zones(self) -> None:
        """Open the editor, and persist whatever comes back."""
        from ..zoneeditor import ZoneEditor
        con = getattr(self.ctx, "con", None)
        current = store.world_clock_zones(con) or list(WORLD_CLOCK_ZONES)
        dialog = ZoneEditor(current, self.theme_name, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.zones()
        if not chosen:
            return
        self.clock.set_zones(chosen)
        if con is not None:
            try:
                store.set_world_clock_zones(con, chosen)
            except Exception:
                pass        # a preference that will not save is not an outage

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
        empty = not any(counts.get(k) for k in ("tasks", "aircraft", "cases"))
        if empty:
            self.first_run.set_path(getattr(self.ctx, "db_path", config.DB_PATH))
        self.first_run.setVisible(empty)
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
        # Deliberately still `unknown`: there is a case base but no repeat
        # detector on this screen yet, and an OK badge over an em-dash would
        # claim a measurement that has not been made.
        self.tile_repeat.update_values(
            "—",
            f"{cases:,} cases indexed; repeat-defect detection arrives in Phase 3."
            if cases else "Needs a case base — Phase 3.",
            status="unknown")
