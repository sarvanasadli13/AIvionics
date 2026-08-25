"""Manuals browser (PLAN 4.5).

Aircraft type × manual type × revision, an ATA tree, and per-chapter
coverage shown in its own column — not a footnote. Coverage is the honest
statement of what was extracted versus what the chapter's own table of
contents claims, and Gate 1 refuses queries in ranges below 70%, so it has
to be visible at the point of use.

All corpus reads go through a read-only connection. If the database is not
built yet the page shows an empty state naming the script to run.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ... import documents as DOCS
from .. import pdfsource
from .. import theme as T
from ..pdfview import PdfViewer
from ..widgets import (StatusBadge, AtaLocator, EmptyState, Placard, SectionHeader,
                       Splitter, Tag, mono_font, ui_font)
from .base import Page, caption

COVERAGE_GATE = 70.0     # PLAN Gate 1


class ManualsPage(Page):
    title = "Manuals"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.manuals: list[dict] = []
        self.current_manual: dict | None = None
        self.current_task: dict | None = None
        self.current_chapter: str | None = None
        self.page_index = pdfsource.TaskPageIndex()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._selector_band())

        self.stack = QStackedWidget()
        self.browser = self._browser()
        self.stack.addWidget(self.browser)
        self.empty = EmptyState(
            "mdi6.database-off-outline",
            "No manual corpus in this database",
            "The manuals browser reads the manual, task and coverage tables. "
            "Build them with the Phase 1 ingest — python scripts/phase1.py — "
            "then reopen this page.",
            theme=self.theme_name)
        self.stack.addWidget(self.empty)

        self.viewer = PdfViewer(self.theme_name)
        self.viewer.closed.connect(
            lambda: self.stack.setCurrentWidget(self.browser))
        self.stack.addWidget(self.viewer)

        outer.addWidget(self.stack, 1)
        self.on_shown()

    # ── selectors ─────────────────────────────────────────────────────
    def _selector_band(self) -> QWidget:
        band = QFrame()
        band.setObjectName("Band")
        lay = QHBoxLayout(band)
        lay.setContentsMargins(15, 10, 15, 10)
        lay.setSpacing(16)

        self.type_combo = QComboBox()
        self.manual_combo = QComboBox()
        self.revision_combo = QComboBox()
        for placard, combo, width in (("Aircraft type", self.type_combo, 150),
                                      ("Manual", self.manual_combo, 110),
                                      ("Revision", self.revision_combo, 150)):
            cell = QVBoxLayout()
            cell.setSpacing(3)
            cell.addWidget(Placard(placard))
            combo.setMinimumWidth(width)
            cell.addWidget(combo)
            lay.addLayout(cell)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        self.manual_combo.currentIndexChanged.connect(self._on_manual_changed)
        self.revision_combo.currentIndexChanged.connect(self._on_revision_changed)

        # What kind of document this is, in words, next to the thing it
        # describes. Training material is useful reference; it is not
        # maintenance data, and the interface never lets the two look alike.
        cls = QVBoxLayout()
        cls.setSpacing(3)
        cls.addWidget(Placard("Document class"))
        self.class_badge = StatusBadge("ok", "maintenance data", self.theme_name)
        cls.addWidget(self.class_badge)
        lay.addLayout(cls)

        lay.addStretch(1)
        cov = QVBoxLayout()
        cov.setSpacing(3)
        cov.addWidget(Placard("Corpus coverage"))
        self.coverage_summary = QLabel("—")
        self.coverage_summary.setFont(ui_font(10, QFont.Weight.DemiBold, tabular=True))
        cov.addWidget(self.coverage_summary)
        lay.addLayout(cov)

        actions = QVBoxLayout()
        actions.setSpacing(3)
        actions.addWidget(Placard("Documents"))
        row = QHBoxLayout()
        row.setSpacing(6)
        self.add_btn = QPushButton("Add…")
        self.add_btn.setToolTip("Add a manual or training document from this PC")
        self.add_btn.clicked.connect(self.add_document)
        row.addWidget(self.add_btn)
        self.remove_btn = QPushButton("Remove…")
        self.remove_btn.setToolTip("Remove the selected document from the corpus")
        self.remove_btn.clicked.connect(self.remove_document)
        row.addWidget(self.remove_btn)
        wrap = QWidget()
        wrap.setLayout(row)
        actions.addWidget(wrap)
        lay.addLayout(actions)
        return band

    # ── browser ───────────────────────────────────────────────────────
    def _browser(self) -> QWidget:
        split = Splitter(Qt.Orientation.Horizontal, self.theme_name)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        self.tree_header = SectionHeader("ATA tree", "coverage vs the chapter TOC")
        ll.addWidget(self.tree_header)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["ATA / task", "Title", "Coverage"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setRootIsDecorated(True)
        self.tree.itemExpanded.connect(self._on_expand)
        self.tree.currentItemChanged.connect(self._on_task_selected)
        head = self.tree.header()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        ll.addWidget(self.tree, 1)
        split.addWidget(left)

        right = QWidget()
        right.setObjectName("Card")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        self.detail_header = SectionHeader("Task", "locator")
        rl.addWidget(self.detail_header)

        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(15, 14, 15, 14)
        dl.setSpacing(9)
        self.detail_locator = AtaLocator("—", self.theme_name)
        dl.addWidget(self.detail_locator)
        self.detail_title = QLabel("Select a task from the tree.")
        self.detail_title.setFont(ui_font(11, QFont.Weight.DemiBold))
        self.detail_title.setWordWrap(True)
        dl.addWidget(self.detail_title)

        self.detail_tags = QHBoxLayout()
        self.detail_tags.setSpacing(6)
        self.detail_tags.addStretch(1)
        dl.addLayout(self.detail_tags)

        self.detail_effectivity = caption("", "Muted", 9)
        dl.addWidget(self.detail_effectivity)
        dl.addStretch(1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.open_btn = QPushButton("Open in manual")
        self.open_btn.setObjectName("Primary")
        self.open_btn.setEnabled(False)
        self.open_btn.setToolTip(
            "Open the chapter PDF inside the app at this task's page")
        self.open_btn.clicked.connect(self._open_in_manual)
        actions.addWidget(self.open_btn)
        self.print_btn = QPushButton("Print locator")
        self.print_btn.setEnabled(False)
        self.print_btn.clicked.connect(self._print_locator)
        actions.addWidget(self.print_btn)
        actions.addStretch(1)
        dl.addLayout(actions)

        self.print_note = caption(
            "Printing emits the locator only — task number, title, manual, "
            "revision, effectivity, tail, timestamp and user. Never procedure text. "
            "The in-app viewer is read-only and cannot export.",
            "Faint", 8)
        dl.addWidget(self.print_note)
        rl.addWidget(detail, 1)
        split.addWidget(right)
        split.setSizes([620, 380])
        return split

    # ── data ──────────────────────────────────────────────────────────
    def on_shown(self) -> None:
        corpus = self.ctx.corpus if self.ctx else None
        self.manuals = corpus.manuals() if corpus else []
        if not self.manuals:
            self.stack.setCurrentWidget(self.empty)
            self.coverage_summary.setText("no corpus")
            return
        self.stack.setCurrentWidget(self.browser)
        types = sorted({m["aircraft_type"] for m in self.manuals})
        self.type_combo.blockSignals(True)
        self.type_combo.clear()
        self.type_combo.addItems(types)
        self.type_combo.blockSignals(False)
        self._on_type_changed()

    def _on_type_changed(self) -> None:
        chosen = self.type_combo.currentText()
        kinds = sorted({m["manual_type"] for m in self.manuals
                        if m["aircraft_type"] == chosen})
        self.manual_combo.blockSignals(True)
        self.manual_combo.clear()
        self.manual_combo.addItems(kinds)
        self.manual_combo.blockSignals(False)
        self._on_manual_changed()

    def _on_manual_changed(self) -> None:
        matches = [m for m in self.manuals
                   if m["aircraft_type"] == self.type_combo.currentText()
                   and m["manual_type"] == self.manual_combo.currentText()]
        self.revision_combo.blockSignals(True)
        self.revision_combo.clear()
        for m in matches:
            label = f"Rev {m['revision'] or '—'}"
            if m["is_current"]:
                label += "  · current"
            self.revision_combo.addItem(label, m)
        self.revision_combo.blockSignals(False)
        self._on_revision_changed()

    def _on_revision_changed(self) -> None:
        self.current_manual = self.revision_combo.currentData()
        if self.current_manual:
            # Selection state is authoritative.  If the page ever displayed
            # its empty state, choosing a real manual must immediately restore
            # the browser instead of updating controls over a stale panel.
            self.stack.setCurrentWidget(self.browser)
        self._refresh_class_badge()
        self._populate_tree()
        self._offer_whole_document()

    def _offer_whole_document(self) -> None:
        """A document with no task index is still readable as a document.

        Task-indexed manuals are opened *at a task*, which is the point of the
        locator workflow. A training PDF has no tasks to open at, so without
        this it could be added to the corpus and then never read — which is
        what happened.
        """
        manual = self.current_manual or {}
        source = manual.get("source_file") or ""
        whole = bool(source) and source.lower().endswith(
            (".pdf", ".docx", ".doc")) and not self._has_tasks(manual)
        self._whole_document = whole
        self.current_chapter = None
        self.current_task = None
        if whole:
            self.open_btn.setText("Open document")
            self.open_btn.setToolTip(
                "Open this document in the read-only viewer. It has no task "
                "index, so it opens at page 1.")
            self.open_btn.setEnabled(True)
        else:
            # Both branches must set every control they touch. Setting them
            # only in the `whole` branch left the previous document's label
            # and enabled-state behind when the selection changed.
            self.open_btn.setText("Open in manual")
            self.open_btn.setToolTip(
                "Open the chapter PDF inside the app at this task's page")
            self.open_btn.setEnabled(False)
        self.print_btn.setEnabled(False)

    def _has_tasks(self, manual: dict) -> bool:
        if not manual or not self.ctx:
            return False
        corpus = getattr(self.ctx, "corpus", None)
        return bool(corpus and corpus.has_tasks(manual.get("id")))

    def _refresh_class_badge(self) -> None:
        """Say in words what the selected document is.

        `DOCS.is_maintenance` reads the `doc_class` column rather than the
        manual's name, so a training document called "AMM" is still badged
        as training.
        """
        manual = self.current_manual or {}
        maintenance = DOCS.is_maintenance(manual) if manual else True
        self.class_badge.kind = "ok" if maintenance else "warn"
        self.class_badge.override = ("maintenance data" if maintenance
                                     else "TRAINING — not maintenance data")
        self.class_badge.refresh_theme(self.theme_name)
        self.class_badge.setToolTip(
            "Task numbers from this document may be cited on a locator."
            if maintenance else
            "Training material. It explains how the system works; it carries "
            "no ATA task numbers and is never cited as a maintenance task.")

    def _populate_tree(self) -> None:
        self.tree.clear()
        manual = self.current_manual
        if not manual or not self.ctx:
            self.coverage_summary.setText("—")
            return
        if not self._has_tasks(manual):
            # Not a failure: training material is organised by topic, not by
            # ATA task number, so there is no task index to show. Say that,
            # rather than presenting an empty tree that reads as broken.
            item = QTreeWidgetItem(self.tree)
            item.setText(0, "No task index")
            item.setText(1, "This document is organised by topic, not by ATA "
                            "task number. Use \u201cOpen document\u201d to read it.")
            item.setDisabled(True)
            self.coverage_summary.setText("no task index")
            self.detail_title.setText(
                manual.get("display_title") or "Document")
            self.detail_effectivity.setText(
                "Training material carries no effectivity and is never cited "
                "as a maintenance task.")
            return
        chapters = self.ctx.corpus.chapters(manual["id"])
        pal = T.THEMES[self.theme_name]
        total, covered = 0, 0.0
        for ch in chapters:
            item = QTreeWidgetItem(self.tree)
            item.setText(0, f"ATA {ch['chapter']}")
            item.setFont(0, mono_font(10))
            n = ch["tasks"]
            label = f"{n} task{'' if n == 1 else 's'} extracted"
            if not self._chapter_readable(ch["chapter"]):
                # Say it here, in the list, rather than after the reader has
                # committed to opening it. The tasks were extracted before the
                # file was damaged, so they remain listed and usable.
                label += "  ·  PDF cannot be opened"
                item.setForeground(1, QBrush(QColor(pal["amb"])))
                item.setToolTip(
                    1, "This chapter's PDF renders no pages and is pending "
                       "repair (PLAN 1.1). The extracted tasks below are "
                       "still valid; only the page images are unavailable.")
            item.setText(1, label)
            pct = ch["pct"]
            if pct is None:
                item.setText(2, "not measured")
                item.setForeground(2, QBrush(QColor(pal["txt3"])))
            else:
                item.setText(2, f"{pct:.0f}%")
                below = pct < COVERAGE_GATE
                item.setForeground(2, QBrush(QColor(pal["amb"] if below else pal["grn"])))
                # Colour is never the only carrier — the word rides along.
                item.setText(2, f"{pct:.0f}%  {'BELOW GATE' if below else 'OK'}")
                item.setToolTip(2, f"{ch['extracted']} of {ch['toc_count']} TOC entries"
                                   + (f" — below the {COVERAGE_GATE:.0f}% Gate 1 "
                                      "threshold; queries in this range are refused"
                                      if below else ""))
                total += 1
                covered += pct
            item.setData(0, Qt.ItemDataRole.UserRole, {"kind": "chapter",
                                                       "chapter": ch["chapter"]})
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        self.coverage_summary.setText(
            f"{covered / total:.0f}% mean over {total} chapter{'' if total == 1 else 's'}"
            if total
            else f"{len(chapters)} chapters · not measured")

    def _on_expand(self, item: QTreeWidgetItem) -> None:
        meta = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if meta.get("kind") != "chapter" or item.childCount():
            return
        tasks = self.ctx.corpus.tasks(self.current_manual["id"], meta["chapter"])
        for t in tasks:
            child = QTreeWidgetItem(item)
            child.setText(0, t["task_number"])
            child.setFont(0, mono_font(9.5))
            child.setText(1, t["title"] or "")
            child.setText(2, "catalogue only" if t["catalogue_only"] else "")
            child.setData(0, Qt.ItemDataRole.UserRole, {"kind": "task", "task": t})

    def _on_task_selected(self, item: QTreeWidgetItem | None, _prev=None) -> None:
        meta = (item.data(0, Qt.ItemDataRole.UserRole) or {}) if item else {}
        if meta.get("kind") == "chapter":
            self._show_chapter(meta["chapter"])
            return
        if meta.get("kind") != "task":
            return
        task = meta["task"]
        self.current_chapter = pdfsource.chapter_of(task["task_number"])
        self.open_btn.setText("Open in manual")
        self.open_btn.setToolTip(
            "Open the chapter PDF inside the app at this task's page")
        self.open_btn.setEnabled(True)
        self.current_task = task
        parent = self.detail_locator.parentWidget()
        layout = parent.layout()
        layout.replaceWidget(self.detail_locator,
                             new := AtaLocator(task["task_number"], self.theme_name))
        self.detail_locator.deleteLater()
        self.detail_locator = new
        self.detail_title.setText(task["title"] or "Title not recorded")

        while self.detail_tags.count() > 1:
            w = self.detail_tags.takeAt(0).widget()
            if w:
                w.deleteLater()
        manual = self.current_manual or {}
        tags = [f"{manual.get('manual_type', '—')} Rev {manual.get('revision', '—')}"]
        if task["catalogue_only"]:
            tags.append("catalogue only — no procedure held")
        if task["function_code"]:
            tags.append("diagnostic" if AtaLocator.is_diagnostic(task["function_code"])
                        else "action")
        warn, caut = task["warning_count"] or 0, task["caution_count"] or 0
        if warn or caut:
            tags.append(f"{warn} warnings · {caut} cautions")
        for i, text in enumerate(tags):
            self.detail_tags.insertWidget(i, Tag(text, self.theme_name))

        eff = (task["effectivity_raw"] or "").strip()
        self.detail_effectivity.setText(
            f"Effectivity: {eff}" if eff
            else "Effectivity: applicability unresolved — verify in controlled data")
        self.print_btn.setEnabled(True)

    def _show_chapter(self, chapter: str) -> None:
        """Chapter node selected: offer the whole chapter PDF, not a locator."""
        self.current_chapter = chapter
        self.current_task = None
        layout = self.detail_locator.parentWidget().layout()
        layout.replaceWidget(self.detail_locator,
                             new := AtaLocator(f"ATA {chapter}", self.theme_name))
        self.detail_locator.deleteLater()
        self.detail_locator = new
        self.detail_title.setText(f"ATA chapter {chapter}")
        while self.detail_tags.count() > 1:
            w = self.detail_tags.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.detail_effectivity.setText(
            "Open the chapter to read it inside the app, or expand it to pick "
            "a task.")
        self.open_btn.setText("Open chapter PDF")
        self.open_btn.setToolTip("Open this chapter's PDF inside the app")
        self.open_btn.setEnabled(True)
        self.print_btn.setEnabled(False)

    # ── documents ─────────────────────────────────────────────────────
    def add_document(self) -> None:
        """Pick a file, say what it is, and add it once the operator agrees."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Add a manual or training document", "",
            "Documents (*.pdf *.docx *.doc);;All files (*)")
        if not path:
            return
        found = DOCS.inspect(path)
        if not found.readable:
            QMessageBox.warning(self, "Cannot add this document", found.reason)
            return

        kind = ("maintenance data" if found.is_maintenance
                else "TRAINING MATERIAL — not maintenance data")
        detail = (f"{Path(path).name}\n\n"
                  f"Reads as: {kind}\n"
                  f"Aircraft: {found.aircraft_type or 'not detected'}\n"
                  f"Type: {found.manual_type}   ·   {found.pages:,} pages\n\n"
                  f"{found.reason}")
        if QMessageBox.question(
                self, "Add this document?", detail,
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            DOCS.add_document(self.ctx.con, path)
        except DOCS.DocumentError as exc:
            QMessageBox.warning(self, "Not added", str(exc))
            return
        self.on_shown()
        if self.ctx and getattr(self.ctx, "window", None):
            self.ctx.window.apply_context()

    def remove_document(self) -> None:
        """Remove the selected document, refusing to break locators silently."""
        manual = self.current_manual
        if not manual:
            QMessageBox.information(self, "Nothing selected",
                                    "Select a manual to remove.")
            return
        name = (f"{manual.get('aircraft_type', '—')} "
                f"{manual.get('manual_type', '—')} "
                f"Rev {manual.get('revision', '—')}")
        try:
            DOCS.remove_document(self.ctx.con, manual["id"])
        except DOCS.DocumentError as exc:
            # The refusal names the cost; the operator decides whether to pay it.
            if "Confirm explicitly" not in str(exc):
                QMessageBox.warning(self, "Not removed", str(exc))
                return
            if QMessageBox.question(
                    self, "Remove this manual and its tasks?",
                    f"{name}\n\n{exc}",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.Cancel
            ) != QMessageBox.StandardButton.Yes:
                return
            try:
                DOCS.remove_document(self.ctx.con, manual["id"], force=True)
            except DOCS.DocumentError as inner:
                QMessageBox.warning(self, "Not removed", str(inner))
                return
        self.current_manual = None
        self.on_shown()
        if self.ctx and getattr(self.ctx, "window", None):
            self.ctx.window.apply_context()

    def _chapter_readable(self, chapter: str) -> bool:
        """Whether this chapter's PDF renders any pages.

        Cached per manual for the session: the answer is a property of the
        file, and probing sixteen PDFs on every tree rebuild would be felt.
        """
        manual = self.current_manual or {}
        key = (manual.get("id"), chapter)
        cache = getattr(self, "_readable_cache", None)
        if cache is None:
            cache = self._readable_cache = {}
        if key in cache:
            return cache[key]
        ok = True
        path = self._chapter_pdf(chapter)
        if path is None:
            ok = False
        else:
            try:
                import fitz
                doc = fitz.open(str(path))
                ok = len(doc) > 0
                doc.close()
            except Exception:                                    # noqa: BLE001
                ok = False
        cache[key] = ok
        return ok

    # ── the PDF viewer ────────────────────────────────────────────────
    def _chapter_pdf(self, chapter: str):
        source = (self.current_manual or {}).get("source_file")
        return pdfsource.resolve_chapter_pdf(chapter, source)

    def _open_in_manual(self) -> None:
        """Open the chapter PDF, jumping to the task's page when there is one.

        The page lookup reads the text layer of every page until it hits, so
        it runs behind a wait cursor and is cached per task for the session.
        A missing drive is reported by the viewer, not raised.
        """
        manual = self.current_manual or {}
        chapter = self.current_chapter

        if getattr(self, "_whole_document", False) and not chapter:
            # A document with no task index: open the file itself at page 1.
            from pathlib import Path as _Path
            path = _Path(manual.get("source_file") or "")
            label = manual.get("display_title") or path.name
            kind = ("" if DOCS.is_maintenance(manual)
                    else "  ·  TRAINING MATERIAL — not maintenance data")
            self.stack.setCurrentWidget(self.viewer)
            self.viewer.open(path if path.exists() else None,
                             f"{manual.get('aircraft_type', '—')} · {label}{kind}",
                             None)
            return

        if not chapter:
            return
        path = self._chapter_pdf(chapter)
        context = (f"{manual.get('aircraft_type', '—')} · "
                   f"{manual.get('manual_type', '—')} Rev {manual.get('revision', '—')}"
                   f" · ATA {chapter}")

        page = None
        if self.current_task and path is not None:
            context += f" · TASK {self.current_task['task_number']}"
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                hit = self.page_index.find(path, self.current_task["task_number"])
            finally:
                QApplication.restoreOverrideCursor()
            if hit is None:
                context += "  (task heading not found — showing page 1)"
            else:
                page = hit.page
                if not hit.exact:
                    context += "  (nearest reference)"

        # Switch first: fit-to-width measures the viewport, which is only
        # correct once the viewer is the visible page of the stack.
        self.stack.setCurrentWidget(self.viewer)
        if not self.viewer.open(path, context, page):
            # Do not leave the reader on an error screen with only a "back"
            # link. Six chapters of this AMM render no pages, so a second
            # damaged chapter showed the identical message and looked as
            # though the first one had never closed. Return to the tree and
            # say which chapter failed, so the next click is an informed one.
            self.stack.setCurrentWidget(self.browser)
            QMessageBox.warning(
                self, f"ATA {chapter} cannot be opened",
                f"{context}\n\n"
                "This chapter's PDF renders no pages and is pending repair "
                "(PLAN 1.1). Six chapters of this AMM are affected and are "
                "marked in the tree.\n\n"
                "The tasks extracted from it are still listed and still "
                "valid — only the page images are unavailable.")

    def _print_locator(self) -> None:
        if self.current_task and self.ctx:
            self.ctx.print_locator(self.current_task, self.current_manual)
