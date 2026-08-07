"""Diagnose — symptom in, ranked task *locators* and prior cases out (PLAN 4.4).

The retrieval engine is Phase 2 and is not wired in here yet; this is the
shell it will populate. The structure is fixed now because two things about
it are non-negotiable and easier to get right before there is data:

  * the prior-cases table separates `replaced:` from `found:`. That split is
    the product — everyone retrieves procedures, nobody surfaces what was
    actually found (PLAN §11.4);
  * the decision-support footer is present on the screen at all times
    (PLAN §0.2), not in an About box.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton,
                               QTableWidget, QVBoxLayout, QWidget)

from .. import theme as T
from ..widgets import (Card, EmptyState, ProvenanceLine, SectionHeader,
                       Splitter, Tag, ui_font)
from .base import Page, caption

CASE_COLUMNS = ["Tail", "Date", "replaced:", "found:"]


class DiagnosePage(Page):
    title = "Diagnose"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._query_band())

        split = Splitter(Qt.Orientation.Horizontal, self.theme_name)
        split.addWidget(self._locators_panel())
        split.addWidget(self._cases_panel())
        split.setSizes([560, 440])
        outer.addWidget(split, 1)

        outer.addWidget(self._footer())

    # ── query ─────────────────────────────────────────────────────────
    def _query_band(self) -> QWidget:
        band = QFrame()
        band.setObjectName("Band")
        lay = QVBoxLayout(band)
        lay.setContentsMargins(15, 11, 15, 10)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(11)
        self.query = QLineEdit()
        self.query.setPlaceholderText(
            "Describe the symptom — e.g. CAPT AIRSPEED UNRELIABLE ON T/O ROLL, "
            "BITE CHK SHOWS NO CURRENT FAULTS")
        self.query.setFont(ui_font(10))
        self.query.setMinimumHeight(38)
        self.query.setClearButtonEnabled(True)
        self.query.setAccessibleName("Symptom")
        self.query.returnPressed.connect(self.run_search)
        top.addWidget(self.query, 1)

        self.search_btn = QPushButton("Search")
        self.search_btn.setObjectName("Primary")
        self.search_btn.setMinimumHeight(38)
        self.search_btn.clicked.connect(self.run_search)
        top.addWidget(self.search_btn)
        lay.addLayout(top)

        chips = QHBoxLayout()
        chips.setSpacing(7)
        for text in ("Free text · no fault code", "All ATA chapters",
                     "Effectivity — no tail selected", "Cases 1995–2026"):
            chips.addWidget(Tag(text, self.theme_name))
        chips.addStretch(1)
        self.query_status = caption("retrieval engine not connected", "Faint", 8.5)
        chips.addWidget(self.query_status)
        lay.addLayout(chips)
        return band

    # ── left column: task locators ────────────────────────────────────
    def _locators_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.locator_header = SectionHeader("Task locators", "ranked · locator only")
        lay.addWidget(self.locator_header)
        self.locator_empty = EmptyState(
            "mdi6.magnify",
            "No search run yet",
            "Results appear here as ranked task locators — task number, title, "
            "manual and revision. The procedure itself is never reproduced; the "
            "engineer is sent to the controlled manual.",
            theme=self.theme_name)
        lay.addWidget(self.locator_empty, 1)
        return panel

    # ── right column: prior cases ─────────────────────────────────────
    def _cases_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("Card")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.cases_header = SectionHeader("Prior cases", "this tail first")
        lay.addWidget(self.cases_header)

        self.cases = QTableWidget(0, len(CASE_COLUMNS))
        self.cases.setHorizontalHeaderLabels(CASE_COLUMNS)
        self.cases.verticalHeader().setVisible(False)
        self.cases.verticalHeader().setDefaultSectionSize(T.ROW_HEIGHT)
        self.cases.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cases.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cases.setShowGrid(False)
        self.cases.setAlternatingRowColors(True)
        header = self.cases.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.cases, 1)

        self.cases_note = ProvenanceLine(
            "SDR-mined proxy · a reportable-occurrence sample, not a measured "
            "rate. Cases with an incomplete closure and no recorded finding are "
            "excluded from every statistic.")
        self.cases_note.setContentsMargins(11, 8, 11, 9)
        lay.addWidget(self.cases_note)
        return panel

    # ── footer ────────────────────────────────────────────────────────
    def _footer(self) -> QWidget:
        foot = QFrame()
        foot.setObjectName("PageHeader")
        foot.setFixedHeight(30)
        lay = QHBoxLayout(foot)
        lay.setContentsMargins(15, 0, 15, 0)
        self.footer_label = QLabel(
            "Decision support — not part of the official maintenance record.")
        self.footer_label.setObjectName("Muted")
        self.footer_label.setFont(ui_font(8.5, QFont.Weight.DemiBold))
        lay.addWidget(self.footer_label)
        lay.addStretch(1)
        return foot

    # ── behaviour ─────────────────────────────────────────────────────
    def run_search(self) -> None:
        """Placeholder until Phase 2 lands.

        It reports honestly rather than pretending to search, because a
        fake result set in a safety tool is worse than no result set.
        """
        text = self.query.text().strip()
        if not text:
            self.query_status.setText("enter a symptom to search")
            return
        self.query_status.setText(
            "retrieval engine not connected — Gate 2 must pass before this "
            "returns results")
