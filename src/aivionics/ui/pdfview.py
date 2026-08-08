"""In-app PDF viewer for the manual chapters.

**Renderer choice, measured rather than assumed.** The intended
implementation was the native QtPdf stack (`QPdfDocument` + `QPdfView`),
which ships with PySide6. It cannot read this corpus: across all 16 AMM
chapter files it returns `InvalidFileFormat` for every one, while PyMuPDF
opens the 10 that PLAN 1.1 records as readable. The files do not begin with
`%PDF` — there is a binary prefix ahead of the header, which MuPDF tolerates
by scanning for it and PDFium rejects outright. A QtPdf code path would
therefore be dead code on the only corpus we have, so this viewer renders
page images with PyMuPDF, which is already a project dependency.

**View-only, deliberately.** There is no save, no export and no print button
here, and there will not be one. Standing rule 1 sends the engineer to the
controlled manual for the procedure; letting them export a page out of this
application would manufacture exactly the uncontrolled copy that rule exists
to prevent. Reading the chapter inside the app is the same act as opening the
PDF, under the app's revision context. The print path stays locator-only and
lives in `printing.py`.
"""
from __future__ import annotations

from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QScrollArea, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget)

from . import theme as T
from .widgets import EmptyState, ui_font

ZOOM_STEPS = [0.5, 0.67, 0.8, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
FIT_WIDTH = "fit"
MAX_SEARCH_PAGES = 400          # cap a wrap-around search on a huge chapter


class PdfViewer(QWidget):
    """Chapter viewer: page navigation, zoom, in-document search."""

    closed = Signal()

    def __init__(self, theme: str = T.DEFAULT_THEME, parent: QWidget | None = None):
        super().__init__(parent)
        self.theme_name = theme
        self.path: Path | None = None
        self._doc = None
        self.page_no = 0
        self.zoom: float | str = FIT_WIDTH
        self._matches: list = []          # fitz Rects on the current page
        self._match_index = -1

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._header())
        lay.addWidget(self._toolbar())

        self.stack = QStackedWidget()

        self.canvas = QLabel()
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignHCenter
                                 | Qt.AlignmentFlag.AlignTop)
        self.canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.scroll = QScrollArea()
        # Not widgetResizable: the canvas is sized to the rendered page and the
        # scroll area centres it, which is what an image viewer wants. With
        # resizing on, the fixed-size canvas is pinned to the top-left corner.
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setWidget(self.canvas)
        self.stack.addWidget(self.scroll)

        self.error_state = EmptyState(
            "mdi6.file-remove-outline",
            "Chapter PDF not available",
            "The manual corpus lives on an external drive. Connect it, or point "
            "AIVIONICS_CORPUS at the folder holding the chapter PDFs, and open "
            "the chapter again.",
            theme=theme)
        self.stack.addWidget(self.error_state)
        lay.addWidget(self.stack, 1)

        self._set_controls_enabled(False)

    # ── chrome ────────────────────────────────────────────────────────
    def _header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("PageHeader")
        bar.setFixedHeight(38)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(11, 0, 15, 0)
        lay.setSpacing(11)

        self.back_btn = QToolButton()
        self.back_btn.setObjectName("RailBtn")
        self.back_btn.setFixedSize(28, 26)
        self.back_btn.setToolTip("Back to the ATA tree")
        self.back_btn.clicked.connect(self.closed.emit)
        lay.addWidget(self.back_btn)

        self.context = QLabel("")
        self.context.setFont(ui_font(9, QFont.Weight.DemiBold))
        lay.addWidget(self.context)
        lay.addStretch(1)

        self.source_label = QLabel("")
        self.source_label.setObjectName("Faint")
        self.source_label.setFont(ui_font(8))
        lay.addWidget(self.source_label)
        return bar

    def _toolbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Band")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(11, 7, 11, 7)
        lay.setSpacing(7)

        self.prev_btn = self._tool("mdi6.chevron-left", "Previous page",
                                   lambda: self.step_page(-1))
        self.next_btn = self._tool("mdi6.chevron-right", "Next page",
                                   lambda: self.step_page(+1))
        lay.addWidget(self.prev_btn)
        lay.addWidget(self.next_btn)

        self.page_edit = QLineEdit()
        self.page_edit.setFixedWidth(52)
        self.page_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_edit.setToolTip("Go to page")
        self.page_edit.returnPressed.connect(self._on_page_typed)
        lay.addWidget(self.page_edit)

        self.page_total = QLabel("of —")
        self.page_total.setObjectName("Faint")
        self.page_total.setFont(ui_font(8.5, tabular=True))
        lay.addWidget(self.page_total)

        lay.addSpacing(10)
        self.zoom_out_btn = self._tool("mdi6.magnify-minus-outline", "Zoom out",
                                       lambda: self.step_zoom(-1))
        self.zoom_in_btn = self._tool("mdi6.magnify-plus-outline", "Zoom in",
                                      lambda: self.step_zoom(+1))
        self.fit_btn = self._tool("mdi6.arrow-expand-horizontal", "Fit to width",
                                  self.fit_width)
        lay.addWidget(self.zoom_out_btn)
        lay.addWidget(self.zoom_in_btn)
        lay.addWidget(self.fit_btn)
        self.zoom_label = QLabel("")
        self.zoom_label.setObjectName("Faint")
        self.zoom_label.setFont(ui_font(8.5, tabular=True))
        self.zoom_label.setMinimumWidth(56)
        lay.addWidget(self.zoom_label)

        lay.addSpacing(12)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Find in this chapter")
        self.search_box.setMinimumWidth(230)
        self.search_box.setClearButtonEnabled(True)
        self.search_box.returnPressed.connect(lambda: self.step_match(+1))
        self.search_box.textChanged.connect(self._on_search_text)
        lay.addWidget(self.search_box)

        self.match_prev = self._tool("mdi6.chevron-up", "Previous match",
                                     lambda: self.step_match(-1))
        self.match_next = self._tool("mdi6.chevron-down", "Next match",
                                     lambda: self.step_match(+1))
        lay.addWidget(self.match_prev)
        lay.addWidget(self.match_next)

        self.match_label = QLabel("")
        self.match_label.setObjectName("Faint")
        self.match_label.setFont(ui_font(8.5, tabular=True))
        self.match_label.setMinimumWidth(120)
        lay.addWidget(self.match_label)

        lay.addStretch(1)
        note = QLabel("View only — use the controlled manual for the procedure")
        note.setObjectName("Faint")
        note.setFont(ui_font(8))
        lay.addWidget(note)
        return bar

    def _tool(self, glyph: str, tip: str, slot) -> QToolButton:
        btn = QToolButton()
        btn.setObjectName("RailBtn")
        btn.setProperty("glyph", glyph)
        btn.setFixedSize(28, 26)
        btn.setToolTip(tip)
        btn.clicked.connect(slot)
        return btn

    # ── opening ───────────────────────────────────────────────────────
    def open(self, path: Path | str | None, context: str = "",
             page: int | None = None) -> bool:
        """Load a chapter. Returns False and shows why if it cannot be read."""
        self.context.setText(context or "Manual chapter")
        self._close_document()

        if path is None:
            self._fail("Chapter PDF not available",
                       "The manual corpus lives on an external drive. Connect "
                       "it, or point AIVIONICS_CORPUS at the folder holding the "
                       "chapter PDFs, and open the chapter again.")
            return False

        self.path = Path(path)
        self.source_label.setText(self.path.name)
        try:
            import fitz
            self._doc = fitz.open(str(self.path))
        except Exception:
            self._fail("Chapter PDF could not be opened",
                       "The file is present but could not be parsed.")
            return False

        if self._doc.page_count <= 0:
            # Six chapters of this AMM are damaged (PLAN 1.1). Saying so beats
            # a blank canvas that reads as a bug in the viewer.
            self._fail(
                "This chapter has no readable pages",
                "The file opens but renders no pages. Six chapters of this AMM "
                "are known to be damaged and are pending repair (PLAN 1.1). Any "
                "tasks already extracted from it are still listed in the tree.")
            return False

        self.stack.setCurrentWidget(self.scroll)
        self._set_controls_enabled(True)
        self.zoom = FIT_WIDTH
        self.goto_page(page or 0)
        return True

    def _close_document(self) -> None:
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:
                pass
        self._doc = None
        self._matches = []
        self._match_index = -1

    def _fail(self, headline: str, detail: str | None = None) -> None:
        self.path = None
        self.source_label.setText("")
        self.error_state.headline.setText(headline)
        if detail:
            self.error_state.detail.setText(detail)
        self.stack.setCurrentWidget(self.error_state)
        self._set_controls_enabled(False)

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (self.prev_btn, self.next_btn, self.page_edit,
                       self.zoom_out_btn, self.zoom_in_btn, self.fit_btn,
                       self.search_box, self.match_prev, self.match_next):
            widget.setEnabled(enabled)
        if not enabled:
            self.canvas.clear()
            self.page_edit.clear()
            self.page_total.setText("of —")
            self.match_label.setText("")
            self.zoom_label.setText("")

    # ── rendering ─────────────────────────────────────────────────────
    @property
    def page_count(self) -> int:
        return self._doc.page_count if self._doc is not None else 0

    def _scale_for(self, page) -> float:
        if self.zoom == FIT_WIDTH:
            available = max(self.scroll.viewport().width() - 24, 200)
            return available / page.rect.width
        return float(self.zoom)

    def render_page(self) -> None:
        """Rasterise the current page and paint any search hits over it."""
        if self._doc is None:
            return
        import fitz

        page = self._doc[self.page_no]
        scale = self._scale_for(page)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = QImage(pixmap.samples, pixmap.width, pixmap.height,
                       pixmap.stride, QImage.Format.Format_RGB888).copy()
        canvas = QPixmap.fromImage(image)

        if self._matches:
            pal = T.THEMES[self.theme_name]
            painter = QPainter(canvas)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            for index, rect in enumerate(self._matches):
                colour = QColor(pal["cyf"] if index == self._match_index
                                else pal["cy"])
                colour.setAlpha(90 if index == self._match_index else 45)
                painter.fillRect(
                    QRectF(rect.x0 * scale, rect.y0 * scale,
                           rect.width * scale, rect.height * scale), colour)
            painter.end()

        self.canvas.setPixmap(canvas)
        self.canvas.setFixedSize(canvas.size())
        self._update_labels(scale, page)

    def _update_labels(self, scale: float, page) -> None:
        self.page_edit.setText(str(self.page_no + 1))
        self.page_total.setText(f"of {self.page_count}")
        percent = round(scale * 100)
        self.zoom_label.setText(f"{percent}%"
                                + ("  fit" if self.zoom == FIT_WIDTH else ""))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._doc is not None and self.zoom == FIT_WIDTH:
            self.render_page()

    def showEvent(self, event):
        # Fit-to-width needs the real viewport width. When the viewer is opened
        # while still the hidden page of a QStackedWidget, the first render is
        # computed against a placeholder size, so redo it once it is visible.
        super().showEvent(event)
        if self._doc is not None and self.zoom == FIT_WIDTH:
            self.render_page()

    # ── navigation ────────────────────────────────────────────────────
    def goto_page(self, page: int, keep_matches: bool = False) -> None:
        if self._doc is None:
            return
        self.page_no = max(0, min(page, self.page_count - 1))
        if not keep_matches:
            self._matches = []
            self._match_index = -1
            self._find_on_current_page()
        self.render_page()
        self.scroll.verticalScrollBar().setValue(0)

    def step_page(self, delta: int) -> None:
        self.goto_page(self.page_no + delta)

    def _on_page_typed(self) -> None:
        text = self.page_edit.text().strip()
        if text.isdigit():
            self.goto_page(int(text) - 1)      # the toolbar counts from 1
        else:
            self.render_page()

    # ── zoom ──────────────────────────────────────────────────────────
    def fit_width(self) -> None:
        self.zoom = FIT_WIDTH
        self.render_page()

    def step_zoom(self, direction: int) -> None:
        if self._doc is None:
            return
        current = self._scale_for(self._doc[self.page_no])
        steps = ZOOM_STEPS if direction > 0 else list(reversed(ZOOM_STEPS))
        self.zoom = next((z for z in steps
                          if (z > current + 0.01 if direction > 0
                              else z < current - 0.01)), current)
        self.render_page()

    # ── search ────────────────────────────────────────────────────────
    def _needle(self) -> str:
        return self.search_box.text().strip()

    def _find_on_current_page(self) -> None:
        needle = self._needle()
        self._matches = []
        self._match_index = -1
        if not needle or self._doc is None:
            return
        try:
            self._matches = self._doc[self.page_no].search_for(needle)
        except Exception:
            self._matches = []
        if self._matches:
            self._match_index = 0

    def _on_search_text(self, _text: str) -> None:
        self._find_on_current_page()
        self.render_page()
        self._update_match_label()

    def _update_match_label(self) -> None:
        if not self._needle():
            self.match_label.setText("")
        elif self._matches:
            self.match_label.setText(
                f"{self._match_index + 1} of {len(self._matches)} on this page")
        else:
            self.match_label.setText("none on this page")

    def step_match(self, delta: int) -> None:
        """Walk hits on this page, then jump to the next page that has any.

        Scanning every page of an 800-page chapter to produce a total match
        count would stall the window, so the count shown is per page and the
        search walks outward on demand.
        """
        if self._doc is None or not self._needle():
            return
        if self._matches and 0 <= self._match_index + delta < len(self._matches):
            self._match_index += delta
            self.render_page()
            self._update_match_label()
            return
        self._jump_to_page_with_match(delta)

    def _jump_to_page_with_match(self, direction: int) -> None:
        needle = self._needle()
        total = self.page_count
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for offset in range(1, min(total, MAX_SEARCH_PAGES) + 1):
                page_no = (self.page_no + offset * (1 if direction > 0 else -1)) % total
                try:
                    hits = self._doc[page_no].search_for(needle)
                except Exception:
                    continue
                if hits:
                    self.page_no = page_no
                    self._matches = hits
                    self._match_index = 0 if direction > 0 else len(hits) - 1
                    self.render_page()
                    self.scroll.verticalScrollBar().setValue(0)
                    self._update_match_label()
                    return
        finally:
            QApplication.restoreOverrideCursor()
        self.match_label.setText("no further matches")

    # ── theming ───────────────────────────────────────────────────────
    def refresh_theme(self, theme: str) -> None:
        self.theme_name = theme
        pal = T.THEMES[theme]
        self.back_btn.setIcon(qta.icon("mdi6.arrow-left", color=pal["cy"]))
        for btn in (self.prev_btn, self.next_btn, self.zoom_out_btn,
                    self.zoom_in_btn, self.fit_btn, self.match_prev,
                    self.match_next):
            btn.setIcon(qta.icon(btn.property("glyph"), color=pal["txt2"],
                                 color_disabled=pal["txt3"]))
        # The page itself renders in its true colours in both themes — a
        # manual page is a document, not chrome, and inverting it would change
        # what a warning triangle looks like.
        self.scroll.setStyleSheet(f"background:{pal['bg']};border:none;")
        self.canvas.setStyleSheet("background:transparent;")
        if self._doc is not None:
            self.render_page()

    def closeEvent(self, event):
        self._close_document()
        super().closeEvent(event)
