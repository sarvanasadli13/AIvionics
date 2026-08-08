"""Fleet register — the aircraft this installation knows about (PLAN 4.7).

Read-only here by design: the register is edited under Admin, because a tail's
year of manufacture and modification state are what the statistics stratify on
(PLAN 3.7), and an engineer correcting one in passing would silently move every
rate on the Reliability screen.

`line_number` and `year_built` are carried explicitly rather than derived. They
are the only things that separate a 1999-standard airframe from a 2015-standard
one, and pooling those two is how a rate ends up directionally wrong.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from .. import theme as T
from ..statsservice import StatsService
from ..widgets import (EmptyState, ProvenanceLine, SectionHeader, Tag,
                       mono_font, ui_font)
from .base import Page, caption

from ...stats.metrics import airframe_standard

COLUMNS = ["Tail", "Type", "MSN", "Line no.", "Built", "Standard",
           "Hours", "Cycles", "Defects"]


class FleetPage(Page):
    title = "Fleet"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.service = StatsService(getattr(ctx, "db_path", None))
        self.rows: list[dict] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._filter_band())

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._register())
        split.addWidget(self._detail())
        split.setSizes([720, 340])
        outer.addWidget(split, 1)
        outer.addWidget(self._footer())

    def _filter_band(self) -> QWidget:
        band = QFrame()
        band.setObjectName("Band")
        lay = QHBoxLayout(band)
        lay.setContentsMargins(15, 10, 15, 10)
        lay.setSpacing(12)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by tail, type or MSN")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(32)
        self.search.textChanged.connect(self._apply_filter)
        lay.addWidget(self.search, 1)
        self.count = caption("", "Faint", 8.5)
        lay.addWidget(self.count)
        return band

    def _register(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(SectionHeader("Fleet register", "read-only · edited in Admin"))

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(T.ROW_HEIGHT)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
        for i in range(len(COLUMNS)):
            header.setSectionResizeMode(
                i, QHeaderView.ResizeMode.Stretch if i == 1
                else QHeaderView.ResizeMode.ResizeToContents)
        self.table.currentCellChanged.connect(
            lambda r, *_: self._show_detail(r))
        lay.addWidget(self.table, 1)

        self.empty = EmptyState(
            "mdi6.airplane",
            "No aircraft registered",
            "The fleet register is empty. Aircraft are added under Admin, or "
            "imported alongside the compliance export. Per-tail statistics and "
            "airframe-standard stratification both depend on this table.",
            theme=self.theme_name)
        lay.addWidget(self.empty, 1)
        self.empty.hide()
        return host

    def _detail(self) -> QWidget:
        host = QWidget()
        host.setObjectName("Card")
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.detail_header = SectionHeader("Configuration", "no tail selected")
        lay.addWidget(self.detail_header)

        body = QWidget()
        self.detail_lay = QVBoxLayout(body)
        self.detail_lay.setContentsMargins(15, 12, 15, 12)
        self.detail_lay.setSpacing(8)
        self.detail_hint = caption(
            "Select an aircraft to see its recorded modification state — "
            "service bulletins, STCs and software load. An unrecorded "
            "configuration is why a retrofitted system can be returned OEM "
            "procedure with false confidence.", "Muted", 9)
        self.detail_lay.addWidget(self.detail_hint)
        self.detail_lay.addStretch(1)
        lay.addWidget(body, 1)
        return host

    def _footer(self) -> QWidget:
        foot = QFrame()
        foot.setObjectName("PageHeader")
        lay = QVBoxLayout(foot)
        lay.setContentsMargins(15, 8, 15, 9)
        lay.addWidget(ProvenanceLine(
            "Year of manufacture and line number drive airframe-standard "
            "stratification on the Reliability screen. A tail with no year "
            "recorded is grouped as 'year unknown' rather than assumed modern."))
        return foot

    # ── data ──────────────────────────────────────────────────────────
    def on_shown(self) -> None:
        self.rows = self.service.aircraft()
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self.search.text().strip().upper()
        rows = [r for r in self.rows
                if not needle
                or needle in (r.get("tail") or "").upper()
                or needle in (r.get("type") or "").upper()
                or needle in (r.get("msn") or "").upper()]
        self.table.setVisible(bool(self.rows))
        self.empty.setVisible(not self.rows)
        self.count.setText(
            f"{len(rows):,} of {len(self.rows):,} aircraft"
            if self.rows else "register empty")

        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            hrs = r.get("total_time_hrs")
            cyc = r.get("total_cycles")
            cells = [
                QTableWidgetItem(r.get("tail") or "—"),
                QTableWidgetItem(r.get("type") or "—"),
                QTableWidgetItem(r.get("msn") or "—"),
                QTableWidgetItem(r.get("line_number") or "—"),
                QTableWidgetItem(str(r.get("year_built") or "—")),
                QTableWidgetItem(airframe_standard(r.get("year_built"))),
                QTableWidgetItem(f"{hrs:,.0f}" if hrs else "—"),
                QTableWidgetItem(f"{cyc:,}" if cyc else "—"),
                QTableWidgetItem(f"{r.get('defects', 0):,}"),
            ]
            cells[0].setFont(mono_font(10))
            for c, item in enumerate(cells):
                self.table.setItem(i, c, item)
        self._filtered = rows

    def _show_detail(self, row: int) -> None:
        rows = getattr(self, "_filtered", [])
        if not (0 <= row < len(rows)):
            return
        tail = rows[row].get("tail")
        self.detail_header.set_right(tail or "—")
        while self.detail_lay.count():
            item = self.detail_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        records = self.service.config_records(tail)
        if not records:
            self.detail_lay.addWidget(caption(
                f"No configuration recorded for {tail}. Effectivity cannot be "
                "resolved for this tail, so applicable tasks will be reported "
                "as unresolved rather than filtered — the fail-closed rule.",
                "Muted", 9))
        else:
            for rec in records:
                card = QFrame()
                card.setObjectName("Card")
                cl = QVBoxLayout(card)
                cl.setContentsMargins(11, 9, 11, 10)
                cl.setSpacing(4)
                head = QLabel(rec.get("effective_from") or "undated")
                head.setFont(ui_font(9.5, tabular=True))
                cl.addWidget(head)
                for key, label in (("sb_embodied", "SB embodied"),
                                   ("stc", "STC"),
                                   ("software_load", "Software load")):
                    if rec.get(key):
                        cl.addWidget(caption(f"{label}: {rec[key]}", "Muted", 9))
                self.detail_lay.addWidget(card)
        self.detail_lay.addStretch(1)
