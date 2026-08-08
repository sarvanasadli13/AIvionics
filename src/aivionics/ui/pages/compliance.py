"""Compliance register — checkups, MEL deferrals and AD/SB (PLAN 4B.1–4B.3).

This screen exists because the owner restored it, over a stated objection: the
CAMO is the legal record, and a second copy of a compliance clock is a shadow
record that people can come to rely on. The mitigation is not to hide the
clocks, it is to make their provenance impossible to lose:

* Every row is a `compliance.ComplianceRow`, and that type refuses to be
  constructed without a `Provenance`. "No alert renders without its source and
  import time" is enforced by the constructor, not by whoever wrote this file.
* One stale source degrades **the whole register**, not just its own rows — an
  engineer looking at an amber badge cannot tell which feed produced it, so
  partial trust is not a state offered here. Stale rows lose their triage
  colour and wear NO DATA instead.
* The provenance line sits under the header at all times, never in a tooltip.

Checkups are tracked on calendar **or** flight hours **or** cycles, whichever
falls first. A date-only scheduler would be quietly wrong on exactly the items
that matter most.
"""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog,
                               QFrame, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from .. import theme as T
from ..widgets import (EmptyState, ProvenanceLine, SectionHeader, StatusBadge,
                       mono_font, ui_font)
from .base import Page, caption

from ...ops import compliance, importer

COLUMNS = ["State", "Tail", "Kind", "Reference / description", "Limit",
           "Remaining", "Source"]


