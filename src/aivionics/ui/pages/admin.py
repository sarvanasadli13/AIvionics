"""Admin — the IT and configuration screen (PLAN 4.9, Phase 6).

Replaces six cards that honestly said NOT BUILT. Each is now the smallest
thing that actually does its job:

* **Fleet** is the one with teeth. Year built and line number are what the
  Reliability screen stratifies on (PLAN 3.7), and an empty register means
  no airframe-standard split and no tails on the Ops map. It is edited here
  rather than on the Fleet screen so a correction is a deliberate act by
  someone with the Admin role, not something done in passing.
* **Models and index** exists to make standing rule 9 visible: changing the
  embedding model invalidates every stored vector and every measurement taken
  with it, so the version set is shown whole and a mismatch reads as a
  mismatch rather than being inferred.
* **Backups** run `VACUUM INTO` and verify the copy, because a backup nobody
  has opened is a belief rather than a backup.
* **Audit** verifies the hash chain here as well as at startup — a broken
  chain means rows were altered outside the application.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFormLayout,
                               QFrame, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from .. import store
from .. import theme as T
from ..widgets import (ProvenanceLine, SectionHeader, StatusBadge, mono_font,
                       ui_font)
from .base import Page, caption, scroll_host
from .stubs import OnlineSection

from ... import config
from ...admin import maintenance

FLEET_COLUMNS = ["Tail", "Type", "MSN", "Line no.", "Built", "Hours", "Cycles"]


class AdminPage(Page):
    title = "Admin"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(SectionHeader("Admin", "IT and configuration"))

        self.tabs = QTabWidget()
        self.tabs.addTab(scroll_host(self._system_tab()), "System")
        self.tabs.addTab(self._fleet_tab(), "Fleet register")
        self.tabs.addTab(self._audit_tab(), "Audit")
        lay.addWidget(self.tabs, 1)

        self.footer = ProvenanceLine("")
        self.footer.setContentsMargins(15, 6, 15, 12)
        lay.addWidget(self.footer)

    # ── helpers ───────────────────────────────────────────────────────
    def _con(self) -> sqlite3.Connection | None:
        return getattr(self.ctx, "con", None)

    def _card(self, title: str, subtitle: str = "") -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("Card")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(13, 11, 13, 12)
        inner.setSpacing(7)
        head = QLabel(title)
        head.setFont(ui_font(10, QFont.Weight.DemiBold))
        inner.addWidget(head)
        if subtitle:
            inner.addWidget(caption(subtitle, "Muted", 8.5))
        return card, inner

    # ── system tab ────────────────────────────────────────────────────
    def _system_tab(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(15, 13, 15, 13)
        lay.setSpacing(10)

        self.online = OnlineSection(
            bool(getattr(self.ctx, "online_enabled", False)), self.theme_name)
        self.online.toggled.connect(self._set_online)
        lay.addWidget(self.online)

        # versions — standing rule 9 made visible
        card, inner = self._card(
            "Models and index",
            "Changing the embedding model invalidates every stored vector and "
            "every measurement taken with it, so a re-index is forced. The "
            "whole version set is shown together; a mismatch should read as a "
            "mismatch, not be inferred.")
        self.versions = QLabel("—")
        self.versions.setFont(mono_font(9, QFont.Weight.Normal))
        self.versions.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        inner.addWidget(self.versions)
        lay.addWidget(card)

        # corpus + coverage
        card, inner = self._card(
            "Corpus and coverage",
            "Extracted tasks against each chapter's own table of contents. A "
            "chapter whose contents page did not survive shows 'not measured' "
            "rather than a number that would read as absence.")
        self.corpus = QLabel("—")
        self.corpus.setFont(ui_font(9))
        self.corpus.setWordWrap(True)
        inner.addWidget(self.corpus)
        row = QHBoxLayout()
        row.addStretch(1)
        hint = caption("Ingest is a command-line step: scripts/phase1.py, "
                       "phase0.py, phase2_index.py, phase3.py", "Faint", 8.5)
        row.addWidget(hint)
        inner.addLayout(row)
        lay.addWidget(card)

        # backups
        card, inner = self._card(
            "Backups",
            "VACUUM INTO, never a file copy: in WAL mode the database is a "
            "file plus a log, and copying the file alone captures a torn "
            "state that restores as corruption. Every backup is opened and "
            "row-counted against the source before it is called successful.")
        self.backup_state = caption("no backup taken this session", "Muted", 9)
        inner.addWidget(self.backup_state)
        row = QHBoxLayout()
        row.addStretch(1)
        btn = QPushButton("Back up now")
        btn.setObjectName("Primary")
        btn.clicked.connect(self._backup)
        row.addWidget(btn)
        restore = QPushButton("Restore…")
        restore.clicked.connect(self._restore)
        row.addWidget(restore)
        inner.addLayout(row)
        lay.addWidget(card)

        # users
        card, inner = self._card(
            "Users and roles",
            "Roles are rows, not a flag on the account. Statistics are "
            "aggregate-only regardless of role — no view attributes an "
            "outcome to a named engineer.")
        self.users = QLabel("—")
        self.users.setFont(ui_font(9))
        inner.addWidget(self.users)
        lay.addWidget(card)

        lay.addStretch(1)
        return host

    # ── fleet tab ─────────────────────────────────────────────────────
    def _fleet_tab(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        band = QFrame()
        band.setObjectName("Band")
        form = QHBoxLayout(band)
        form.setContentsMargins(13, 10, 13, 10)
        form.setSpacing(8)
        self.f_tail = QLineEdit(); self.f_tail.setPlaceholderText("Tail *")
        self.f_type = QLineEdit(); self.f_type.setPlaceholderText("Type *")
        self.f_msn = QLineEdit(); self.f_msn.setPlaceholderText("MSN")
        self.f_line = QLineEdit(); self.f_line.setPlaceholderText("Line no.")
        self.f_icao = QLineEdit(); self.f_icao.setPlaceholderText("ICAO24 (map)")
        for w, width in ((self.f_tail, 96), (self.f_type, 104), (self.f_msn, 84),
                         (self.f_line, 84), (self.f_icao, 104)):
            w.setFixedWidth(width)
            w.setMinimumHeight(30)
            form.addWidget(w)
        self.f_year = QSpinBox()
        self.f_year.setRange(0, 2100)
        self.f_year.setSpecialValueText("year —")
        self.f_year.setFixedWidth(88)
        self.f_year.setMinimumHeight(30)
        form.addWidget(self.f_year)
        add = QPushButton("Add / update")
        add.setObjectName("Primary")
        add.clicked.connect(self._save_aircraft)
        form.addWidget(add)
        form.addStretch(1)
        lay.addWidget(band)

        lay.addWidget(caption(
            "Year built and line number drive airframe-standard stratification "
            "on the Reliability screen, and ICAO24 is what puts a tail on the "
            "Ops map. A tail with no year recorded is grouped as 'year "
            "unknown' rather than assumed modern.", "Muted", 8.5))

        self.fleet_table = QTableWidget(0, len(FLEET_COLUMNS))
        self.fleet_table.setHorizontalHeaderLabels(FLEET_COLUMNS)
        self.fleet_table.verticalHeader().setVisible(False)
        self.fleet_table.verticalHeader().setDefaultSectionSize(T.ROW_HEIGHT)
        self.fleet_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.fleet_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.fleet_table.setShowGrid(False)
        self.fleet_table.setAlternatingRowColors(True)
        header = self.fleet_table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
        for i in range(len(FLEET_COLUMNS)):
            header.setSectionResizeMode(
                i, QHeaderView.ResizeMode.Stretch if i == 1
                else QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self.fleet_table, 1)
        return host

    # ── audit tab ─────────────────────────────────────────────────────
    def _audit_tab(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        band = QFrame()
        band.setObjectName("Band")
        row = QHBoxLayout(band)
        row.setContentsMargins(13, 10, 13, 10)
        self.chain_badge = StatusBadge("unknown", "NOT VERIFIED", self.theme_name)
        row.addWidget(self.chain_badge)
        self.chain_text = caption("", "Muted", 9)
        row.addWidget(self.chain_text, 1)
        verify = QPushButton("Verify chain")
        verify.clicked.connect(self._verify_chain)
        row.addWidget(verify)
        lay.addWidget(band)

        self.audit_table = QTableWidget(0, 5)
        self.audit_table.setHorizontalHeaderLabels(
            ["When", "Action", "Entity", "Id", "Row hash"])
        self.audit_table.verticalHeader().setVisible(False)
        self.audit_table.verticalHeader().setDefaultSectionSize(T.ROW_HEIGHT)
        self.audit_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.audit_table.setShowGrid(False)
        self.audit_table.setAlternatingRowColors(True)
        h = self.audit_table.horizontalHeader()
        h.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                              | Qt.AlignmentFlag.AlignVCenter)
        for i in range(5):
            h.setSectionResizeMode(
                i, QHeaderView.ResizeMode.Stretch if i == 1
                else QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self.audit_table, 1)
        lay.addWidget(ProvenanceLine(
            "Every row carries the hash of its predecessor. A break means rows "
            "were altered outside the application — the log cannot be quietly "
            "rewritten, only visibly broken. Aggregate only: no view here "
            "attributes an outcome to a named engineer."))
        return host

    # ── data ──────────────────────────────────────────────────────────
    def on_shown(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        con = self._con()
        self.versions.setText(
            "\n".join(maintenance.versions(con).lines()) if con is not None
            else "no database")
        self._load_corpus(con)
        self._load_users(con)
        self._load_fleet(con)
        self._load_audit(con)
        self.footer.setText(
            f"Database {config.DB_PATH} · schema {maintenance.SCHEMA_VERSION} "
            f"· index {config.INDEX_VERSION}")

    def _load_corpus(self, con) -> None:
        if con is None:
            self.corpus.setText("no database")
            return
        try:
            tasks = con.execute("SELECT COUNT(*) FROM task").fetchone()[0]
            bodies = con.execute(
                "SELECT COUNT(*) FROM task WHERE body IS NOT NULL").fetchone()[0]
            rows = con.execute(
                "SELECT ata_chapter, pct FROM coverage ORDER BY ata_chapter"
            ).fetchall()
        except sqlite3.Error as exc:
            self.corpus.setText(f"unreadable — {exc}")
            return
        measured = [f"{ch} {pct:.0f}%" for ch, pct in rows if pct is not None]
        unmeasured = [ch for ch, pct in rows if pct is None]
        text = (f"{tasks:,} tasks · {bodies:,} with a body · "
                f"{tasks - bodies:,} locator-only\n"
                f"Coverage: {', '.join(measured) if measured else 'none measured'}")
        if unmeasured:
            text += (f"\nNot measured (contents page did not survive): "
                     f"{', '.join(unmeasured)}")
        self.corpus.setText(text)

    def _load_users(self, con) -> None:
        if con is None:
            self.users.setText("no database")
            return
        try:
            rows = con.execute(
                "SELECT u.username, r.name, u.active FROM app_user u"
                " JOIN role r ON r.id=u.role_id ORDER BY u.username").fetchall()
        except sqlite3.Error as exc:
            self.users.setText(f"unreadable — {exc}")
            return
        self.users.setText(
            "\n".join(f"{u} · {r}" + ("" if a else " · disabled")
                      for u, r, a in rows) or "no accounts")

    def _load_fleet(self, con) -> None:
        if con is None:
            return
        try:
            rows = con.execute(
                "SELECT tail,type,msn,line_number,year_built,total_time_hrs,"
                "total_cycles FROM aircraft ORDER BY tail").fetchall()
        except sqlite3.Error:
            rows = []
        self.fleet_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for c, value in enumerate(row):
                item = QTableWidgetItem("—" if value in (None, "") else str(value))
                if c == 0:
                    item.setFont(mono_font(10))
                self.fleet_table.setItem(i, c, item)

    def _load_audit(self, con, limit: int = 300) -> None:
        if con is None:
            return
        try:
            rows = con.execute(
                "SELECT ts, action, entity, entity_id, row_hash FROM audit_log"
                " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        except sqlite3.Error:
            rows = []
        self.audit_table.setRowCount(len(rows))
        for i, (ts, action, entity, entity_id, row_hash) in enumerate(rows):
            cells = [ts[:19].replace("T", " ") if ts else "—", action or "—",
                     entity or "—", entity_id or "—", (row_hash or "")[:16]]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c in (0, 4):
                    item.setFont(mono_font(9, QFont.Weight.Normal))
                self.audit_table.setItem(i, c, item)

    # ── actions ───────────────────────────────────────────────────────
    def _set_online(self, enabled: bool) -> None:
        """Flip the master switch. Nothing is fetched as a side effect."""
        con = self._con()
        if con is None:
            return
        store.set_setting(con, "online_enabled", "1" if enabled else "0")
        self.ctx.online_enabled = enabled
        window = getattr(self.ctx, "window", None)
        if window is not None:
            window.apply_context()

    def _save_aircraft(self) -> None:
        con = self._con()
        tail = self.f_tail.text().strip().upper()
        actype = self.f_type.text().strip()
        if con is None or not tail or not actype:
            QMessageBox.warning(self, "Not saved",
                                "A tail and a type are required.")
            return
        year = self.f_year.value() or None
        try:
            con.execute(
                "INSERT INTO aircraft(tail,type,msn,line_number,year_built)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(tail) DO UPDATE SET type=excluded.type,"
                " msn=excluded.msn, line_number=excluded.line_number,"
                " year_built=excluded.year_built",
                (tail, actype, self.f_msn.text().strip() or None,
                 self.f_line.text().strip() or None, year))
            icao = self.f_icao.text().strip().lower()
            if icao:
                cols = {r[1] for r in con.execute("PRAGMA table_info(aircraft)")}
                if "icao24" in cols:
                    con.execute("UPDATE aircraft SET icao24=? WHERE tail=?",
                                (icao, tail))
            con.commit()
        except sqlite3.Error as exc:
            QMessageBox.warning(self, "Not saved", str(exc))
            return
        from ... import audit
        audit.log(con, "fleet_upsert", user_id=getattr(self.ctx.user, "id", None),
                  entity="aircraft", entity_id=tail)
        for field in (self.f_tail, self.f_type, self.f_msn, self.f_line,
                      self.f_icao):
            field.clear()
        self.f_year.setValue(0)
        self.refresh()

    def _backup(self) -> None:
        con = self._con()
        if con is None:
            return
        target = Path(config.DATA_DIR) / "backups" / maintenance.default_backup_name()
        self.backup_state.setText("backing up…")
        result = maintenance.backup(con, target)
        self.backup_state.setText(result.summary())
        if not result.ok:
            QMessageBox.warning(self, "Backup not verified", result.summary())

    def _restore(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Restore a verified backup",
            str(Path(config.DATA_DIR) / "backups"), "Database (*.db)")
        if not path:
            return
        QMessageBox.information(
            self, "Restore is a command-line step",
            "A restore replaces the live database, so it is deliberately not "
            "done from inside the running application — the file would be "
            "swapped underneath open connections.\n\n"
            "Close AIvionics and run:\n\n"
            f"    python scripts\\backup.py --restore \"{path}\" --force\n\n"
            "The backup is integrity-checked before anything is replaced.")

    def _verify_chain(self) -> None:
        con = self._con()
        if con is None:
            return
        from ... import audit
        ok, rows = audit.verify_chain(con)
        self.chain_badge.kind = "ok" if ok else "alert"
        self.chain_badge.override = "VERIFIED" if ok else "BROKEN"
        self.chain_badge.refresh_theme(self.theme_name)
        self.chain_text.setText(
            f"{rows:,} rows verified — every row's hash matches its predecessor"
            if ok else
            f"chain breaks after row {rows:,}: rows were altered outside the "
            f"application")
        self._load_audit(con)
