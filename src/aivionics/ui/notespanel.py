"""Engineer notes, rendered inside the object they belong to (PLAN 4C).

Deliberately **not** a rail item. A note is anchored to a tail, a defect, a task
or a case, and it is only meaningful next to that thing; a global notes screen
would turn it into a second inbox.

The promotion action is the point of the whole feature. `defect_finding` is
what separates this product from a record of box swaps, and nothing else in the
plan captures it: SDR does not contain it and the shop never returns it. An
engineer typing *"found chafed wire at the connector behind P6-4, not the LRU"*
against a defect **is** the finding — one click turns that note into a
structured row without retyping it.

Three rules ride along, all enforced in `notes.store` rather than here:
private by default, never joined into any aggregate, and `tool_assisted`
inherited from the object the note is anchored to.
"""
from __future__ import annotations

import sqlite3
from datetime import date

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from . import theme as T
from .widgets import Tag, ui_font
from ..notes import ics, store

FINDING_LABELS = {
    "confirmed_fault": "Confirmed fault — the defect was real and located",
    "no_fault_found": "No fault found — nothing was located",
    "not_recorded": "Not recorded",
}


class NoteCard(QFrame):
    """One note. Shows what it is anchored to only when the panel is global."""

    promote = Signal(int)
    share = Signal(int, bool)
    remove = Signal(int)

    def __init__(self, note: store.Note, theme: str, *, show_anchor: bool = False,
                 can_promote: bool = False, parent=None):
        super().__init__(parent)
        self.note = note
        self.theme_name = theme
        self.setObjectName("Card")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 11)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(7)
        when = QLabel(note.created_at[:16].replace("T", " "))
        when.setObjectName("Faint")
        when.setFont(ui_font(8.5))
        head.addWidget(when)
        if show_anchor:
            head.addWidget(Tag(f"{note.anchor_type} {note.anchor_id}", theme))
        if note.due_date:
            overdue = store.is_due(note, date.today())
            tag = Tag(("due " if not overdue else "DUE ") + note.due_date, theme)
            head.addWidget(tag)
        if note.tool_assisted:
            # Standing rule 7: this cannot be retrofitted, so it is visible.
            head.addWidget(Tag("tool-assisted", theme))
        head.addWidget(Tag("shared" if note.shared else "private", theme))
        head.addStretch(1)
        lay.addLayout(head)

        body = QLabel(note.body)
        body.setWordWrap(True)
        body.setFont(ui_font(9.5))
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(body)

        actions = QHBoxLayout()
        actions.setSpacing(7)
        actions.addStretch(1)
        if can_promote:
            btn = QPushButton("Promote to finding…")
            btn.setToolTip(
                "Record this as what was actually found. It becomes a "
                "structured finding on the defect — the one thing SDR and the "
                "shop never give back.")
            btn.clicked.connect(lambda: self.promote.emit(note.id))
            actions.addWidget(btn)
        share_btn = QPushButton("Unshare" if note.shared else "Share with team")
        share_btn.clicked.connect(lambda: self.share.emit(note.id, not note.shared))
        actions.addWidget(share_btn)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(lambda: self.remove.emit(note.id))
        actions.addWidget(del_btn)
        lay.addLayout(actions)


