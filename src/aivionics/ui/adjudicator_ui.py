"""Adjudication window (PLAN 0.7) — the Qt layer over `adjudicator.py`.

One pair per screen, keyboard verdicts, auto-advance, progress saved after
every commit so the session can be abandoned and resumed at will.
"""
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QProgressBar, QPushButton, QScrollArea,
                               QSizeGrip, QVBoxLayout, QWidget)

from . import fonts
from . import theme as T
from .adjudicator import (VERDICT_KEYS, VERDICT_LABELS, AdjudicationQueue,
                          Pair, QueueMissing)
from .widgets import (AtaLocator, EmptyState, Placard, ShellBackground,
                      Splitter, StatusBadge, Tag, TitleBar, mono_font, ui_font)

VERDICT_BUTTONS = [
    ("yes", "Y", "ok"),
    ("no", "N", "alert"),
    ("partial", "P", "warn"),
    ("unsure", "U", "unknown"),
]


class AdjudicatorWindow(QWidget):
    def __init__(self, con: sqlite3.Connection, theme: str = T.DEFAULT_THEME):
        super().__init__()
        self.queue = AdjudicationQueue(con)
        self.theme_name = theme
        self.seq: int | None = None
        self.pair: Pair | None = None
        self.pending_verdict: str | None = None

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowTitle("AIvionics — Gold-set adjudication")
        self.resize(1340, 880)
        self.setMinimumSize(1040, 700)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.frame = ShellBackground(theme, self)
        root.addWidget(self.frame)
        lay = QVBoxLayout(self.frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.titlebar = TitleBar(theme)
        self.titlebar.set_context("Gold-set adjudication  ·  Phase 0.7")
        self.titlebar.set_badge("adjudicating")
        self.titlebar.minimise_requested.connect(self.showMinimized)
        self.titlebar.maximise_requested.connect(
            lambda: self.showNormal() if self.isMaximized() else self.showMaximized())
        self.titlebar.close_requested.connect(self.close)
        self.titlebar.theme_changed.connect(self.set_theme)
        lay.addWidget(self.titlebar)

        lay.addWidget(self._progress_band())

        self.body = Splitter(Qt.Orientation.Horizontal, theme)
        self.body.addWidget(self._defect_panel())
        self.body.addWidget(self._task_panel())
        self.body.setSizes([520, 820])
        lay.addWidget(self.body, 1)

        lay.addWidget(self._verdict_bar())

        grip_row = QWidget()
        gl = QHBoxLayout(grip_row)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.addStretch(1)
        gl.addWidget(QSizeGrip(grip_row))
        grip_row.setFixedHeight(14)
        lay.addWidget(grip_row)

        self.missing = EmptyState(
            "mdi6.database-off-outline",
            "No adjudication queue in this database",
            "The stratified queue has not been built yet. "
            "Run scripts/build_gold_queue.py first, then reopen this tool.",
            theme=theme)
        self.missing.hide()
        lay.addWidget(self.missing)

        self.set_theme(theme)
        self.start()

    # ── layout ────────────────────────────────────────────────────────
    def _progress_band(self) -> QWidget:
        band = QFrame()
        band.setObjectName("Band")
        lay = QVBoxLayout(band)
        lay.setContentsMargins(15, 9, 15, 9)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(12)
        self.progress_label = QLabel("—")
        self.progress_label.setFont(ui_font(10, QFont.Weight.DemiBold, tabular=True))
        top.addWidget(self.progress_label)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        top.addWidget(self.bar, 1)
        self.unsure_label = QLabel("")
        self.unsure_label.setObjectName("Faint")
        self.unsure_label.setFont(ui_font(8.5, tabular=True))
        top.addWidget(self.unsure_label)
        lay.addLayout(top)

        self.strata = QHBoxLayout()
        self.strata.setSpacing(7)
        self.strata.addStretch(1)
        lay.addLayout(self.strata)
        return band

    def _defect_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("Card")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(17, 15, 17, 15)
        lay.setSpacing(9)

        lay.addWidget(Placard("Defect as reported — leak-free query"))
        self.defect_text = QLabel("")
        self.defect_text.setWordWrap(True)
        self.defect_text.setFont(ui_font(12))
        self.defect_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.defect_text.setAlignment(Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.defect_text)
        lay.addSpacing(6)

        self.defect_meta = QHBoxLayout()
        self.defect_meta.setSpacing(6)
        self.defect_meta.addStretch(1)
        lay.addLayout(self.defect_meta)
        lay.addStretch(1)

        hint = QLabel(
            "Judge the structure: is the cited task diagnostic or an action, and "
            "is it in the right ATA subject for this symptom? Whether it was the "
            "optimal entry point is a Part-66 judgement — mark those unsure.")
        hint.setObjectName("Faint")
        hint.setFont(ui_font(8.5))
        hint.setWordWrap(True)
        lay.addWidget(hint)
        return panel

    def _task_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(17, 15, 17, 10)
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

        # Warnings and cautions render first and cannot be collapsed
        # (standing rule 3). They are never summarised by anything.
        self.hazards = QVBoxLayout()
        self.hazards.setSpacing(6)
        lay.addLayout(self.hazards)

        self.body_scroll = QScrollArea()
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.body_label = QLabel("")
        self.body_label.setWordWrap(True)
        self.body_label.setFont(mono_font(9.5, QFont.Weight.Normal))
        self.body_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.body_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body_label.setContentsMargins(11, 9, 11, 9)
        host = QWidget()
        hl = QVBoxLayout(host)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.body_label)
        hl.addStretch(1)
        self.body_scroll.setWidget(host)
        lay.addWidget(self.body_scroll, 1)
        return panel

    def _verdict_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Band")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(15, 10, 15, 11)
        lay.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(9)
        self.verdict_buttons: dict[str, QPushButton] = {}
        for verdict, key, _status in VERDICT_BUTTONS:
            btn = QPushButton(f"  {key}   {verdict.capitalize()}")
            btn.setCheckable(True)
            btn.setMinimumHeight(38)
            btn.setToolTip(VERDICT_LABELS[verdict])
            btn.clicked.connect(lambda _=False, v=verdict: self.set_verdict(v))
            row.addWidget(btn)
            self.verdict_buttons[verdict] = btn
        row.addSpacing(10)

        correction = QVBoxLayout()
        correction.setSpacing(3)
        correction.addWidget(Placard("Correct task number (optional)"))
        self.correction = QLineEdit()
        self.correction.setPlaceholderText("e.g. 34-11-00-810-801")
        self.correction.setFont(mono_font(10, QFont.Weight.Normal))
        self.correction.setMinimumWidth(210)
        self.correction.returnPressed.connect(self.commit_and_advance)
        correction.addWidget(self.correction)
        row.addLayout(correction)

        self.commit_btn = QPushButton("Commit  ⏎")
        self.commit_btn.setObjectName("Primary")
        self.commit_btn.setMinimumHeight(38)
        self.commit_btn.clicked.connect(self.commit_and_advance)
        row.addWidget(self.commit_btn)
        lay.addLayout(row)

        self.keys_hint = QLabel(
            "Y yes · N no · P partial · U unsure   ⏎ commit and advance   "
            "⌫ previous pair")
        self.keys_hint.setObjectName("Faint")
        self.keys_hint.setFont(ui_font(8.5))
        lay.addWidget(self.keys_hint)
        return bar

    # ── data ──────────────────────────────────────────────────────────
    def start(self) -> None:
        try:
            self.seq = self.queue.resume_seq()
        except QueueMissing:
            self.body.hide()
            self.missing.show()
            self.progress_label.setText("queue not built")
            self.commit_btn.setEnabled(False)
            return
        self.load(self.seq)

    def load(self, seq: int | None) -> None:
        if seq is None:
            return
        pair = self.queue.pair(seq)
        if pair is None:
            return
        self.seq, self.pair = seq, pair
        self.pending_verdict = pair.verdict
        pal = T.THEMES[self.theme_name]

        self.defect_text.setText(pair.defect_text or "— no defect narrative on record —")
        self._fill(self.defect_meta, [
            f"seq {pair.seq}", f"stratum {pair.stratum}",
            f"tail {pair.tail or '—'}", f"reported {pair.reported_at[:10] or '—'}",
            f"ATA {pair.ata_ref or '—'}",
            f"fault code {pair.fault_code}" if pair.fault_code else "no fault code",
        ])

        layout = self.locator.parentWidget().layout()
        layout.replaceWidget(self.locator,
                             new := AtaLocator(pair.task_number, self.theme_name))
        self.locator.deleteLater()
        self.locator = new
        self.task_title.setText(pair.task_title or "— task not found in the corpus —")

        tags = []
        if pair.manual_type:
            tags.append(f"{pair.manual_type} Rev {pair.revision or '—'}")
        if pair.catalogue_only:
            tags.append("catalogue only — no procedure held")
        tags.append("diagnostic" if AtaLocator.is_diagnostic(
            pair.task_number.split("-")[3] if pair.task_number.count("-") >= 3 else "")
            else "action")
        self._fill(self.task_tags, tags)

        while self.hazards.count():
            w = self.hazards.takeAt(0).widget()
            if w:
                w.deleteLater()
        for kind, items in (("alert", pair.warnings), ("warn", pair.cautions)):
            for text in items:
                self.hazards.addWidget(self._hazard(kind, text))

        if pair.has_body:
            self.body_label.setText(pair.task_body)
            self.body_label.setStyleSheet(
                f"background:{pal['well']};border:1px solid {pal['line']};"
                f"border-radius:6px;color:{pal['txt']};")
        else:
            self.body_label.setText(
                "No procedure text held for this task.\n\n"
                "FIM rows are catalogue-only — the IFIM content is DRM-encrypted "
                "and was not decrypted. Judge this pair on the task number, its "
                "title and the ATA hierarchy alone, and mark it unsure if that is "
                "not enough.")
            self.body_label.setStyleSheet(
                f"background:{pal['s2']};border:1px dashed {pal['line']};"
                f"border-radius:6px;color:{pal['txt3']};")

        self.correction.setText(pair.correct_task_number or "")
        self._sync_verdict_buttons()
        self.refresh_progress()

    def _hazard(self, kind: str, text: str) -> QWidget:
        pal = T.THEMES[self.theme_name]
        colour = pal["red"] if kind == "alert" else pal["amb"]
        quiet = pal["redq"] if kind == "alert" else pal["ambq"]
        box = QFrame()
        box.setStyleSheet(f"background:{quiet};border:1px solid {colour};"
                          f"border-left-width:3px;border-radius:4px;")
        lay = QHBoxLayout(box)
        lay.setContentsMargins(10, 8, 11, 8)
        lay.setSpacing(9)
        lay.addWidget(StatusBadge(kind, "WARNING" if kind == "alert" else "CAUTION",
                                  self.theme_name), 0, Qt.AlignmentFlag.AlignTop)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setFont(ui_font(9))
        label.setStyleSheet(f"color:{pal['txt']};border:none;background:transparent;")
        lay.addWidget(label, 1)
        return box

    def _fill(self, layout, texts: list[str]) -> None:
        while layout.count() > 1:
            w = layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        for i, text in enumerate(texts):
            layout.insertWidget(i, Tag(text, self.theme_name))

    def refresh_progress(self) -> None:
        p = self.queue.progress()
        self.progress_label.setText(f"{p.done} / {p.total}")
        self.bar.setMaximum(max(p.total, 1))
        self.bar.setValue(p.done)
        self.unsure_label.setText(
            f"unsure {p.unsure} ({p.unsure / p.done * 100:.0f}%)" if p.done
            else "unsure 0")
        self._fill(self.strata,
                   [f"{name}  {done}/{total}" for name, done, total in p.per_stratum])

    # ── verdicts ──────────────────────────────────────────────────────
    def set_verdict(self, verdict: str) -> None:
        self.pending_verdict = verdict
        self._sync_verdict_buttons()

    def _sync_verdict_buttons(self) -> None:
        pal = T.THEMES[self.theme_name]
        for verdict, _key, status in VERDICT_BUTTONS:
            btn = self.verdict_buttons[verdict]
            chosen = verdict == self.pending_verdict
            btn.setChecked(chosen)
            spec = T.status_style(status, self.theme_name)
            btn.setStyleSheet(
                f"background:{spec['quiet']};border:2px solid {spec['color']};"
                f"border-radius:6px;color:{spec['color']};font-weight:700;"
                if chosen else
                f"background:{pal['s1']};border:1px solid {pal['line']};"
                f"border-radius:6px;color:{pal['txt2']};")
        self.commit_btn.setEnabled(self.pending_verdict is not None)

    def commit_and_advance(self) -> None:
        if self.seq is None or self.pending_verdict is None:
            return
        self.queue.commit(self.seq, self.pending_verdict,
                          self.correction.text())
        nxt = self.queue.next_seq(self.seq)
        if nxt is None:
            self.refresh_progress()
            self.keys_hint.setText("All pairs adjudicated. Close the window.")
            return
        self.correction.clear()
        self.load(nxt)

    def go_previous(self) -> None:
        if self.seq is None:
            return
        prev = self.queue.previous_seq(self.seq)
        if prev is not None:
            self.load(prev)

    # ── keyboard ──────────────────────────────────────────────────────
    def keyPressEvent(self, event):
        key = event.text().lower()
        if key in VERDICT_KEYS and not self.correction.hasFocus():
            self.set_verdict(VERDICT_KEYS[key])
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commit_and_advance()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Backspace and not self.correction.hasFocus():
            self.go_previous()
            event.accept()
            return
        super().keyPressEvent(event)

    # ── theming ───────────────────────────────────────────────────────
    def set_theme(self, theme: str) -> None:
        self.theme_name = theme
        self.setStyleSheet(fonts.qss(theme))
        for widget in self.findChildren(QWidget):
            fn = getattr(widget, "refresh_theme", None)
            if callable(fn):
                fn(theme)
        self.frame.refresh_theme(theme)
        if self.pair is not None:
            self.load(self.seq)
        else:
            self._sync_verdict_buttons()