class CompliancePage(Page):
    title = "Compliance"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.rows: list[compliance.ComplianceRow] = []
        self.state: compliance.ModuleState | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._filter_band())
        outer.addWidget(self._banner())
        outer.addWidget(self._register(), 1)
        outer.addWidget(self._provenance_footer())

    # ── filters ───────────────────────────────────────────────────────
    def _filter_band(self) -> QWidget:
        band = QFrame()
        band.setObjectName("Band")
        lay = QHBoxLayout(band)
        lay.setContentsMargins(15, 10, 15, 10)
        lay.setSpacing(12)

        lay.addWidget(caption("Kind", "Muted", 8.5))
        self.kind = QComboBox()
        self.kind.addItem("All", None)
        for key in compliance.KINDS:
            self.kind.addItem(compliance.KIND_LABEL[key], key)
        self.kind.currentIndexChanged.connect(self.reload)
        lay.addWidget(self.kind)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by tail or reference")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(30)
        self.search.textChanged.connect(self._render)
        lay.addWidget(self.search, 1)

        self.import_btn = QPushButton("Import CAMO export…")
        self.import_btn.clicked.connect(self._import)
        lay.addWidget(self.import_btn)

        self.counts = caption("", "Faint", 8.5)
        lay.addWidget(self.counts)
        return band

    def _banner(self) -> QWidget:
        self.banner = QLabel("")
        self.banner.setObjectName("Banner")
        self.banner.setWordWrap(True)
        self.banner.setFont(ui_font(9, QFont.Weight.DemiBold))
        self.banner.setContentsMargins(15, 9, 15, 9)
        self.banner.hide()
        return self.banner

    def _register(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = SectionHeader("Compliance register",
                                    "mirror of the CAMO export · not the legal record")
        lay.addWidget(self.header)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(T.ROW_HEIGHT + 4)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
        for i in range(len(COLUMNS)):
            header.setSectionResizeMode(
                i, QHeaderView.ResizeMode.Stretch if i == 3
                else QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self.table, 1)

        self.empty = EmptyState(
            "mdi6.calendar-clock",
            "No compliance data imported",
            "Checkups, MEL deferrals and AD/SB rows arrive from a CAMO export. "
            "Until one is imported this register stays empty rather than "
            "showing a clock this application cannot vouch for. Use "
            "'Import CAMO export…' with a CSV from your maintenance system.",
            theme=self.theme_name)
        lay.addWidget(self.empty, 1)
        return host

    def _provenance_footer(self) -> QWidget:
        foot = QFrame()
        foot.setObjectName("PageHeader")
        lay = QVBoxLayout(foot)
        lay.setContentsMargins(15, 8, 15, 9)
        self.provenance = ProvenanceLine("")
        lay.addWidget(self.provenance)
        return foot

    # ── data ──────────────────────────────────────────────────────────
    def _con(self):
        corpus = getattr(self.ctx, "corpus", None)
        return getattr(corpus, "con", None)

    def on_shown(self) -> None:
        self.reload()

    def reload(self) -> None:
        con = self._con()
        now = datetime.now(timezone.utc)
        self.state = compliance.module_state(con, now)
        self.rows = compliance.load_rows(
            con, kind=self.kind.currentData(), now=now, state=self.state)
        self._render()

    def _render(self) -> None:
        state = self.state
        needle = self.search.text().strip().upper()
        rows = [r for r in self.rows
                if not needle
                or needle in r.tail.upper()
                or needle in r.title().upper()]

        has_data = bool(state and state.has_data)
        self.table.setVisible(has_data)
        self.empty.setVisible(not has_data)

        if state is not None and state.degraded:
            pal = T.THEMES[self.theme_name]
            self.banner.setText("⚠ " + state.banner())
            self.banner.setStyleSheet(
                f"background:{pal['ambq']};color:{pal['amb']};"
                f"border-bottom:1px solid {pal['amb']};")
            self.banner.show()
        else:
            self.banner.hide()

        self.provenance.setText(
            state.provenance_line() if state else
            "Source system: none · import timestamp: none")

        counts = compliance.counts_by_state(rows)
        self.counts.setText(
            " · ".join(f"{compliance.STATE_WORD[k].lower()} {counts.get(k, 0)}"
                       for k in (compliance.BREACHED, compliance.WARNING,
                                 compliance.OK, compliance.UNKNOWN))
            if rows else "nothing imported")
        self.header.set_right(
            f"{len(rows)} of {len(self.rows)} items · ordered by time to limit")

        self.table.setRowCount(len(rows))
        badge_width = 0
        for i, row in enumerate(rows):
            badge = StatusBadge(row.badge_kind, row.badge_word, self.theme_name)
            badge_width = max(badge_width, badge.sizeHint().width())
            self.table.setCellWidget(i, 0, badge)
            cells = [
                None,
                QTableWidgetItem(row.tail),
                QTableWidgetItem(row.kind_label
                                 + (f" {row.item.mel_category}"
                                    if row.item.mel_category else "")),
                QTableWidgetItem(row.title()),
                QTableWidgetItem(row.due.due_text()),
                QTableWidgetItem(row.due.remaining_text()),
                QTableWidgetItem(row.provenance.source_system),
            ]
            for c, item in enumerate(cells):
                if item is None:
                    continue
                if c == 1:
                    item.setFont(mono_font(10))
                if row.provenance.stale:
                    # A row nobody can trust must not read like one that can.
                    item.setForeground(QColor(T.THEMES[self.theme_name]["txt3"]))
                item.setToolTip(row.provenance.line())
                self.table.setItem(i, c, item)

        # ResizeToContents measures items, not cell widgets, so the status
        # badges were clipped mid-word ("BREACHED · STA"). Size that column
        # from the widgets themselves.
        if badge_width:
            self.table.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(0, badge_width + 20)

    # ── import ────────────────────────────────────────────────────────
    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import a CAMO compliance export", "",
            "CSV export (*.csv);;All files (*)")
        if not path:
            return
        source, ok = self._ask_source()
        if not ok:
            return
        try:
            report = importer.import_file(
                self._writable_con(), path, source_system=source,
                dry_run=True)
        except Exception as exc:                                 # noqa: BLE001
            QMessageBox.warning(self, "Import failed", str(exc))
            return

        if not report.ok:
            QMessageBox.warning(
                self, "Nothing imported",
                report.summary()
                + "\n\nThe file was not imported. A partial register looks "
                  "complete and is not, so the whole import is refused.")
            return

        confirm = QMessageBox.question(
            self, "Confirm import",
            f"{report.summary()}.\n\nEvery existing row from "
            f"'{source}' will be replaced — a CAMO export is a snapshot, and "
            f"merging would leave closed items on the register with nothing "
            f"to clear them.\n\nImport now?")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            importer.commit(self._writable_con(), report)
        except Exception as exc:                                 # noqa: BLE001
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self.reload()

    def _ask_source(self) -> tuple[str, bool]:
        from PySide6.QtWidgets import QInputDialog
        return QInputDialog.getText(
            self, "Source system",
            "Which system produced this export?\n"
            "This is stamped on every row and shown beside every alert.",
            text="CAMO export")

    def _writable_con(self):
        """The import is the one write this screen performs."""
        return getattr(self.ctx, "con", None)
