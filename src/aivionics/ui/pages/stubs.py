"""Admin.

Structured placeholders: the headers, the provenance pattern and the
honest-limitation copy are real, the data wiring is not. Getting the frame
right first is deliberate — the rules these screens have to obey (standing
rules 2, 4 and 6) are structural, and retrofitting them onto a screen built
without them is how a shadow compliance clock gets shipped.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import store
from .. import theme as T
from ..widgets import (EmptyState, Placard, ProvenanceLine, SectionHeader,
                       StatusBadge, ui_font)
from .base import Page, caption, scroll_host

from ...llm import client as llm
from ...ops import net


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





class OnlineSection(QWidget):
    """The outbound picture, in the terms IT will ask about (standing rule 12).

    Three questions, answered on one card: may this machine call out at all,
    which hosts is it permitted to reach, and which of them has it actually
    reached this session. The host table is `net.HOST_REGISTRY` rendered
    verbatim — adding a row there is a code change, not a setting, and this
    screen is deliberately incapable of adding one.
    """

    toggled = Signal(bool)

    # The second network path in the application, and the reason it is on this
    # screen rather than folded into the table above: it is not allow-listed,
    # because it is not an internet host. Leaving it off a screen headed "what
    # this machine talks to" would make that screen quietly wrong.
    LOCAL_ENDPOINT_NOTE = (
        f"The optional LLM layer opens its own connection to an Ollama "
        f"endpoint, {llm.DEFAULT_ENDPOINT} by default — this machine. It is "
        f"not allow-listed because it is not an internet host; point it at a "
        f"LAN server and it stays inside your network. It is off unless the "
        f"LLM layer is enabled, and every feature works without it.")

    def __init__(self, enabled: bool, theme: str = T.DEFAULT_THEME, parent=None):
        super().__init__(parent)
        self.theme_name = theme
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(9)

        card = QFrame()
        card.setObjectName("Card")
        box = QVBoxLayout(card)
        box.setContentsMargins(13, 10, 13, 11)
        box.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel("Online features")
        title.setFont(ui_font(10, QFont.Weight.DemiBold))
        head.addWidget(title)
        head.addStretch(1)
        self.switch = QCheckBox("Allow outbound connections")
        self.switch.setChecked(enabled)
        self.switch.toggled.connect(self.toggled.emit)
        head.addWidget(self.switch)
        box.addLayout(head)

        box.addWidget(caption(
            "Off by default. With this off the application makes no internet "
            "connection at all: the manuals core, retrieval, the case base and "
            "the statistics run identically with the network cable out, and the "
            "Ops screen still serves bundled airport data, runways and local "
            "time. The one connection this switch does not govern is named "
            "below.", "Muted", 8.5))

        box.addWidget(Placard("Allow-listed hosts"))
        hosts = QTableWidget(len(net.HOST_REGISTRY), 3)
        hosts.setHorizontalHeaderLabels(["Host", "What it is used for", "Terms"])
        hosts.verticalHeader().setVisible(False)
        hosts.verticalHeader().setDefaultSectionSize(T.ROW_HEIGHT + 8)
        hosts.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hosts.setShowGrid(False)
        hosts.setWordWrap(True)
        header = hosts.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
        for i in range(3):
            header.setSectionResizeMode(
                i, QHeaderView.ResizeMode.ResizeToContents if i == 0
                else QHeaderView.ResizeMode.Stretch)
        for i, host in enumerate(net.HOST_REGISTRY):
            for c, value in enumerate((host.host, host.purpose, host.terms)):
                hosts.setItem(i, c, QTableWidgetItem(value))
        # Let the terms wrap to their real height. A fixed row height elided
        # OpenSky's row mid-sentence, on the clause about incomplete coverage.
        hosts.resizeRowsToContents()
        hosts.setFixedHeight(
            header.sizeHint().height() + 4
            + sum(hosts.rowHeight(i) for i in range(len(net.HOST_REGISTRY))))
        box.addWidget(hosts)
        box.addWidget(caption(
            "A host outside this list is refused before a socket is opened, "
            "and the match is on whole labels — opensky-network.org.example.com "
            "is rejected, not accepted as a substring.", "Faint", 8))

        box.addWidget(Placard("Contacted this session"))
        self.activity = QVBoxLayout()
        self.activity.setSpacing(3)
        box.addLayout(self.activity)

        box.addWidget(Placard("Other connections this application can make"))
        box.addWidget(caption(self.LOCAL_ENDPOINT_NOTE, "Muted", 8.5))

        box.addWidget(Placard("Bundled offline sources — never call out"))
        for line in net.OFFLINE_SOURCES:
            box.addWidget(caption("· " + line, "Faint", 8))

        lay.addWidget(card)
        self.refresh_activity()

    def refresh_activity(self) -> None:
        """Per-source last fetch. In memory and per session, as it says."""
        while self.activity.count():
            item = self.activity.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Unparent before deleting: `deleteLater` leaves the old
                # label painted until the event loop runs, which drew this
                # section's line twice, in two different places.
                widget.setParent(None)
                widget.deleteLater()
        rows = net.ACTIVITY.rows()
        if not rows:
            self.activity.addWidget(caption(
                "No outbound request has been made this session.",
                "Muted", 8.5))
            return
        for row in rows:
            # Most source names already name their host; repeating it reads
            # as a stutter rather than as extra information.
            where = "" if not row.host or row.host in row.source \
                else f" ({row.host})"
            self.activity.addWidget(caption(
                f"{row.source}{where} — {row.line()}", "Muted", 8.5))


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
        self.online = OnlineSection(
            bool(getattr(ctx, "online_enabled", False)), self.theme_name)
        self.online.toggled.connect(self._set_online)
        bl.addWidget(self.online)
        for name, icon, detail in self.SECTIONS:
            bl.addWidget(self._section(name, icon, detail))
        bl.addStretch(1)
        lay.addWidget(scroll_host(body), 1)

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

    def _set_online(self, enabled: bool) -> None:
        """Flip the master switch and let the shell repaint the rail and badge.

        The setting is the only thing written here. Nothing is fetched as a
        side effect of switching on — the Ops screen decides when to call out.
        """
        con = getattr(self.ctx, "con", None)
        if con is None:
            return
        store.set_setting(con, "online_enabled", "1" if enabled else "0")
        self.ctx.online_enabled = enabled
        window = getattr(self.ctx, "window", None)
        if window is not None:
            window.apply_context()

    def on_shown(self) -> None:
        self.online.refresh_activity()
        if self.ctx and self.ctx.chain_ok is not None:
            ok, rows = self.ctx.chain_ok, self.ctx.chain_rows
            self.chain_state.setText(
                f"Audit chain verified on startup: {'intact' if ok else 'BROKEN'} "
                f"over {rows} rows.")
