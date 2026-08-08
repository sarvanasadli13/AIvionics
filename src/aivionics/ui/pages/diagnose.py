"""Diagnose — symptom in, ranked task *locators* and prior cases out (PLAN 4.4).

Wired to the Phase 2 engine through `searchservice`. Three properties of this
screen are non-negotiable and shaped the layout before there was any data:

  * the prior-cases table separates `replaced:` from `found:`. That split is
    the product — everyone retrieves procedures, nobody surfaces what was
    actually found (PLAN §11.4). On SDR-mined cases `found:` is honestly
    reported as *not recorded*, because the shop teardown finding is not in
    this data at all;
  * results are **locators**. Task number, title, manual, revision — never the
    procedure (standing rule 1). The body is reachable in the read-only viewer
    on the Manuals screen, which is inside the app;
  * the decision-support footer is on the screen at all times (PLAN §0.2),
    not in an About box.

Every figure rendered here comes from the database or from the retrieval
engine's own arithmetic; nothing is generated (standing rule 4).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton,
                               QScrollArea, QSizePolicy, QStackedWidget,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from .. import theme as T
from ..searchservice import SearchService, SearchSignals, index_status
from ..widgets import (AtaLocator, EmptyState, ProvenanceLine, SectionHeader,
                       Splitter, Tag, ui_font)
from .base import Page, caption

CASE_COLUMNS = ["Tail", "Date", "replaced:", "found:"]

CHANNEL_TEXT = {
    "both": "keyword + semantic",
    "fts": "keyword match",
    "dense": "semantic match",
}

# Standing rule 8 is "fail closed on effectivity": anything not positively
# resolved must say so in words an engineer can act on. The engine's tokens are
# machine values and must never reach the screen as they are.
APPLICABILITY_TEXT = {
    "applicable": "effectivity: applicable",
    "not_applicable": "effectivity: NOT applicable to this tail",
    "unresolved": "applicability unresolved — verify in controlled data",
    "not_checked": "effectivity not checked — no tail selected",
}


class LocatorRow(QFrame):
    """One ranked task locator. Selectable, never expandable into a body."""

    clicked = Signal(object)

    def __init__(self, rank: int, result, theme: str, parent=None):
        super().__init__(parent)
        self.result = result
        self.theme_name = theme
        self.selected = False
        self.setObjectName("LocatorRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(13, 8, 13, 9)
        lay.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(9)
        self.rank = QLabel(f"{rank}")
        self.rank.setObjectName("Faint")
        self.rank.setFont(ui_font(9, QFont.Weight.DemiBold))
        self.rank.setFixedWidth(16)
        top.addWidget(self.rank)
        self.locator = AtaLocator(result.task_number or "—", theme)
        top.addWidget(self.locator)
        top.addStretch(1)
        prov = result.provenance or {}
        self.channel = QLabel(CHANNEL_TEXT.get(prov.get("channels", ""), ""))
        self.channel.setObjectName("Faint")
        self.channel.setFont(ui_font(8))
        top.addWidget(self.channel)
        lay.addLayout(top)

        self.title = QLabel(result.title or "—")
        self.title.setFont(ui_font(9.5))
        self.title.setWordWrap(True)
        lay.addWidget(self.title)

        tags = QHBoxLayout()
        tags.setSpacing(6)
        revision = prov.get("manual_revision")
        label = result.manual_type or "—"
        if revision:
            label = f"{label} {revision}"
        tags.addWidget(Tag(label, theme))
        if prov.get("catalogue_only"):
            tags.addWidget(Tag("locator only — no body held", theme))
        applic = prov.get("applicability")
        if applic and applic != "applicable":
            tags.addWidget(Tag(APPLICABILITY_TEXT.get(applic, str(applic)), theme))
        if prov.get("jasc_boost"):
            tags.addWidget(Tag("ATA hint boosted", theme))
        tags.addStretch(1)
        lay.addLayout(tags)

        self.refresh_theme(theme)

    def set_selected(self, on: bool) -> None:
        self.selected = on
        self.refresh_theme(self.theme_name)

    def refresh_theme(self, theme: str) -> None:
        self.theme_name = theme
        pal = T.THEMES[theme]
        bg = pal["cyq"] if self.selected else pal["s1"]
        edge = pal["cy"] if self.selected else pal["hair"]
        self.setStyleSheet(
            f"#LocatorRow{{background:{bg};border:1px solid {edge};"
            f"border-radius:5px;}}")

    def mousePressEvent(self, event):
        self.clicked.emit(self)
        super().mousePressEvent(event)


class DiagnosePage(Page):
    title = "Diagnose"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.service = SearchService(getattr(ctx, "db_path", None))
        self.signals = SearchSignals()
        self.signals.done.connect(self._on_results)
        self.signals.failed.connect(self._on_failed)
        self.rows: list[LocatorRow] = []
        self.selected: LocatorRow | None = None

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
        self._refresh_status()

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
        self.chips: list[Tag] = []
        for text in ("Free text · no fault code", "All ATA chapters",
                     "Effectivity — no tail selected", "Cases 1995–2026"):
            tag = Tag(text, self.theme_name)
            self.chips.append(tag)
            chips.addWidget(tag)
        chips.addStretch(1)
        self.query_status = caption("", "Faint", 8.5)
        self.query_status.setSizePolicy(QSizePolicy.Policy.Preferred,
                                        QSizePolicy.Policy.Preferred)
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

        self.locator_stack = QStackedWidget()
        self.locator_empty = EmptyState(
            "mdi6.magnify",
            "No search run yet",
            "Results appear here as ranked task locators — task number, title, "
            "manual and revision. The procedure itself is never reproduced; the "
            "engineer is sent to the controlled manual.",
            theme=self.theme_name)
        self.locator_stack.addWidget(self.locator_empty)

        # A distinct state, not a reworded one: EmptyState sizes its body at
        # construction, so replacing the text of a live one clips it.
        self.locator_nomatch = EmptyState(
            "mdi6.file-search-outline",
            "Nothing matched",
            "No task in the ingested corpus matched this symptom. The corpus "
            "holds one aircraft type and ten AMM chapters, so a miss here is "
            "as likely to be a coverage gap as a retrieval failure — check the "
            "chapter coverage on the Manuals screen before concluding either.",
            theme=self.theme_name)
        self.locator_stack.addWidget(self.locator_nomatch)

        # Abstention is a first-class answer, not an empty result. The engine
        # could not previously produce it at all: max-normalisation pins the
        # top score at 1.0, so no absolute threshold could ever fire and 94% of
        # queries returned a confident wrong locator.
        self.locator_abstain = EmptyState(
            "mdi6.help-circle-outline",
            "No confident match — escalate to a licensed engineer",
            "Candidates were found, but none clears the confidence threshold "
            "fitted for this ATA chapter, so none is shown as an answer. In a "
            "maintenance tool a confident wrong locator is worse than no "
            "locator. Use 'Show candidates anyway' only as a starting point "
            "for your own search of the controlled manual.",
            theme=self.theme_name)
        self.locator_stack.addWidget(self.locator_abstain)

        self.results_host = QWidget()
        self.results_lay = QVBoxLayout(self.results_host)
        self.results_lay.setContentsMargins(11, 10, 11, 10)
        self.results_lay.setSpacing(7)
        self.results_lay.addStretch(1)
        self.results_area = QScrollArea()
        self.results_area.setWidgetResizable(True)
        self.results_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.results_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results_area.setWidget(self.results_host)
        self.locator_stack.addWidget(self.results_area)
        lay.addWidget(self.locator_stack, 1)

        actions = QFrame()
        actions.setObjectName("PageHeader")
        actions.setFixedHeight(44)
        arow = QHBoxLayout(actions)
        arow.setContentsMargins(13, 0, 13, 0)
        arow.setSpacing(9)
        self.selection_label = caption("no locator selected", "Faint", 8.5)
        arow.addWidget(self.selection_label, 1)
        self.override_btn = QPushButton("Show candidates anyway")
        self.override_btn.setVisible(False)
        self.override_btn.clicked.connect(self._show_anyway)
        arow.addWidget(self.override_btn)
        self.print_btn = QPushButton("Print locator")
        self.print_btn.setEnabled(False)
        self.print_btn.clicked.connect(self._print_selected)
        arow.addWidget(self.print_btn)
        lay.addWidget(actions)
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
        self.cases.setWordWrap(False)
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
    def on_shown(self) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        """Report exactly what the engine can and cannot do right now."""
        status = index_status(getattr(self.ctx, "corpus", None)
                              and self.ctx.corpus.con)
        self.index_ready = status.ready
        if status.ready:
            self.query_status.setText(
                f"{status.task_vectors:,} task locators · "
                f"{status.case_vectors:,} cases indexed")
        else:
            self.query_status.setText(status.reason)
        self.search_btn.setEnabled(True)

    def run_search(self) -> None:
        text = self.query.text().strip()
        if not text:
            self.query_status.setText("enter a symptom to search")
            return
        if not getattr(self, "index_ready", False):
            self._refresh_status()
            if not self.index_ready:
                return
        if not self.service.submit(text, self.signals):
            self.query_status.setText("a search is already running")
            return
        self.search_btn.setEnabled(False)
        self.query_status.setText("searching — first query loads the index…")

    def _on_results(self, result: dict) -> None:
        self.search_btn.setEnabled(True)
        run = result["tasks"]
        self._last_run = run
        self._render_locators(run, abstain=result.get("abstain", False))
        self._render_cases(result["case_rows"])
        kind = "exact token" if run.exact_query else "free text"
        note = result.get("calibration_note", "")
        if not result.get("calibrated"):
            note = "not calibrated — run scripts/phase2_calibrate.py"
        self.query_status.setText(
            f"{len(run.results)} of {run.stage1_size} candidates · {kind} · "
            f"keyword {run.weights['fts']:.2f} / semantic "
            f"{run.weights['dense']:.2f} · {note}")

    def _on_failed(self, query: str, message: str) -> None:
        self.search_btn.setEnabled(True)
        self.query_status.setText(f"search failed — {message}")

    def _render_locators(self, run, abstain: bool = False) -> None:
        for row in self.rows:
            row.setParent(None)
        self.rows.clear()
        self.selected = None
        self.print_btn.setEnabled(False)
        self.selection_label.setText("no locator selected")

        if not run.results:
            self.locator_header.set_right("no match · locator only")
            self.override_btn.setVisible(False)
            self.locator_stack.setCurrentWidget(self.locator_nomatch)
            return

        if abstain:
            self.locator_header.set_right("below confidence threshold")
            self.override_btn.setVisible(True)
            self.locator_stack.setCurrentWidget(self.locator_abstain)
            return
        self.override_btn.setVisible(False)

        for i, result in enumerate(run.results, 1):
            row = LocatorRow(i, result, self.theme_name)
            row.clicked.connect(self._select_row)
            self.results_lay.insertWidget(self.results_lay.count() - 1, row)
            self.rows.append(row)
        self.locator_header.set_right(
            f"{len(run.results)} shown · locator only")
        self.locator_stack.setCurrentWidget(self.results_area)

    def _show_anyway(self) -> None:
        """Deliberate override. The engineer asked for the candidates after
        being told they are below the bar, so they are shown and labelled — not
        promoted to an answer."""
        run = getattr(self, "_last_run", None)
        if run is None:
            return
        self.override_btn.setVisible(False)
        self._render_locators(run, abstain=False)
        self.locator_header.set_right("below threshold · shown on request")

    def _select_row(self, row: LocatorRow) -> None:
        for other in self.rows:
            other.set_selected(other is row)
        self.selected = row
        self.print_btn.setEnabled(True)
        self.selection_label.setText(row.result.task_number or "—")

    def _print_selected(self) -> None:
        if self.selected is None:
            return
        result = self.selected.result
        prov = result.provenance or {}
        task = {
            "task_number": result.task_number,
            "title": result.title,
            "effectivity_raw": prov.get("applicability"),
            "id": result.id,
        }
        manual = {
            "manual_type": result.manual_type,
            "revision": prov.get("manual_revision"),
        }
        self.ctx.print_locator(task, manual)

    def _render_cases(self, rows) -> None:
        self.cases.setRowCount(len(rows))
        pal = T.THEMES[self.theme_name]
        for r, case in enumerate(rows):
            for c, text in enumerate((case.tail, case.reported_at,
                                      case.replaced, case.found)):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if c == 3 and not case.found_recorded:
                    item.setForeground(Qt.GlobalColor.gray)
                    item.setToolTip(
                        "SDR records what was reported and what was replaced. "
                        "The shop teardown finding is not in this data.")
                self.cases.setItem(r, c, item)
        recorded = sum(1 for case in rows if case.found_recorded)
        self.cases_header.set_right(
            f"{len(rows)} cases · {recorded} with a recorded finding")
        _ = pal

    def refresh_theme(self, theme: str) -> None:
        super().refresh_theme(theme)
        for row in self.rows:
            row.refresh_theme(theme)
