"""The gold-review questionnaire, as an embeddable panel.

One implementation, two hosts: the `AI Validation` page inside the main
window, and the standalone `AdjudicatorWindow` kept for development and
recovery. Everything below is a plain `QWidget` with no window chrome of its
own, so embedding it costs nothing and the two hosts cannot drift apart.

Nothing in this file decides anything. Every rule about what may be stored
lives in `aivionics.goldreview`; the widgets ask it and render the answer, so
a disabled button and a refused write can never disagree.
"""
from __future__ import annotations

import sqlite3

import qtawesome as qta
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import goldreview as G
from . import theme as T
from .widgets import (AtaLocator, EmptyState, Placard, SectionHeader,
                      StatusBadge, Tag, mono_font, ui_font)

# Order is the reading order and the keyboard order.
VERDICT_ORDER = [
    ("yes", "Yes", "Y", "ok"),
    ("no", "No", "N", "alert"),
    ("partial", "Partial", "P", "warn"),
    ("unsure", "Unsure", "U", "unknown"),
]

THE_QUESTION = (
    "For this reported defect, is the cited task a technically appropriate "
    "troubleshooting or maintenance entry point?")

PREAMBLE = (
    "These 400 held-out cases are the safety reference used to test whether "
    "AIvionics retrieves an appropriate ATA task. Your answers do not train "
    "the AI directly. Review the reported defect and the cited manual task. "
    "Do not guess — Unsure is a valid and important answer.")

PROTOCOL = [
    "Judge the candidate against the reported defect.",
    "Use the exact manual evidence shown.",
    "Do not judge based on whether the wording sounds plausible.",
    "Do not use another AI system.",
    "Mark Unsure when the evidence or your qualification is insufficient.",
    "Retrieval scores and ranks are deliberately not shown.",
]

FREEZE_WARNING = (
    "Finalizing freezes this version as held-out evaluation data. It must not "
    "be used for model training, prompt tuning, threshold fitting or reranker "
    "selection.")

SESSION_CHOICES = [("No limit", 0), ("10 cases", 10), ("20 cases", 20),
                   ("25 cases", 25)]

_TEXT_ENTRY = (QLineEdit, QPlainTextEdit)


def _is_typing() -> bool:
    """True when a shortcut key would be a character the reviewer meant to type.

    A verdict key that fires while somebody is writing a note is not a
    shortcut, it is data loss.
    """
    from PySide6.QtWidgets import QApplication
    w = QApplication.focusWidget()
    return isinstance(w, _TEXT_ENTRY) and w.isEnabled() and not w.isReadOnly()


