"""Fleet, Reliability, Compliance, Ops and Admin.

Structured placeholders: the headers, the provenance pattern and the
honest-limitation copy are real, the data wiring is not. Getting the frame
right first is deliberate — the rules these screens have to obey (standing
rules 2, 4 and 6) are structural, and retrofitting them onto a screen built
without them is how a shadow compliance clock gets shipped.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel,
                               QHeaderView, QTableWidget, QVBoxLayout, QWidget)

from .. import theme as T
from ..widgets import (EmptyState, ProvenanceLine, SectionHeader, StatusBadge,
                       ui_font)
from .base import Page, caption


class _TablePage(Page):
    """A dense table page with a header, a provenance line and an empty state."""

    columns: list[str] = []
    header_right = ""
    provenance = ""
    empty_icon = "mdi6.database-off-outline"
    empty_head = ""
    empty_body = ""
    banner: tuple[str, str] | None = None      # (status kind, text)

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        lay.addWidget(SectionHeader(self.title, self.header_right))

        if self.banner:
            lay.addWidget(self._banner(*self.banner))

        if self.provenance:
            line = ProvenanceLine(self.provenance)
            line.setContentsMargins(15, 9, 15, 6)
            lay.addWidget(line)

        self.table = QTableWidget(0, len(self.columns))
        self.table.setHorizontalHeaderLabels(self.columns)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(T.ROW_HEIGHT)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
        # Keep the header visible while there are no rows: the column set is
        # the part of this screen that is already decided, and hiding it makes
        # the page look unfinished rather than empty.
        self.table.setFixedHeight(header.sizeHint().height() + 2)
        lay.addWidget(self.table)

        lay.addWidget(EmptyState(self.empty_icon, self.empty_head, self.empty_body,
                                 theme=self.theme_name), 1)

    def _banner(self, kind: str, text: str) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Well")
        row = QHBoxLayout(bar)
        row.setContentsMargins(11, 8, 11, 8)
        row.setSpacing(10)
        row.addWidget(StatusBadge(kind, theme=self.theme_name))
        label = QLabel(text)
        label.setObjectName("Muted")
        label.setFont(ui_font(8.5))
        label.setWordWrap(True)
        row.addWidget(label, 1)
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(15, 10, 15, 2)
        wl.addWidget(bar)
        return wrap


class FleetPage(_TablePage):
    title = "Fleet register"
    header_right = "per-tail configuration record"
    columns = ["Tail", "Type", "MSN", "Line no.", "Year built",
               "Total time (h)", "Cycles"]
    empty_head = "No aircraft registered"
    empty_body = ("Tails are added in Admin or imported with the fleet CSV. "
                  "Line number and year of manufacture are held because pooling "
                  "airframes built to different standards can make a fleet "
                  "statistic directionally wrong.")


class ReliabilityPage(_TablePage):
    title = "Reliability"
    header_right = "aggregate only · n always shown"
    columns = ["ATA", "Symptom class", "Removals", "Repeat ≤30 d",
               "Rate", "n", "95% CI"]
    provenance = ("Source: SDR-mined proxy · no operator tech log or shop findings "
                  "imported. Every figure on this page is computed from a "
                  "reportable-occurrence sample — directional evidence, not a "
                  "measured rate.")
    empty_head = "No statistics yet"
    empty_body = ("The repeat-defect metric is 'removals on this part number "
                  "followed by a repeat of the same defect within 30 days' — it "
                  "is not an NFF rate, because NFF is a shop finding and is not "
                  "in this data. Groups below the small-n threshold stay "
                  "suppressed rather than being shown with a wide interval.")


class CompliancePage(_TablePage):
    title = "Compliance register"
    header_right = "checkups · MEL · AD/SB"
    columns = ["Tail", "Kind", "Reference", "Description", "Due",
               "Status", "Source system", "Imported"]
    provenance = ("No CAMO export has been imported. Source system: none · "
                  "import timestamp: none · freshness: unknown.")
    banner = ("warn",
              "The CAMO remains the legal record. This register tracks an "
              "imported mirror; if the last import is older than its freshness "
              "window the whole module degrades and alerts are greyed out.")
    empty_head = "No compliance data imported"
    empty_body = ("Import a CAMO export (CSV or Excel) from Admin. Every row "
                  "carries its source system and import time, and no alert "
                  "renders without that provenance line visible.")


class OpsPage(Page):
    """The entire online-dependent surface: map, airport page, weather.

    One rail item, so switching `online_enabled` off removes exactly one
    thing from the navigation and the offline core is provably untouched
    (standing rule 12).
    """

    title = "Ops"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(SectionHeader("Ops", "live fleet map · airport page · weather"))

        self.offline_state = EmptyState(
            "mdi6.wifi-off",
            "Online features are switched off",
            "Live ADS-B positions, METAR/TAF and arrivals/departures sit behind "
            "the `online_enabled` setting in Admin, with their own network layer "
            "and their own allow-listed hosts. The manuals core, retrieval and "
            "statistics run identically with the network unplugged.",
            theme=self.theme_name)
        lay.addWidget(self.offline_state, 1)

        self.hosts = ProvenanceLine(
            "Outbound hosts when enabled: aviationweather.gov · opensky-network.org. "
            "Airport, runway and navaid data is bundled offline and never calls out.")
        self.hosts.setContentsMargins(15, 8, 15, 12)
        lay.addWidget(self.hosts)

    def on_shown(self) -> None:
        enabled = bool(self.ctx and self.ctx.online_enabled)
        self.offline_state.headline.setText(
            "Online features are enabled — modules not built yet" if enabled
            else "Online features are switched off")


class AdminPage(Page):
    title = "Admin"

    SECTIONS = [
        ("Document ingest", "mdi6.file-import-outline",
         "Load a manual revision, run the per-OEM parser, record coverage."),
        ("Fleet", "mdi6.airplane-cog",
         "Add tails, MSN, line number, year built, configuration record."),
        ("Users and roles", "mdi6.account-key-outline",
         "Accounts and role assignment. Roles are rows, not flags."),
        ("Models and index", "mdi6.brain",
         "Embedding model and index version. Changing either invalidates every "
         "stored vector and every prior measurement — a re-index is forced."),
        ("Coverage report", "mdi6.chart-donut",
         "Extracted tasks versus each chapter's own table of contents."),
        ("Audit viewer", "mdi6.shield-key-outline",
         "Hash-chained log. The chain is verified on startup."),
        ("Online features", "mdi6.web",
         "Master switch plus the allow-listed outbound hosts, so IT can audit "
         "exactly what this machine talks to."),
    ]

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(SectionHeader("Admin", "IT and configuration"))

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(15, 13, 15, 13)
        bl.setSpacing(9)
        for name, icon, detail in self.SECTIONS:
            bl.addWidget(self._section(name, icon, detail))
        bl.addStretch(1)
        lay.addWidget(body, 1)

        self.chain_state = ProvenanceLine("Audit chain: not verified this session.")
        self.chain_state.setContentsMargins(15, 6, 15, 12)
        lay.addWidget(self.chain_state)

    def _section(self, name: str, icon: str, detail: str) -> QWidget:
        import qtawesome as qta
        card = QFrame()
        card.setObjectName("Card")
        row = QHBoxLayout(card)
        row.setContentsMargins(13, 10, 13, 10)
        row.setSpacing(12)
        pal = T.THEMES[self.theme_name]
        glyph = QLabel()
        glyph.setPixmap(qta.icon(icon, color=pal["cy"]).pixmap(18, 18))
        glyph.setFixedWidth(20)
        row.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel(name)
        title.setFont(ui_font(10, QFont.Weight.DemiBold))
        text.addWidget(title)
        text.addWidget(caption(detail, "Muted", 8.5))
        row.addLayout(text, 1)
        row.addWidget(StatusBadge("unknown", "NOT BUILT", self.theme_name),
                      0, Qt.AlignmentFlag.AlignTop)
        return card

    def on_shown(self) -> None:
        if self.ctx and self.ctx.chain_ok is not None:
            ok, rows = self.ctx.chain_ok, self.ctx.chain_rows
            self.chain_state.setText(
                f"Audit chain verified on startup: {'intact' if ok else 'BROKEN'} "
                f"over {rows} rows.")
