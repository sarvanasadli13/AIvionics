"""Reliability — repeat-defect analysis for the reliability engineer (PLAN 4.6).

The screen is built around three refusals, each of which is the plan's own:

* **No rate appears without its n.** `metrics.Rate.value` is None below the
  support threshold and the cell renders "n too small (n=3)" instead. "43% from
  7 cases" is statistically void and rhetorically powerful, which is the worst
  possible combination in a maintenance decision.
* **The metric is never called NFF.** It is *removals followed by a repeat of
  the same defect within N days*. True No Fault Found is a shop teardown
  finding, determined weeks later by the repair vendor, and it is not in this
  data at all.
* **Nothing is attributed to a person.** Aggregate only — standing rule 6.

Every figure also carries how much the case base actually knows: a corpus of
removals with almost no recorded findings is a record of what was swapped, not
of what was wrong, and the header says so on every render.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout,
                               QWidget)

from .. import theme as T
from ..statsservice import StatsService, StatsSignals
from ..widgets import (Card, EmptyState, Placard, ProvenanceLine, SectionHeader,
                       Tag, mono_font, ui_font)
from .base import Page, caption

from ...stats import metrics

CHAPTER_COLUMNS = ["ATA", "Chapter", "Removals", "Repeats", "Rate", "95% CI"]
PART_COLUMNS = ["Part number", "Description", "ATA", "Removals", "Repeats",
                "Rate", "95% CI"]
EVENT_COLUMNS = ["Tail", "Reported", "ATA", "Days apart", "Match", "Symptom"]


def _rate_item(rate: metrics.Rate, pal: dict) -> QTableWidgetItem:
    """A rate cell that can never show a number without its support."""
    item = QTableWidgetItem(rate.text)
    if rate.suppressed:
        item.setForeground(QColor(pal["txt3"]))
        item.setToolTip(
            f"Suppressed: {rate.n} case(s) is below the support threshold of "
            f"{rate.min_support}. A percentage from this few cases would be "
            f"statistically void.")
    else:
        item.setToolTip(f"{rate.support_text} · {rate.metric_name()}")
    return item


class ReliabilityPage(Page):
    title = "Reliability"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.service = StatsService(getattr(ctx, "db_path", None))
        self.signals = StatsSignals()
        self.signals.done.connect(self._on_snapshot)
        self.signals.failed.connect(self._on_failed)
        self.snapshot = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._filter_band())
        outer.addWidget(self._headline())
        outer.addWidget(self._tabs(), 1)
        outer.addWidget(self._provenance_footer())

    # ── filters ───────────────────────────────────────────────────────
    def _filter_band(self) -> QWidget:
        band = QFrame()
        band.setObjectName("Band")
        lay = QHBoxLayout(band)
        lay.setContentsMargins(15, 10, 15, 10)
        lay.setSpacing(14)

        lay.addWidget(caption("Period", "Muted", 8.5))
        self.period = QComboBox()
        for label, days in metrics.PERIODS:
            self.period.addItem(label, days)
        self.period.setCurrentIndex(len(metrics.PERIODS) - 1)
        self.period.currentIndexChanged.connect(self.refresh)
        lay.addWidget(self.period)

        lay.addWidget(caption("Repeat window", "Muted", 8.5))
        self.window = QComboBox()
        for days in (7, 14, 30, 60, 90):
            self.window.addItem(f"{days} days", days)
        self.window.setCurrentIndex(2)
        self.window.currentIndexChanged.connect(self.refresh)
        lay.addWidget(self.window)

        lay.addWidget(caption("Tail", "Muted", 8.5))
        self.tail = QComboBox()
        self.tail.addItem("Whole fleet", None)
        self.tail.setMinimumWidth(150)
        self.tail.currentIndexChanged.connect(self.refresh)
        lay.addWidget(self.tail)

        lay.addStretch(1)
        self.status = caption("", "Faint", 8.5)
        lay.addWidget(self.status)
        return band

    # ── headline ──────────────────────────────────────────────────────
    def _headline(self) -> QWidget:
        card = Card()
        lay = QVBoxLayout(card)
        lay.setContentsMargins(15, 12, 15, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(12)
        self.metric_label = Placard("Removal → repeat rate")
        top.addWidget(self.metric_label)
        self.headline_tag = Tag("whole fleet", self.theme_name)
        top.addWidget(self.headline_tag)
        top.addStretch(1)
        self.findings_label = caption("", "Faint", 8.5)
        top.addWidget(self.findings_label)
        lay.addLayout(top)

        self.headline_value = QLabel("—")
        self.headline_value.setFont(ui_font(22, QFont.Weight.DemiBold, tabular=True))
        lay.addWidget(self.headline_value)

        self.headline_detail = caption("", "Muted", 9)
        lay.addWidget(self.headline_detail)

        self.simpson = caption("", "Muted", 8.5)
        self.simpson.setWordWrap(True)
        lay.addWidget(self.simpson)
        return card

    # ── tables ────────────────────────────────────────────────────────
    def _table(self, columns: list[str], stretch: int) -> QTableWidget:
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(T.ROW_HEIGHT)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        header = table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
        for i in range(len(columns)):
            header.setSectionResizeMode(
                i, QHeaderView.ResizeMode.Stretch if i == stretch
                else QHeaderView.ResizeMode.ResizeToContents)
        return table

    def _tabs(self) -> QWidget:
        self.tabs = QTabWidget()
        self.chapter_table = self._table(CHAPTER_COLUMNS, 1)
        self.part_table = self._table(PART_COLUMNS, 1)
        self.event_table = self._table(EVENT_COLUMNS, 5)
        self.tabs.addTab(self.chapter_table, "By ATA chapter")
        self.tabs.addTab(self.part_table, "Repeat offenders")
        self.tabs.addTab(self.event_table, "Repeat events")

        self.empty = EmptyState(
            "mdi6.chart-line",
            "No case base yet",
            "Repeat-defect statistics are built from the case base. Run "
            "scripts/phase3.py to extract actions and findings from the "
            "ingested reports, then reopen this screen.",
            theme=self.theme_name)
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(SectionHeader("Repeat-defect analysis",
                                    "aggregate only · never per engineer"))
        lay.addWidget(self.tabs, 1)
        lay.addWidget(self.empty, 1)
        self.empty.hide()
        return host

    def _provenance_footer(self) -> QWidget:
        foot = QFrame()
        foot.setObjectName("PageHeader")
        lay = QVBoxLayout(foot)
        lay.setContentsMargins(15, 8, 15, 9)
        self.provenance = ProvenanceLine(
            metrics.PROVENANCE_TEXT[metrics.SDR_MINED]
            + " · without the operator's tech log and shop findings every "
              "figure here is directional evidence, not a measured rate")
        lay.addWidget(self.provenance)
        return foot

    # ── behaviour ─────────────────────────────────────────────────────
    def on_shown(self) -> None:
        if self.tail.count() <= 1:
            for t in self.service.tails()[:2000]:
                self.tail.addItem(t, t)
        self.refresh()

    def refresh(self) -> None:
        ok, reason = self.service.ready()
        if not ok:
            self.status.setText(reason)
            self.tabs.hide()
            self.empty.show()
            return
        if not self.service.submit(
                self.signals,
                period_days=self.period.currentData(),
                period_label=self.period.currentText(),
                tail=self.tail.currentData(),
                window_days=self.window.currentData()):
            return
        self.status.setText("computing…")

    def _on_failed(self, message: str) -> None:
        self.status.setText(f"failed — {message}")

    def _on_snapshot(self, snap) -> None:
        self.snapshot = snap
        if not snap.available:
            self.status.setText(snap.reason)
            self.tabs.hide()
            self.empty.show()
            return
        self.empty.hide()
        self.tabs.show()
        pal = T.THEMES[self.theme_name]

        rate = snap.fleet
        self.metric_label.setText(
            metrics.METRIC_SHORT.format(days=rate.window_days).upper())
        self.headline_tag.setText(rate.label or "fleet")
        self.headline_value.setText(rate.text)
        self.headline_detail.setText(
            f"{rate.support_text} removals · 95% CI {rate.interval_text} · "
            f"{snap.period_label.lower()}"
            + (f" from {snap.since}" if snap.since else ""))
        self.findings_label.setText(snap.findings_text)
        self._render_simpson(snap)

        self._fill_chapters(snap.chapters, pal)
        self._fill_parts(snap.parts, pal)
        self._fill_events(snap.events)
        self.status.setText(
            f"{len(snap.chapters)} chapters · {len(snap.parts)} part numbers · "
            f"latest report {snap.latest_report or 'unknown'}")

    def _render_simpson(self, snap) -> None:
        strat = snap.stratified
        if strat is None:
            self.simpson.setText("")
            return
        if strat.direction_conflict:
            self.simpson.setText(
                "⚠ The pooled figure falls outside every airframe-standard "
                "group taken separately. That is a Simpson reversal: the "
                "groups and the total disagree because they are unequally "
                "sized. Read the per-standard figures, not the pooled one — "
                + " · ".join(f"{r.label} {r.text}" for r in strat.reportable))
        else:
            parts = " · ".join(f"{r.label} {r.text}" for r in strat.reportable)
            self.simpson.setText(
                f"By airframe standard: {parts}" if parts else
                "No airframe-standard group clears the support threshold.")

    def _fill_chapters(self, rows, pal) -> None:
        from ...parsers.ata import chapter_name
        self.chapter_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            rate = row["rate"]
            cells = [QTableWidgetItem(row["chapter"] or "—"),
                     QTableWidgetItem(chapter_name(row["chapter"] or "")),
                     QTableWidgetItem(f"{rate.n:,}"),
                     QTableWidgetItem(f"{rate.numerator:,}"),
                     _rate_item(rate, pal),
                     QTableWidgetItem(rate.interval_text)]
            cells[0].setFont(mono_font(10))
            for c, item in enumerate(cells):
                self.chapter_table.setItem(r, c, item)

    def _fill_parts(self, rows, pal) -> None:
        self.part_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            rate = row["rate"]
            cells = [QTableWidgetItem(row["part_number"]),
                     QTableWidgetItem(row["part_name"]),
                     QTableWidgetItem(row["ata_chapter"]),
                     QTableWidgetItem(f"{rate.n:,}"),
                     QTableWidgetItem(f"{rate.numerator:,}"),
                     _rate_item(rate, pal),
                     QTableWidgetItem(rate.interval_text)]
            cells[0].setFont(mono_font(10))
            for c, item in enumerate(cells):
                self.part_table.setItem(r, c, item)

    def _fill_events(self, rows) -> None:
        self.event_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            sym = " ".join((row.get("symptom") or "").split())[:160]
            cells = [QTableWidgetItem(row.get("tail") or "—"),
                     QTableWidgetItem(row.get("reported_at") or "—"),
                     QTableWidgetItem(row.get("chapter") or "—"),
                     QTableWidgetItem(str(row.get("days_apart", "—"))),
                     QTableWidgetItem(f"{(row.get('similarity') or 0):.2f}"),
                     QTableWidgetItem(sym)]
            cells[0].setFont(mono_font(10))
            cells[5].setToolTip(sym)
            for c, item in enumerate(cells):
                self.event_table.setItem(r, c, item)