def _scroll(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setWidget(inner)
    return area


class TaskPickerDialog(QDialog):
    """Pick the task the reviewer believes is correct — by database search only.

    There is deliberately no ranking, no embedding and no assistant here. The
    correction is evidence about the right answer; proposing it with the
    engine under test would fold that engine into its own reference set.
    """

    def __init__(self, service: G.GoldReviewService, theme: str,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.chosen: str | None = None
        self.setWindowTitle("Select the correct task")
        self.resize(880, 560)
        self.setMinimumSize(640, 420)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        lay.addWidget(SectionHeader(
            "Find the correct task", "database search — no AI, no ranking"))

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        self.f_number = QLineEdit()
        self.f_number.setPlaceholderText("task number, e.g. 34-11-00")
        self.f_number.setFont(mono_font(10, QFont.Weight.Normal))
        self.f_title = QLineEdit()
        self.f_title.setPlaceholderText("words in the title")
        self.f_chapter = QComboBox()
        self.f_chapter.addItem("Any ATA chapter", "")
        for ch in service.chapters():
            self.f_chapter.addItem(f"ATA {ch}", ch)
        self.f_manual = QComboBox()
        self.f_manual.addItem("Any manual", "")
        for mt in service.manual_types():
            self.f_manual.addItem(mt, mt)
        for col, (cap, widget) in enumerate((
                ("Task number", self.f_number), ("Title", self.f_title),
                ("Chapter", self.f_chapter), ("Manual", self.f_manual))):
            form.addWidget(Placard(cap), 0, col)
            form.addWidget(widget, 1, col)
        lay.addLayout(form)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Task number", "Title", "ATA", "Manual", "Rev", "Procedure"])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(0, 190)
        self.table.setColumnWidth(1, 330)
        self.table.doubleClicked.connect(self._accept_selected)
        lay.addWidget(self.table, 1)

        self.count = QLabel("")
        self.count.setObjectName("Faint")
        self.count.setFont(ui_font(8.5))
        lay.addWidget(self.count)

        row = QHBoxLayout()
        row.setSpacing(9)
        self.not_found = QPushButton("Not in this corpus")
        self.not_found.setToolTip(
            "Record that the correct task cannot be identified from the "
            "available corpus")
        self.not_found.clicked.connect(self._accept_not_found)
        row.addWidget(self.not_found)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self.ok = QPushButton("Use this task")
        self.ok.setObjectName("Primary")
        self.ok.setEnabled(False)
        self.ok.clicked.connect(self._accept_selected)
        row.addWidget(self.ok)
        lay.addLayout(row)

        for w in (self.f_number, self.f_title):
            w.textChanged.connect(self.search)
        for w in (self.f_chapter, self.f_manual):
            w.currentIndexChanged.connect(self.search)
        self.table.itemSelectionChanged.connect(
            lambda: self.ok.setEnabled(bool(self.table.selectedItems())))
        self.search()

    def search(self) -> None:
        rows = self.service.search_tasks(
            number=self.f_number.text(), title=self.f_title.text(),
            chapter=self.f_chapter.currentData() or "",
            manual_type=self.f_manual.currentData() or "")
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            cells = [r["task_number"], r["title"] or "—", r["ata_chapter"] or "—",
                     r["manual_type"] or "—", r["revision"] or "—",
                     "catalogue only" if r["catalogue_only"] else "held"]
            for j, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if j == 0:
                    item.setFont(mono_font(9.5, QFont.Weight.Normal))
                self.table.setItem(i, j, item)
        self.count.setText(
            f"{len(rows)} matching task{'s' if len(rows) != 1 else ''}"
            + ("  ·  refine the search to see fewer" if len(rows) >= 200 else ""))
        self.ok.setEnabled(False)

    def _accept_selected(self) -> None:
        items = self.table.selectedItems()
        if not items:
            return
        self.chosen = self.table.item(items[0].row(), 0).text()
        self.accept()

    def _accept_not_found(self) -> None:
        self.chosen = None
        self.done(QDialog.DialogCode.Accepted + 1)   # distinct outcome


class VerdictBar(QWidget):
    """The four responses. Checkable, never preselected, never colour alone."""

    chosen = Signal(str)

    def __init__(self, theme: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme_name = theme
        self.buttons: dict[str, QPushButton] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(9)
        for verdict, label, key, _status in VERDICT_ORDER:
            btn = QPushButton(f"{key}   {label}")
            btn.setCheckable(True)
            btn.setAutoExclusive(False)
            btn.setMinimumHeight(46)
            btn.setObjectName("VerdictBtn")
            btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Fixed)
            # Text, border and checked state all carry the selection; a
            # screen reader gets the full sentence, not the initial.
            btn.setAccessibleName(f"{label} — {G.VERDICT_LABELS[verdict]}")
            btn.setAccessibleDescription(G.VERDICT_LABELS[verdict])
            btn.setToolTip(G.VERDICT_LABELS[verdict])
            btn.clicked.connect(lambda _=False, v=verdict: self.select(v))
            lay.addWidget(btn, 1)
            self.buttons[verdict] = btn

    def select(self, verdict: str | None, *, emit: bool = True) -> None:
        """Set the selection. `emit=False` restores a saved answer silently.

        Restoring a stored verdict is not a reviewer action, so it must not
        travel the same path as a click — that is what made merely opening an
        answered case mark the form as edited.
        """
        for name, btn in self.buttons.items():
            btn.setChecked(name == verdict)
        if verdict and emit:
            self.chosen.emit(verdict)

    def current(self) -> str | None:
        for name, btn in self.buttons.items():
            if btn.isChecked():
                return name
        return None

    def refresh_theme(self, theme: str) -> None:
        self.theme_name = theme
        pal = T.THEMES[theme]
        for btn in self.buttons.values():
            btn.setStyleSheet(
                f"QPushButton#VerdictBtn{{border:1px solid {pal['line']};"
                f"border-radius:4px;background:{pal['s1']};color:{pal['txt']};"
                f"font-weight:600;padding:6px 10px;text-align:left;}}"
                f"QPushButton#VerdictBtn:hover{{border-color:{pal['cy']};}}"
                f"QPushButton#VerdictBtn:focus{{border:2px solid {pal['cy']};}}"
                f"QPushButton#VerdictBtn:checked{{border:2px solid {pal['cy']};"
                f"background:{pal['cyq']};color:{pal['cyl']};}}")


class GoldReviewPanel(QWidget):
    """Dashboard, questionnaire and completion, with no window chrome.

    The host supplies the connection and the authenticated user; this widget
    never invents either.
    """

    DASHBOARD, QUESTION, DONE, BLOCKED = 0, 1, 2, 3

    progress_changed = Signal()

    def __init__(self, con: sqlite3.Connection, user=None,
                 theme: str = T.DEFAULT_THEME, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme_name = theme
        self.service = G.GoldReviewService(con, user)
        self.seq: int | None = None
        self.pair: G.ReviewPair | None = None
        self.session_target = 0
        self.session_done = 0
        # The form state as loaded. Dirtiness is `current != this`, so every
        # field is covered automatically and no widget has to remember to
        # announce itself.
        self._loaded_state: tuple | None = None
        self._editing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.stack.addWidget(self._dashboard())
        self.stack.addWidget(self._questionnaire())
        self.stack.addWidget(self._completion())
        self.stack.addWidget(self._blocked())
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.refresh_theme(theme)

    # ── dashboard ─────────────────────────────────────────────────────
    def _dashboard(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(22, 18, 22, 20)
        lay.setSpacing(14)

        lay.addWidget(SectionHeader(
            "AI Validation — held-out gold set",
            "human reference for retrieval evaluation"))

        intro = QLabel(PREAMBLE)
        intro.setWordWrap(True)
        intro.setFont(ui_font(10.5))
        intro.setMaximumWidth(900)
        lay.addWidget(intro)

        self.tiles = QGridLayout()
        self.tiles.setHorizontalSpacing(10)
        self.tiles.setVerticalSpacing(8)
        lay.addLayout(self.tiles)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        lay.addWidget(self.bar)

        self.meta = QLabel("")
        self.meta.setObjectName("Faint")
        self.meta.setFont(ui_font(9, tabular=True))
        self.meta.setWordWrap(True)
        lay.addWidget(self.meta)

        proto = QFrame()
        proto.setObjectName("Card")
        pl = QVBoxLayout(proto)
        pl.setContentsMargins(15, 12, 15, 13)
        pl.setSpacing(5)
        pl.addWidget(Placard("Review protocol"))
        for line in PROTOCOL:
            item = QLabel(f"·  {line}")
            item.setWordWrap(True)
            item.setFont(ui_font(9.5))
            pl.addWidget(item)
        lay.addWidget(proto)

        self.chapter_wrap = QFrame()
        self.chapter_wrap.setObjectName("Card")
        cw = QVBoxLayout(self.chapter_wrap)
        cw.setContentsMargins(15, 12, 15, 13)
        cw.setSpacing(7)
        cw.addWidget(Placard("Completion by ATA chapter"))
        self.chapter_flow = QHBoxLayout()
        self.chapter_flow.setSpacing(6)
        chapter_scroll = QScrollArea()
        chapter_scroll.setWidgetResizable(True)
        chapter_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        chapter_scroll.setFixedHeight(46)
        chapter_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        chost = QWidget()
        chost.setLayout(self.chapter_flow)
        chapter_scroll.setWidget(chost)
        cw.addWidget(chapter_scroll)
        lay.addWidget(self.chapter_wrap)

        actions = QHBoxLayout()
        actions.setSpacing(9)
        actions.addWidget(Placard("Session"))
        self.session_combo = QComboBox()
        for label, n in SESSION_CHOICES:
            self.session_combo.addItem(label, n)
        self.session_combo.setToolTip(
            "Optional. A block size to work in — it does not limit anything, "
            "it just offers a break.")
        actions.addWidget(self.session_combo)
        actions.addStretch(1)
        self.review_btn = QPushButton("Review answered cases")
        self.review_btn.clicked.connect(lambda: self.open_filtered("completed"))
        actions.addWidget(self.review_btn)
        self.resume_btn = QPushButton("Resume next unanswered case")
        self.resume_btn.setObjectName("Primary")
        self.resume_btn.setMinimumHeight(38)
        self.resume_btn.clicked.connect(self.resume)
        actions.addWidget(self.resume_btn)
        lay.addLayout(actions)
        lay.addStretch(1)
        return _scroll(host)

    # ── questionnaire ─────────────────────────────────────────────────
    def _questionnaire(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # top strip: position, filter, navigation
        strip = QFrame()
        strip.setObjectName("Band")
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(15, 8, 15, 8)
        sl.setSpacing(9)
        self.position_label = QLabel("—")
        self.position_label.setFont(ui_font(10, QFont.Weight.DemiBold,
                                            tabular=True))
        sl.addWidget(self.position_label)
        self.state_badge = StatusBadge("unknown", "unanswered", self.theme_name)
        sl.addWidget(self.state_badge)
        self.session_label = QLabel("")
        self.session_label.setObjectName("Faint")
        self.session_label.setFont(ui_font(8.5, tabular=True))
        sl.addWidget(self.session_label)
        sl.addStretch(1)
        self.filter_combo = QComboBox()
        for label, key in (("All cases", "all"), ("Unanswered", "unanswered"),
                           ("Completed", "completed"), ("Yes", "yes"),
                           ("No", "no"), ("Partial", "partial"),
                           ("Unsure", "unsure")):
            self.filter_combo.addItem(label, key)
        self.filter_combo.currentIndexChanged.connect(self._filter_changed)
        sl.addWidget(self.filter_combo)
        self.jump = QLineEdit()
        self.jump.setPlaceholderText("go to case…")
        self.jump.setFixedWidth(110)
        self.jump.returnPressed.connect(self._jump)
        sl.addWidget(self.jump)
        for text, slot, tip in (("‹ Previous", self.previous, "Backspace"),
                                ("Next ›", self.next_case, ""),
                                ("Next unanswered", self.next_unanswered, "")):
            b = QPushButton(text)
            if tip:
                b.setToolTip(tip)
            b.clicked.connect(slot)
            sl.addWidget(b)
        back = QPushButton("Dashboard")
        back.clicked.connect(self.show_dashboard)
        sl.addWidget(back)
        lay.addWidget(strip)

        from .widgets import Splitter
        split = Splitter(Qt.Orientation.Horizontal, self.theme_name)
        split.addWidget(self._defect_side())
        split.addWidget(self._task_side())
        split.setSizes([460, 780])
        lay.addWidget(split, 1)

        lay.addWidget(self._answer_side())
        return host

    def _defect_side(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("Card")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(17, 14, 17, 14)
        lay.setSpacing(9)
        lay.addWidget(Placard("Reported defect — exactly what the engine was given"))

        self.defect_text = QLabel("")
        self.defect_text.setWordWrap(True)
        self.defect_text.setFont(ui_font(12))
        self.defect_text.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.defect_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 0, 0)
        il.addWidget(self.defect_text)
        il.addStretch(1)
        lay.addWidget(_scroll(inner), 1)

        self.defect_meta = QHBoxLayout()
        self.defect_meta.setSpacing(6)
        self.defect_meta.addStretch(1)
        lay.addLayout(self.defect_meta)

        why = QLabel(
            "Only the narrative, the reported ATA reference and the aircraft "
            "model are shown, because those are the only fields the retrieval "
            "evaluation conditions on. Sampling metadata, retrieval scores and "
            "ranks are withheld by design.")
        why.setObjectName("Faint")
        why.setWordWrap(True)
        why.setFont(ui_font(8.5))
        lay.addWidget(why)
        return panel

    def _task_side(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(17, 14, 17, 10)
        lay.setSpacing(9)
        lay.addWidget(Placard("Cited task"))
        self.locator = AtaLocator("—", self.theme_name)
        lay.addWidget(self.locator)
        self.task_title = QLabel("")
        self.task_title.setFont(ui_font(12, QFont.Weight.DemiBold))
        self.task_title.setWordWrap(True)
        lay.addWidget(self.task_title)

        self.task_tags = QHBoxLayout()
        self.task_tags.setSpacing(6)
        self.task_tags.addStretch(1)
        lay.addLayout(self.task_tags)

        # Standing rule 3: hazards render before the procedure and cannot be
        # collapsed. They are never summarised and never truncated.
        self.hazards = QVBoxLayout()
        self.hazards.setSpacing(6)
        lay.addLayout(self.hazards)

        self.body_label = QLabel("")
        self.body_label.setWordWrap(True)
        self.body_label.setFont(mono_font(9.5, QFont.Weight.Normal))
        self.body_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.body_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body_label.setContentsMargins(11, 9, 11, 9)
        bhost = QWidget()
        bl = QVBoxLayout(bhost)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.body_label)
        bl.addStretch(1)
        self.body_scroll = _scroll(bhost)
        lay.addWidget(self.body_scroll, 1)
        return panel

    def _answer_side(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Band")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(15, 11, 15, 12)
        lay.setSpacing(8)

        q = QLabel(THE_QUESTION)
        q.setWordWrap(True)
        q.setFont(ui_font(11.5, QFont.Weight.DemiBold))
        lay.addWidget(q)

        self.verdicts = VerdictBar(self.theme_name)
        self.verdicts.chosen.connect(self._verdict_chosen)
        lay.addWidget(self.verdicts)

        # conditional follow-ups
        self.followup = QWidget()
        fl = QGridLayout(self.followup)
        fl.setContentsMargins(0, 2, 0, 0)
        fl.setHorizontalSpacing(9)
        fl.setVerticalSpacing(5)

        self.reason_caption = Placard("Reason")
        self.reason_combo = QComboBox()
        self.reason_combo.setMinimumWidth(250)
        fl.addWidget(self.reason_caption, 0, 0)
        fl.addWidget(self.reason_combo, 1, 0)

        self.correction_caption = Placard("Correct task")
        crow = QHBoxLayout()
        crow.setSpacing(6)
        self.correction = QLineEdit()
        self.correction.setPlaceholderText("select from the task list…")
        self.correction.setReadOnly(True)
        self.correction.setFont(mono_font(10, QFont.Weight.Normal))
        self.correction.setMinimumWidth(190)
        crow.addWidget(self.correction)
        self.pick_btn = QPushButton("Find…")
        self.pick_btn.clicked.connect(self.pick_task)
        crow.addWidget(self.pick_btn)
        cwrap = QWidget()
        cwrap.setLayout(crow)
        fl.addWidget(self.correction_caption, 0, 1)
        fl.addWidget(cwrap, 1, 1)

        self.unknown_box = QCheckBox(
            "The correct task cannot be identified from the available corpus")
        self.unknown_box.toggled.connect(self._unknown_toggled)
        fl.addWidget(self.unknown_box, 2, 0, 1, 2)

        self.conf_caption = Placard("Confidence (optional)")
        self.conf_combo = QComboBox()
        self.conf_combo.addItem("—", None)
        for c in G.CONFIDENCES:
            self.conf_combo.addItem(c.capitalize(), c)
        self.conf_combo.currentIndexChanged.connect(lambda _: self._changed())
        self.correction.textChanged.connect(lambda _: self._changed())
        fl.addWidget(self.conf_caption, 0, 2)
        fl.addWidget(self.conf_combo, 1, 2)
        fl.setColumnStretch(3, 1)
        lay.addWidget(self.followup)

        note_row = QHBoxLayout()
        note_row.setSpacing(9)
        self.note = QLineEdit()
        self.note.setPlaceholderText("Optional short note")
        self.note.textChanged.connect(lambda _: self._changed())
        note_row.addWidget(self.note, 1)
        self.problem_label = QLabel("")
        self.problem_label.setObjectName("Faint")
        self.problem_label.setFont(ui_font(9))
        self.problem_label.setWordWrap(True)
        note_row.addWidget(self.problem_label, 2)
        self.edit_btn = QPushButton("Edit answer")
        self.edit_btn.setToolTip(
            "Revise the finalized answer. The current answer stays in force "
            "until the replacement is saved.")
        self.edit_btn.clicked.connect(self.begin_edit)
        note_row.addWidget(self.edit_btn)
        self.cancel_btn = QPushButton("Cancel edit")
        self.cancel_btn.clicked.connect(self.cancel_edit)
        note_row.addWidget(self.cancel_btn)
        self.draft_btn = QPushButton("Save draft")
        self.draft_btn.clicked.connect(self.save_draft)
        note_row.addWidget(self.draft_btn)
        self.commit_btn = QPushButton("Save and next  ⏎")
        self.commit_btn.setObjectName("Primary")
        self.commit_btn.setMinimumHeight(38)
        self.commit_btn.clicked.connect(self.commit)
        note_row.addWidget(self.commit_btn)
        lay.addLayout(note_row)

        hint = QLabel("Y yes · N no · P partial · U unsure    ⏎ save and next    "
                      "⌫ previous    keys are inactive while typing")
        hint.setObjectName("Faint")
        hint.setFont(ui_font(8.5))
        lay.addWidget(hint)
        return bar

    # ── completion ────────────────────────────────────────────────────
    def _completion(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(13)
        lay.addWidget(SectionHeader("All cases answered",
                                    "review before freezing"))
        self.done_summary = QLabel("")
        self.done_summary.setWordWrap(True)
        self.done_summary.setFont(ui_font(10.5))
        lay.addWidget(self.done_summary)

        self.readiness_label = QLabel("")
        self.readiness_label.setWordWrap(True)
        self.readiness_label.setFont(ui_font(10))
        lay.addWidget(self.readiness_label)

        warn = QFrame()
        warn.setObjectName("Card")
        wl = QVBoxLayout(warn)
        wl.setContentsMargins(15, 12, 15, 13)
        wl.addWidget(Placard("Before you freeze"))
        t = QLabel(FREEZE_WARNING)
        t.setWordWrap(True)
        t.setFont(ui_font(10))
        wl.addWidget(t)
        lay.addWidget(warn)

        row = QHBoxLayout()
        row.addStretch(1)
        back = QPushButton("Back to dashboard")
        back.clicked.connect(self.show_dashboard)
        row.addWidget(back)
        self.freeze_btn = QPushButton("Freeze this gold set")
        self.freeze_btn.setObjectName("Primary")
        self.freeze_btn.setMinimumHeight(38)
        self.freeze_btn.clicked.connect(self.freeze)
        row.addWidget(self.freeze_btn)
        lay.addLayout(row)
        lay.addStretch(1)
        return _scroll(host)

    def _blocked(self) -> QWidget:
        self.blocked_state = EmptyState(
            "mdi6.shield-lock-outline", "Not available",
            "This account cannot open the gold review.", theme=self.theme_name)
        return self.blocked_state

    # ── loading ───────────────────────────────────────────────────────
    def start(self) -> None:
        """Entry point for the host. Decides which screen the reviewer gets."""
        if not self.service.authorised():
            self._block("mdi6.shield-lock-outline", "Not authorised",
                        "This account does not hold the 'gold_review' "
                        "permission. An administrator grants it on the role.")
            return
        if not self.service.queue_exists:
            self._block("mdi6.database-off-outline",
                        "No gold-set queue in this database",
                        "The stratified 400-case queue is built during corpus "
                        "preparation. Nothing here can be reviewed until it "
                        "exists.")
            return
        self.refresh_dashboard()
        self.stack.setCurrentIndex(self.DASHBOARD)

    def _block(self, icon: str, headline: str, detail: str) -> None:
        old = self.blocked_state
        self.blocked_state = EmptyState(icon, headline, detail,
                                        theme=self.theme_name)
        self.stack.insertWidget(self.BLOCKED, self.blocked_state)
        self.stack.removeWidget(old)
        old.deleteLater()
        self.stack.setCurrentIndex(self.BLOCKED)

    def refresh_dashboard(self) -> None:
        if not self.service.queue_exists:
            return
        p = self.service.progress()
        while self.tiles.count():
            item = self.tiles.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        cells = [
            ("Total cases", f"{p.total}"), ("Completed", f"{p.completed}"),
            ("Remaining", f"{p.remaining}"), ("Complete", f"{p.pct:.1f}%"),
            ("Yes", f"{p.yes}"), ("No", f"{p.no}"), ("Partial", f"{p.partial}"),
            ("Unsure", f"{p.unsure}  ({p.unsure_pct:.0f}%)"),
        ]
        for i, (cap, value) in enumerate(cells):
            box = QFrame()
            box.setObjectName("Card")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(13, 9, 13, 10)
            bl.setSpacing(1)
            bl.addWidget(Placard(cap))
            v = QLabel(value)
            v.setFont(ui_font(17, QFont.Weight.DemiBold, tabular=True))
            bl.addWidget(v)
            self.tiles.addWidget(box, i // 4, i % 4)

        self.bar.setRange(0, max(p.total, 1))
        self.bar.setValue(p.completed)
        reviewer = getattr(self.service.user, "display_name", None) or "—"
        state = (f"FROZEN as release v{p.release_version}" if p.frozen
                 else "open for review")
        self.meta.setText(
            f"Reviewer: {reviewer}   ·   Last saved: "
            f"{(p.last_saved or '—')[:19].replace('T', ' ')}   ·   Set is "
            f"{state}   ·   {p.drafts} draft(s) in progress")

        while self.chapter_flow.count():
            item = self.chapter_flow.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for chapter, done, total in p.per_chapter:
            self.chapter_flow.addWidget(
                Tag(f"ATA {chapter}  {done}/{total}", self.theme_name))
        self.chapter_flow.addStretch(1)

        self.resume_btn.setEnabled(not p.frozen)
        self.resume_btn.setText("Review answered cases (frozen)" if p.frozen
                                else "Resume next unanswered case")
        self.progress_changed.emit()

    def resume(self) -> None:
        self.session_target = self.session_combo.currentData() or 0
        self.session_done = 0
        seq = self.service.resume_seq()
        if seq is None:
            return
        self.load(seq)
        self.stack.setCurrentIndex(self.QUESTION)

    def open_filtered(self, kind: str) -> None:
        seqs = self.service.filtered_sequences(kind)
        if not seqs:
            QMessageBox.information(self, "Nothing to show",
                                    f"No cases match '{kind}'.")
            return
        idx = self.filter_combo.findData(kind)
        if idx >= 0:
            self.filter_combo.blockSignals(True)
            self.filter_combo.setCurrentIndex(idx)
            self.filter_combo.blockSignals(False)
        self.load(seqs[0])
        self.stack.setCurrentIndex(self.QUESTION)

    def show_dashboard(self) -> None:
        if not self._confirm_leaving():
            return
        self.refresh_dashboard()
        self.stack.setCurrentIndex(self.DASHBOARD)

    def load(self, seq: int | None) -> None:
        if seq is None:
            return
        pair = self.service.pair(seq)
        if pair is None:
            return
        self.seq, self.pair = seq, pair
        self._editing = False

        self.position_label.setText(f"Case {pair.position} of {pair.total}")
        answered = pair.is_answered
        self.state_badge.kind = "ok" if answered else "unknown"
        self.state_badge.override = ("answered" if answered else
                                     "draft saved" if pair.draft else
                                     "unanswered")
        self.state_badge.refresh_theme(self.theme_name)

        self.defect_text.setText(
            pair.defect_text or "— no defect narrative on record —")
        meta = []
        if pair.ata_ref:
            meta.append(f"reported ATA {pair.ata_ref}")
        if pair.aircraft_model:
            meta.append(pair.aircraft_model)
        self._fill_tags(self.defect_meta, meta or ["no further context"])

        layout = self.locator.parentWidget().layout()
        layout.replaceWidget(self.locator,
                             new := AtaLocator(pair.task_number, self.theme_name))
        self.locator.deleteLater()
        self.locator = new
        self.task_title.setText(
            pair.task_title or "— this task number is not held in the corpus —")

        tags = []
        if pair.manual_type:
            tags.append(f"{pair.manual_type} Rev {pair.revision or '—'}")
        if pair.aircraft_type:
            tags.append(pair.aircraft_type)
        if pair.task_in_corpus:
            tags.append("current revision" if pair.is_current
                        else "NOT the current revision")
        if pair.catalogue_only:
            tags.append("catalogue only — no procedure held")
        self._fill_tags(self.task_tags, tags or ["not in corpus"])

        while self.hazards.count():
            item = self.hazards.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for kind, texts in (("alert", pair.warnings), ("warn", pair.cautions)):
            for text in texts:
                self.hazards.addWidget(self._hazard(kind, text))

        if pair.has_body:
            self.body_label.setText(pair.task_body)
        else:
            self.body_label.setText(pair.body_unavailable_reason)
        self.body_scroll.verticalScrollBar().setValue(0)

        # A draft the reviewer left behind takes precedence over the stored
        # answer as *what to show*; the answer itself stays in force either way.
        r = pair.draft or pair.answer
        self._restore(r)
        self._loaded_state = self._form_state()
        self._update_commit()

    def _restore(self, r) -> None:
        """Put a stored answer or draft on screen without marking it edited."""
        widgets = (self.correction, self.unknown_box, self.note,
                   self.conf_combo, self.reason_combo)
        for w in widgets:
            w.blockSignals(True)
        try:
            self.verdicts.select(r.verdict if r else None, emit=False)
            self.correction.setText((r.correct_task_number if r else "") or "")
            self.unknown_box.setChecked(bool(r and r.correct_task_unknown))
            self.note.setText((r.note if r else "") or "")
            idx = self.conf_combo.findData(r.confidence if r else None)
            self.conf_combo.setCurrentIndex(max(idx, 0))
            self._sync_followup(r.verdict if r else None,
                                keep_reason=r.reason_code if r else None,
                                touch=False)
        finally:
            for w in widgets:
                w.blockSignals(False)
        self._apply_readonly()

    def _form_state(self) -> tuple:
        """Exactly the fields a reviewer can change."""
        return (self.verdicts.current(),
                self.correction.text().strip() or None,
                self.unknown_box.isChecked(),
                self.reason_combo.currentData(),
                self.note.text().strip() or None,
                self.conf_combo.currentData())

    @property
    def dirty(self) -> bool:
        if self._loaded_state is None or self.pair is None:
            return False
        return self._form_state() != self._loaded_state

    def _apply_readonly(self) -> None:
        """A finalized answer is read-only until Edit is chosen."""
        answered = bool(self.pair and self.pair.is_answered)
        frozen = self.service.is_frozen()
        locked = frozen or (answered and not self._editing)
        for w in (self.verdicts, self.reason_combo, self.pick_btn,
                  self.unknown_box, self.note, self.conf_combo):
            w.setEnabled(not locked)
        self.edit_btn.setVisible(answered and not self._editing and not frozen)
        self.cancel_btn.setVisible(answered and self._editing)
        self.draft_btn.setEnabled(not frozen and (not answered or self._editing))

    def begin_edit(self) -> None:
        self._editing = True
        self._apply_readonly()
        self._update_commit()

    def cancel_edit(self) -> None:
        """Return to the unchanged finalized answer."""
        self._editing = False
        self._restore(self.pair.answer if self.pair else None)
        self._loaded_state = self._form_state()
        self._update_commit()

    def _hazard(self, kind: str, text: str) -> QWidget:
        box = QFrame()
        box.setObjectName("HazardBox")
        pal = T.THEMES[self.theme_name]
        colour = pal["red"] if kind == "alert" else pal["amb"]
        quiet = pal["redq"] if kind == "alert" else pal["ambq"]
        box.setStyleSheet(f"QFrame#HazardBox{{background:{quiet};"
                          f"border:1px solid {colour};border-left:4px solid "
                          f"{colour};border-radius:3px;}}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(11, 8, 11, 9)
        lay.setSpacing(3)
        head = QHBoxLayout()
        head.setSpacing(6)
        icon = QLabel()
        icon.setPixmap(qta.icon("mdi6.alert-outline" if kind == "alert"
                                else "mdi6.alert-circle-outline",
                                color=colour).pixmap(15, 15))
        head.addWidget(icon)
        word = QLabel("WARNING" if kind == "alert" else "CAUTION")
        word.setFont(ui_font(9, QFont.Weight.Bold))
        word.setStyleSheet(f"color:{colour};")
        head.addWidget(word)
        head.addStretch(1)
        lay.addLayout(head)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setFont(ui_font(10))
        lay.addWidget(body)
        return box

    def _fill_tags(self, layout: QHBoxLayout, texts: list[str]) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for text in texts:
            layout.addWidget(Tag(text, self.theme_name))
        layout.addStretch(1)

    # ── answering ─────────────────────────────────────────────────────
    def _changed(self) -> None:
        """Any editable field moved. Dirtiness is derived, never asserted."""
        self._update_commit()

    def _verdict_chosen(self, verdict: str) -> None:
        if verdict == "yes":
            had = self.correction.text().strip() or self.unknown_box.isChecked()
            if had:
                ok = QMessageBox.question(
                    self, "Clear the correction?",
                    "A Yes verdict cannot carry a correction. Clear the "
                    "correct task you entered?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if ok != QMessageBox.StandardButton.Yes:
                    stored = (self.pair.draft or self.pair.answer
                              if self.pair else None)
                    self.verdicts.select(stored.verdict if stored else None,
                                         emit=False)
                    return
                self.correction.clear()
                self.unknown_box.blockSignals(True)
                self.unknown_box.setChecked(False)
                self.unknown_box.blockSignals(False)
        self._sync_followup(verdict)
        self._changed()

    def _sync_followup(self, verdict: str | None,
                       keep_reason: str | None = None,
                       touch: bool = True) -> None:
        needs_reason = verdict in ("no", "partial", "unsure")
        needs_correction = verdict in ("no", "partial")
        self.reason_caption.setVisible(needs_reason)
        self.reason_combo.setVisible(needs_reason)
        for w in (self.correction_caption, self.correction, self.pick_btn,
                  self.unknown_box):
            w.setVisible(needs_correction)
        self.correction.parentWidget().setVisible(needs_correction)
        self.followup.setVisible(bool(verdict))

        if needs_reason:
            source = (G.WRONG_REASONS if needs_correction else G.UNSURE_REASONS)
            current = keep_reason or self.reason_combo.currentData()
            self.reason_combo.blockSignals(True)
            self.reason_combo.clear()
            self.reason_combo.addItem("— choose a reason —", None)
            for code, label in source:
                self.reason_combo.addItem(label, code)
            idx = self.reason_combo.findData(current)
            self.reason_combo.setCurrentIndex(max(idx, 0))
            self.reason_combo.blockSignals(False)
            # Connected once, at construction. Reconnecting on every verdict
            # change meant disconnecting a signal that might have no
            # connections yet, which Qt reports as a failure every time.
            if not getattr(self, "_reason_wired", False):
                self.reason_combo.currentIndexChanged.connect(
                    lambda _: self._changed())
                self._reason_wired = True
        if touch:
            self._update_commit()

    def _unknown_toggled(self, on: bool) -> None:
        if on:
            self.correction.clear()
        self.correction.setEnabled(not on)
        self.pick_btn.setEnabled(not on)
        self._changed()

    def pick_task(self) -> None:
        dlg = TaskPickerDialog(self.service, self.theme_name, self)
        outcome = dlg.exec()
        if outcome == QDialog.DialogCode.Accepted and dlg.chosen:
            self.correction.setText(dlg.chosen)
            self.unknown_box.setChecked(False)
            self._changed()
        elif outcome == QDialog.DialogCode.Accepted + 1:
            self.correction.clear()
            self.unknown_box.setChecked(True)
            self._changed()

    def _draft_fields(self) -> dict:
        return {
            "verdict": self.verdicts.current(),
            "correct_task_number": self.correction.text().strip() or None,
            "correct_task_unknown": self.unknown_box.isChecked(),
            "reason_code": self.reason_combo.currentData(),
            "note": self.note.text().strip() or None,
            "confidence": self.conf_combo.currentData(),
        }

    def _candidate(self) -> G.Answer:
        return G.Answer(queue_id=self.pair.queue_id,
                        reviewer_user_id=self.service.user_id or 0,
                        review_kind=self.service.review_kind,
                        **self._draft_fields())

    def _update_commit(self) -> None:
        if self.pair is None:
            return
        frozen = self.service.is_frozen()
        problems = G.validate(self.service.con, self._candidate(), final=True)
        ok = not problems and not frozen
        self.commit_btn.setEnabled(ok)
        self.draft_btn.setEnabled(not frozen)
        if frozen:
            self.problem_label.setText("This gold set is frozen — read only.")
        elif problems:
            self.problem_label.setText(problems[0])
        else:
            self.problem_label.setText("")

    def save_draft(self) -> bool:
        """Returns True only when the draft actually reached the database."""
        if self.pair is None:
            return False
        try:
            self.service.save_draft(self.pair.queue_id, **self._draft_fields())
        except G.GoldReviewError as exc:
            QMessageBox.warning(self, "Not saved", str(exc))
            return False
        self.load(self.seq)
        self.progress_changed.emit()
        return True

    def commit(self) -> None:
        if self.pair is None:
            return
        reason = None
        if self.pair.is_answered:
            from PySide6.QtWidgets import QInputDialog
            reason, ok = QInputDialog.getText(
                self, "Reason for the revision",
                "This case already has a finalized answer. Record why it is "
                "being changed:")
            if not ok:
                return
            if not reason.strip():
                QMessageBox.warning(self, "Not saved",
                                    "A revision needs a reason.")
                return
        try:
            self.service.finalize(self.pair.queue_id, change_reason=reason,
                                  **self._draft_fields())
        except G.ValidationFailed as exc:
            QMessageBox.warning(self, "Not saved", "\n".join(exc.problems))
            return
        except G.GoldReviewError as exc:
            QMessageBox.warning(self, "Not saved", str(exc))
            return
        self._editing = False
        self.session_done += 1
        self.progress_changed.emit()

        if self.session_target and self.session_done >= self.session_target:
            self.session_done = 0
            QMessageBox.information(
                self, "Block complete",
                f"That is {self.session_target} cases. A break here is a good "
                f"idea — your place is saved.")
        self.session_label.setText(
            f"{self.session_done} of {self.session_target} this session"
            if self.session_target else "")

        nxt = self.service.next_unanswered_seq(self.seq)
        p = self.service.progress()
        if p.completed >= p.total:
            self.show_completion()
            return
        self.load(nxt if nxt is not None else self.seq)

    def show_completion(self) -> None:
        p = self.service.progress()
        problems = self.service.readiness()
        corrected = self.service.con.execute(
            "SELECT COUNT(*) FROM gold_review_response WHERE review_kind='primary'"
            " AND correct_task_number IS NOT NULL").fetchone()[0]
        unavailable = self.service.con.execute(
            "SELECT COUNT(*) FROM gold_review_response WHERE review_kind='primary'"
            " AND correct_task_unknown=1").fetchone()[0]
        chapters = ", ".join(f"ATA {c} {d}/{t}" for c, d, t in p.per_chapter)
        self.done_summary.setText(
            f"Yes {p.yes}   ·   No {p.no}   ·   Partial {p.partial}   ·   "
            f"Unsure {p.unsure} ({p.unsure_pct:.1f}%)\n"
            f"{corrected} case(s) corrected to another task   ·   "
            f"{unavailable} case(s) where the correct task was unavailable\n\n"
            f"Coverage — {chapters}")
        self.readiness_label.setText(
            "Data-integrity checks passed." if not problems
            else "Cannot freeze yet:\n· " + "\n· ".join(problems[:8]))
        self.freeze_btn.setEnabled(not problems and not p.frozen)
        self.stack.setCurrentIndex(self.DONE)

    def freeze(self) -> None:
        ok = QMessageBox.question(
            self, "Freeze this gold set?", FREEZE_WARNING + "\n\nProceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if ok != QMessageBox.StandardButton.Yes:
            return
        try:
            version = self.service.freeze()
        except G.GoldReviewError as exc:
            QMessageBox.warning(self, "Not frozen", str(exc))
            return
        QMessageBox.information(self, "Gold set frozen",
                                f"Released as version {version}. Editing is "
                                f"now closed.")
        self.refresh_dashboard()
        self.stack.setCurrentIndex(self.DASHBOARD)

    # ── navigation ────────────────────────────────────────────────────
    def can_leave(self) -> bool:
        """The page-leave contract, used by navigation and by window close.

        Returns True only when it is genuinely safe to go: choosing *Save
        draft* and having that save fail keeps the reviewer on the case,
        because reporting success there would discard the work it failed to
        store.
        """
        if not self.dirty or self.pair is None:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Unfinished response")
        box.setText("This case has changes that are not saved as an answer.")
        box.setInformativeText(
            "Save them as a draft, discard them, or stay on the case.")
        save = box.addButton("Save draft", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Discard changes",
                                QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Stay on case", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is save:
            return self.save_draft()
        if box.clickedButton() is discard:
            self._restore(self.pair.draft or self.pair.answer)
            self._loaded_state = self._form_state()
            return True
        return False

    # kept for the questionnaire's own internal navigation
    _confirm_leaving = can_leave

    def _go(self, seq: int | None) -> None:
        if seq is None or not self._confirm_leaving():
            return
        self.load(seq)

    def previous(self) -> None:
        self._go(self.service.previous_seq(self.seq)) if self.seq is not None else None

    def next_case(self) -> None:
        self._go(self.service.next_seq(self.seq)) if self.seq is not None else None

    def next_unanswered(self) -> None:
        self._go(self.service.next_unanswered_seq(self.seq))

    def _filter_changed(self) -> None:
        seqs = self.service.filtered_sequences(self.filter_combo.currentData())
        if seqs:
            self._go(seqs[0])

    def _jump(self) -> None:
        text = self.jump.text().strip()
        self.jump.clear()
        if not text.isdigit():
            return
        seqs = self.service.sequences()
        idx = int(text) - 1
        if 0 <= idx < len(seqs):
            self._go(seqs[idx])

    # ── keyboard ──────────────────────────────────────────────────────
    def keyPressEvent(self, event):
        if self.stack.currentIndex() != self.QUESTION or _is_typing():
            super().keyPressEvent(event)
            return
        key = event.text().lower()
        if key in G.VERDICT_KEYS:
            # `select` emits `chosen`, which runs `_verdict_chosen`. Calling
            # it again here would run the handler — and its confirmations —
            # twice for one keypress.
            self.verdicts.select(G.VERDICT_KEYS[key])
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.commit_btn.isEnabled():
                self.commit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Backspace:
            self.previous()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Tab and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.next_unanswered()
            event.accept()
            return
        super().keyPressEvent(event)

    # ── theming ───────────────────────────────────────────────────────
    def refresh_theme(self, theme: str) -> None:
        self.theme_name = theme
        for child in self.findChildren(QWidget):
            fn = getattr(child, "refresh_theme", None)
            if callable(fn) and child is not self:
                fn(theme)
        if self.pair is not None:
            # Hazard boxes carry literal palette colours, so they are rebuilt.
            # The form is restored from the same source and re-snapshotted, so
            # switching theme can never look like an edit.
            editing, seq = self._editing, self.seq
            self.load(seq)
            self._editing = editing
            self._apply_readonly()
