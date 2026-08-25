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

**No file export.** The owner permits printing, but every printed sheet is
watermarked and stamped with its manual revision, source page, operator and
time. The viewer still cannot export a clean PDF copy that could be circulated
without that provenance.
"""
from __future__ import annotations

from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from . import theme as T
from .widgets import EmptyState, ui_font

ZOOM_STEPS = [0.5, 0.67, 0.8, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
PAGE_GAP = 14          # gap between pages in the strip
RENDER_AHEAD = 2       # pages rasterised either side of the viewport
FIT_WIDTH = "fit"
MAX_SEARCH_PAGES = 400          # cap a wrap-around search on a huge chapter


class PdfViewer(QWidget):
    """Chapter viewer: page navigation, zoom, in-document search."""

    closed = Signal()
    printed = Signal(int, int)      # first, last page index printed

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

        # One label per page, stacked vertically, so the whole chapter scrolls
        # as a document rather than advancing a page at a time. Pages are
        # rasterised only when they come near the viewport: a 559-page AMM
        # chapter rendered eagerly is several gigabytes of pixmap.
        self.canvas = QWidget()
        self.page_layout = QVBoxLayout(self.canvas)
        self.page_layout.setContentsMargins(0, 0, 0, 0)
        self.page_layout.setSpacing(PAGE_GAP)
        self.page_labels: list[QLabel] = []
        self._rendered: set[int] = set()

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setWidget(self.canvas)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scrolled)
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
        self.print_btn = self._tool("mdi6.printer-outline",
                                    "Print a page range — every sheet is "
                                    "stamped UNCONTROLLED COPY with its source "
                                    "page, revision and timestamp",
                                    self.print_pages)
        lay.addWidget(self.print_btn)
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
        self._build_strip()
        self.goto_page(page or 0)
        return True

    # ── the page strip ────────────────────────────────────────────────
    def _clear_strip(self) -> None:
        while self.page_layout.count():
            item = self.page_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.page_labels = []
        self._rendered = set()

    def _build_strip(self) -> None:
        """One correctly-sized placeholder per page, rendered on demand.

        Sizing every placeholder up front is what makes the scrollbar honest:
        the bar reflects the whole chapter immediately, instead of growing as
        pages are rasterised.
        """
        self._clear_strip()
        if self._doc is None:
            return
        for index in range(self._doc.page_count):
            label = QLabel()
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter
                               | Qt.AlignmentFlag.AlignTop)
            label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            rect = self._doc[index].rect
            scale = self._scale_for(self._doc[index])
            label.setFixedSize(int(rect.width * scale), int(rect.height * scale))
            self.page_layout.addWidget(label, 0, Qt.AlignmentFlag.AlignHCenter)
            self.page_labels.append(label)
        self.page_layout.addStretch(1)
        self._render_visible()

    def _visible_range(self) -> tuple[int, int]:
        if not self.page_labels:
            return 0, -1
        top = self.scroll.verticalScrollBar().value()
        # Before the viewer is mapped the viewport reports zero height, and a
        # zero-height window matches every page — which rasterised all 559
        # pages of a chapter on open. Assume one screenful until it is real.
        height = self.scroll.viewport().height() or 900
        bottom = top + height
        first, last = None, 0
        for index, label in enumerate(self.page_labels):
            y = label.y()
            if y + label.height() < top:
                continue
            if y > bottom:
                break
            if first is None:
                first = index
            last = index
        first = 0 if first is None else first
        return max(0, first - RENDER_AHEAD), min(len(self.page_labels) - 1,
                                                 last + RENDER_AHEAD)

    def _render_visible(self) -> None:
        if self._doc is None or not self.page_labels:
            return
        first, last = self._visible_range()
        for index in range(first, last + 1):
            if index not in self._rendered:
                self._render_into(index)
        # Free pixmaps well outside the viewport, so scrolling a long chapter
        # does not accumulate every page it passed.
        for index in list(self._rendered):
            if index < first - RENDER_AHEAD * 4 or index > last + RENDER_AHEAD * 4:
                self.page_labels[index].clear()
                self._rendered.discard(index)

    def _render_into(self, index: int) -> None:
        import fitz
        page = self._doc[index]
        scale = self._scale_for(page)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = QImage(pix.samples, pix.width, pix.height, pix.stride,
                       QImage.Format.Format_RGB888).copy()
        canvas = QPixmap.fromImage(image)
        if self._matches and index == self.page_no:
            pal = T.THEMES[self.theme_name]
            painter = QPainter(canvas)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            for i, rect in enumerate(self._matches):
                colour = QColor(pal["cyf"] if i == self._match_index else pal["cy"])
                colour.setAlpha(90 if i == self._match_index else 45)
                painter.fillRect(QRectF(rect.x0 * scale, rect.y0 * scale,
                                        rect.width * scale, rect.height * scale),
                                 colour)
            painter.end()
        label = self.page_labels[index]
        label.setFixedSize(canvas.size())
        label.setPixmap(canvas)
        self._rendered.add(index)

    def _on_scrolled(self, _value: int = 0) -> None:
        """Keep the page number in step with what the reader is looking at."""
        if not self.page_labels:
            return
        self._render_visible()
        top = self.scroll.verticalScrollBar().value()
        centre = top + self.scroll.viewport().height() // 3
        for index, label in enumerate(self.page_labels):
            if label.y() <= centre <= label.y() + label.height():
                if index != self.page_no:
                    self.page_no = index
                    self._sync_page_controls()
                return

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
        # Drop the document as well as the path. A file that opened with zero
        # pages was previously left loaded, so the next `showEvent` called
        # `render_page`, indexed page 0 of an empty document and raised
        # `IndexError: page 0 not in document` — a failed open took the whole
        # page down on the *next* chapter the reader tried.
        self._close_document()
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
                       self.print_btn,
                       self.search_box, self.match_prev, self.match_next):
            widget.setEnabled(enabled)
        if not enabled:
            # `canvas` is the page strip now, not a single QLabel, so it is
            # emptied by tearing the strip down rather than by `clear()`.
            self._clear_strip()
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
        """Refresh the visible part of the strip.

        Kept under its old name because `showEvent`, `resizeEvent` and the
        page box all call it; it no longer draws a single page onto one
        canvas, it asks the strip to repaint what the reader can see.
        """
        if self._doc is None:
            return
        # A zero-page document is reachable: six chapters of this AMM open
        # and render nothing. Guard here as well, because `showEvent` calls
        # this directly and must never raise.
        if self._doc.page_count <= 0:
            return
        self._rendered.discard(self.page_no)
        self._render_visible()
        if self.page_no < len(self.page_labels):
            page = self._doc[self.page_no]
            self._update_labels(self._scale_for(page), page)

    def _sync_page_controls(self) -> None:
        """The page box follows the scroll position."""
        self.page_edit.setText(str(self.page_no + 1))
        self.page_total.setText(f"of {self.page_count}")

    def _update_labels(self, scale: float, page) -> None:
        self.page_edit.setText(str(self.page_no + 1))
        self.page_total.setText(f"of {self.page_count}")
        percent = round(scale * 100)
        self.zoom_label.setText(f"{percent}%"
                                + ("  fit" if self.zoom == FIT_WIDTH else ""))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._doc is not None and self.zoom == FIT_WIDTH:
            # Fit-to-width changes every page's size, so the whole strip is
            # re-measured rather than only the visible page repainted.
            self._build_strip()

    def showEvent(self, event):
        # Fit-to-width needs the real viewport width. When the viewer is opened
        # while still the hidden page of a QStackedWidget, the first render is
        # computed against a placeholder size, so redo it once it is visible.
        super().showEvent(event)
        if self._doc is not None and self.zoom == FIT_WIDTH:
            self._build_strip()

    # ── printing ──────────────────────────────────────────────────────
    def print_pages(self) -> bool:
        """Print a range of pages from the open document.

        PLAN standing rule 1 said "never render or print a task body outside
        the app — print locators only, then send the engineer to the
        controlled source". The owner reversed that on 2026-08-25.

        The reason the rule existed does not go away with it: a loose printed
        procedure can outlive the revision it came from, and an engineer
        working from a superseded sheet is an airworthiness problem. So every
        sheet produced here carries, burned into the page rather than offered
        as an option:

          * a diagonal **UNCONTROLLED COPY** watermark,
          * the manual, revision and source page number it came from,
          * who printed it and when.

        A sheet found on a bench can therefore always be traced back and
        checked against the controlled revision.
        """
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter

        if self._doc is None or self._doc.page_count <= 0:
            return False

        first, last = self._print_range()
        if first is None:
            return False

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setFromTo(1, last - first + 1)
        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle(f"Print pages {first + 1}–{last + 1}")
        if dialog.exec() != QPrintDialog.DialogCode.Accepted:
            return False

        painter = QPainter()
        if not painter.begin(printer):
            return False
        try:
            for offset, index in enumerate(range(first, last + 1)):
                if offset:
                    printer.newPage()
                self._paint_sheet(painter, printer, index)
        finally:
            painter.end()
        self.printed.emit(first, last)
        return True

    def _print_range(self) -> tuple:
        """Ask which pages. Defaults to the page on screen."""
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(
            self, "Print pages",
            f"Pages to print (1\u2013{self.page_count}).\n"
            f"A single page, or a range such as 12-18:",
            text=str(self.page_no + 1))
        if not ok or not text.strip():
            return None, None
        raw = text.replace("\u2013", "-").strip()
        try:
            if "-" in raw:
                a, b = (int(part) for part in raw.split("-", 1))
            else:
                a = b = int(raw)
        except ValueError:
            return None, None
        first, last = sorted((a - 1, b - 1))
        first = max(0, first)
        last = min(self.page_count - 1, last)
        if last < first:
            return None, None
        return first, last

    def _paint_sheet(self, painter, printer, index: int) -> None:
        """One page, scaled to the sheet, stamped and captioned."""
        import fitz
        from datetime import datetime, timezone
        from PySide6.QtPrintSupport import QPrinter

        page_rect = printer.pageRect(QPrinter.Unit.DevicePixel)
        header = page_rect.height() * 0.045
        footer = page_rect.height() * 0.035
        body_h = page_rect.height() - header - footer

        page = self._doc[index]
        scale = min(page_rect.width() / page.rect.width,
                    body_h / page.rect.height) * 2.0
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = QImage(pix.samples, pix.width, pix.height, pix.stride,
                       QImage.Format.Format_RGB888).copy()

        target = QRectF(page_rect.x(), page_rect.y() + header,
                        page_rect.width(), body_h)
        fitted = QRectF(image.rect())
        fitted.setSize(fitted.size().scaled(target.size(),
                                            Qt.AspectRatioMode.KeepAspectRatio))
        fitted.moveCenter(target.center())
        painter.drawImage(fitted, image)

        source = self.source_label.text() or (self.path.name if self.path else "")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        who = getattr(self, "printed_by", "") or ""

        font = painter.font()
        font.setPointSizeF(max(page_rect.height() * 0.010, 6.0))
        painter.setFont(font)
        painter.setPen(QColor("#333333"))
        painter.drawText(
            QRectF(page_rect.x(), page_rect.y(), page_rect.width(), header),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            f"{self.context.text()}")
        painter.drawText(
            QRectF(page_rect.x(), page_rect.y(), page_rect.width(), header),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            f"source page {index + 1} of {self.page_count}")
        painter.drawText(
            QRectF(page_rect.x(), page_rect.bottom() - footer,
                   page_rect.width(), footer),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            f"{source}   ·   printed {stamp}"
            + (f"   ·   {who}" if who else ""))
        painter.drawText(
            QRectF(page_rect.x(), page_rect.bottom() - footer,
                   page_rect.width(), footer),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            "Verify against the controlled revision before use")

        # The watermark goes on last, over the page image, so it cannot be
        # cropped off by trimming the margins.
        painter.save()
        mark = painter.font()
        mark.setPointSizeF(max(page_rect.height() * 0.045, 24.0))
        mark.setBold(True)
        painter.setFont(mark)
        painter.setPen(QColor(190, 38, 44, 46))
        painter.translate(page_rect.center())
        painter.rotate(-35)
        painter.drawText(QRectF(-page_rect.width() / 2, -page_rect.height() / 8,
                                page_rect.width(), page_rect.height() / 4),
                         int(Qt.AlignmentFlag.AlignCenter), "UNCONTROLLED COPY")
        painter.restore()

    # ── navigation ────────────────────────────────────────────────────
    def goto_page(self, page: int, keep_matches: bool = False) -> None:
        if self._doc is None:
            return
        self.page_no = max(0, min(page, self.page_count - 1))
        if not keep_matches:
            self._matches = []
            self._match_index = -1
            self._find_on_current_page()
        self._rendered.discard(self.page_no)
        self._render_visible()
        # Scroll the strip to the page rather than resetting to the top: the
        # document is continuous now, so "go to page 40" means put page 40
        # under the reader's eyes, not rewind to page 1.
        if self.page_no < len(self.page_labels):
            bar = self.scroll.verticalScrollBar()
            bar.blockSignals(True)
            bar.setValue(self.page_labels[self.page_no].y())
            bar.blockSignals(False)
            self._render_visible()
        self._sync_page_controls()

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