class NotesPanel(QWidget):
    """Notes for one anchor, or every note the viewer may see."""

    changed = Signal()

    def __init__(self, ctx, anchor_type: str | None = None,
                 anchor_id: str | None = None, *, show_anchor: bool = False,
                 parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.theme_name = getattr(ctx, "theme_name", T.DEFAULT_THEME)
        self.anchor_type = anchor_type
        self.anchor_id = anchor_id
        self.show_anchor = show_anchor
        self.cards: list[NoteCard] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._composer())

        self.list_host = QWidget()
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(11, 10, 11, 10)
        self.list_lay.setSpacing(8)
        self.list_lay.addStretch(1)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setWidget(self.list_host)
        lay.addWidget(area, 1)
        lay.addWidget(self._footer())

    # ── composer ──────────────────────────────────────────────────────
    def _composer(self) -> QWidget:
        band = QFrame()
        band.setObjectName("Band")
        lay = QVBoxLayout(band)
        lay.setContentsMargins(11, 9, 11, 10)
        lay.setSpacing(7)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "What did you find? e.g. found chafed wire at the connector "
            "behind P6-4, not the LRU")
        self.editor.setFixedHeight(66)
        self.editor.setFont(ui_font(9.5))
        lay.addWidget(self.editor)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.due = QComboBox()
        self.due.addItem("No due date", None)
        for label, days in (("Due today", 0), ("Due tomorrow", 1),
                            ("Due in 3 days", 3), ("Due in a week", 7)):
            self.due.addItem(label, days)
        row.addWidget(self.due)
        row.addStretch(1)
        self.add_btn = QPushButton("Add note")
        self.add_btn.setObjectName("Primary")
        self.add_btn.clicked.connect(self._add)
        row.addWidget(self.add_btn)
        lay.addLayout(row)
        return band

    def _footer(self) -> QWidget:
        foot = QFrame()
        foot.setObjectName("PageHeader")
        lay = QHBoxLayout(foot)
        lay.setContentsMargins(11, 6, 11, 7)
        self.status = QLabel("")
        self.status.setObjectName("Faint")
        self.status.setFont(ui_font(8))
        self.status.setWordWrap(True)
        lay.addWidget(self.status, 1)
        self.ics_btn = QPushButton("Export due notes (.ics)")
        self.ics_btn.setToolTip(
            "The calendar owns the alerting. This application never fires a "
            "reminder: it would have to be running, it lives on one machine, "
            "and a missed in-app reminder manufactures the same false "
            "confidence as a shadow compliance clock — with no source system "
            "to stamp it.")
        self.ics_btn.clicked.connect(self._export)
        lay.addWidget(self.ics_btn)
        return foot

    # ── data ──────────────────────────────────────────────────────────
    def _con(self) -> sqlite3.Connection | None:
        return getattr(self.ctx, "con", None)

    def _user_id(self) -> int | None:
        user = getattr(self.ctx, "user", None)
        return getattr(user, "id", None)

    def set_anchor(self, anchor_type: str | None, anchor_id: str | None) -> None:
        self.anchor_type, self.anchor_id = anchor_type, anchor_id
        self.reload()

    def reload(self) -> None:
        con, viewer = self._con(), self._user_id()
        for card in self.cards:
            card.setParent(None)
        self.cards.clear()
        self.notes: list[store.Note] = []
        anchored = bool(self.anchor_type and self.anchor_id)
        self.editor.setEnabled(anchored)
        self.add_btn.setEnabled(anchored)

        if con is None or viewer is None:
            self.status.setText("sign in to keep notes")
            return
        try:
            if anchored:
                self.notes = store.for_anchor(
                    con, self.anchor_type, self.anchor_id, viewer_id=viewer)
            else:
                self.notes = store.list_notes(con, viewer_id=viewer)
        except sqlite3.Error as exc:
            self.status.setText(f"notes unavailable — {exc}")
            return

        can_promote = self.anchor_type == "defect"
        for note in self.notes:
            card = NoteCard(note, self.theme_name,
                            show_anchor=self.show_anchor or not anchored,
                            can_promote=can_promote or note.anchor_type == "defect")
            card.promote.connect(self._promote)
            card.share.connect(self._share)
            card.remove.connect(self._delete)
            self.list_lay.insertWidget(self.list_lay.count() - 1, card)
            self.cards.append(card)

        due = sum(1 for n in self.notes if store.is_due(n, date.today()))
        self.status.setText(
            f"{len(self.notes)} note(s) · {due} due · private by default · "
            f"never counted in any statistic"
            if self.notes else
            ("no notes on this object yet" if anchored else "no notes yet"))

    # ── actions ───────────────────────────────────────────────────────
    def _add(self) -> None:
        con, viewer = self._con(), self._user_id()
        body = self.editor.toPlainText().strip()
        if not (con and viewer and body and self.anchor_type and self.anchor_id):
            return
        days = self.due.currentData()
        due = None
        if days is not None:
            from datetime import timedelta
            due = (date.today() + timedelta(days=days)).isoformat()
        try:
            store.create(con, author_id=viewer, anchor_type=self.anchor_type,
                         anchor_id=self.anchor_id, body=body, due_date=due)
        except (store.AnchorRequired, ValueError) as exc:
            QMessageBox.warning(self, "Note not saved", str(exc))
            return
        self.editor.clear()
        self.due.setCurrentIndex(0)
        self.reload()
        self.changed.emit()

    def _share(self, note_id: int, shared: bool) -> None:
        con, viewer = self._con(), self._user_id()
        if con is None or viewer is None:
            return
        try:
            store.set_shared(con, note_id, author_id=viewer, shared=shared)
        except (store.NoteNotFound, PermissionError, ValueError) as exc:
            QMessageBox.warning(self, "Not changed", str(exc))
            return
        self.reload()

    def _delete(self, note_id: int) -> None:
        con, viewer = self._con(), self._user_id()
        if con is None or viewer is None:
            return
        if QMessageBox.question(self, "Delete note",
                                "Delete this note? It cannot be recovered."
                                ) != QMessageBox.StandardButton.Yes:
            return
        try:
            store.delete(con, note_id, author_id=viewer)
        except (store.NoteNotFound, PermissionError) as exc:
            QMessageBox.warning(self, "Not deleted", str(exc))
            return
        self.reload()
        self.changed.emit()

    def _promote(self, note_id: int) -> None:
        con, viewer = self._con(), self._user_id()
        if con is None:
            return
        from PySide6.QtWidgets import QInputDialog
        labels = [FINDING_LABELS[k] for k in store.FINDING_TYPES]
        choice, ok = QInputDialog.getItem(
            self, "Promote to finding",
            "Record this note as what was actually found on the defect.\n"
            "It becomes a structured finding — the one thing SDR and the "
            "shop never give back.",
            labels, 0, False)
        if not ok:
            return
        finding_type = store.FINDING_TYPES[labels.index(choice)]
        try:
            store.promote_to_finding(con, note_id, finding_type=finding_type,
                                     user_id=viewer)
        except store.PromotionNotAllowed as exc:
            QMessageBox.warning(self, "Not promoted", str(exc))
            return
        QMessageBox.information(
            self, "Recorded",
            "The finding is recorded on the defect. The note stays where it "
            "is, so the free text remains readable beside the structured row "
            "it produced.")
        self.reload()
        self.changed.emit()

    def _export(self) -> None:
        due = [n for n in getattr(self, "notes", []) if n.due_date]
        if not due:
            QMessageBox.information(
                self, "Nothing to export",
                "Only notes with a due date go to the calendar.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export due notes", "aivionics-notes.ics",
            "Calendar (*.ics)")
        if not path:
            return
        count = ics.write_calendar(path, due)
        QMessageBox.information(
            self, "Exported",
            f"{count} note(s) written to {path}.\n\n"
            "Your calendar owns the reminder from here — this application "
            "never fires an alarm.")
