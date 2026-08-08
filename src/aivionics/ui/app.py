"""Application shell (PLAN 4.1): frameless window, title bar, rail, pages."""
from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QSizeGrip,
                               QStackedWidget, QVBoxLayout, QWidget)

from .. import audit, config, db
from . import auth, fonts, printing
from . import store
from . import theme as T
from .login import LoginDialog
from .pages import (AdminPage, CompliancePage, DiagnosePage, FleetPage,
                    HomePage, ManualsPage, OpsPage, ReliabilityPage)
from .widgets import (AppStatusBar, Rail, ShellBackground, TitleBar, mono_font,
                      ui_font)

RESIZE_MARGIN = 5


@dataclass
class AppContext:
    """Everything a page needs, passed in rather than reached for globally."""

    con: sqlite3.Connection
    corpus: store.CorpusReader
    db_path: Path = field(default_factory=lambda: config.DB_PATH)
    theme_name: str = T.DEFAULT_THEME
    user: auth.User | None = None
    online_enabled: bool = False
    chain_ok: bool | None = None
    chain_rows: int = 0
    window: "MainWindow | None" = field(default=None, repr=False)

    def print_locator(self, task: dict, manual: dict | None = None,
                      aircraft: dict | None = None) -> str:
        """Format, log and show the locator block. Never any body text."""
        text = printing.print_locator(self.con, task, manual, aircraft, self.user)
        if self.window is not None:
            PrintPreview(text, self.theme_name, self.window).exec()
        return text


class PrintPreview(QDialog):
    """Preview of the locator block, with the option to send it to a printer."""

    def __init__(self, text: str, theme: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.text = text
        self.setWindowTitle("Print locator")
        self.setStyleSheet(fonts.qss(theme))
        self.setMinimumSize(620, 300)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(11)

        head = QLabel("Locator only — the procedure is not reproduced")
        head.setFont(ui_font(11, QFont.Weight.DemiBold))
        lay.addWidget(head)

        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        view.setFont(mono_font(10, QFont.Weight.Normal))
        lay.addWidget(view, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        send = QPushButton("Send to printer")
        send.setObjectName("Primary")
        send.clicked.connect(self.send_to_printer)
        buttons.addWidget(close)
        buttons.addWidget(send)
        lay.addLayout(buttons)

    def send_to_printer(self) -> None:
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dialog = QPrintDialog(printer, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            doc = QPlainTextEdit(self.text)
            doc.setFont(mono_font(10, QFont.Weight.Normal))
            doc.document().print_(printer)
        self.accept()


class MainWindow(QWidget):
    """Frameless shell. The window chrome is ours, so it can carry the mark."""

    PAGES = [
        ("home", HomePage),
        ("diagnose", DiagnosePage),
        ("manuals", ManualsPage),
        ("fleet", FleetPage),
        ("reliability", ReliabilityPage),
        ("compliance", CompliancePage),
        ("ops", OpsPage),
        ("admin", AdminPage),
    ]

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        ctx.window = self
        self.theme_name = ctx.theme_name

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowTitle("AIvionics")
        self.resize(1440, 900)
        self.setMinimumSize(1100, 700)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.frame = ShellBackground(self.theme_name, self)
        root.addWidget(self.frame)

        frame_lay = QVBoxLayout(self.frame)
        frame_lay.setContentsMargins(0, 0, 0, 0)
        frame_lay.setSpacing(0)

        self.titlebar = TitleBar(self.theme_name)
        self.titlebar.minimise_requested.connect(self.showMinimized)
        self.titlebar.maximise_requested.connect(self.toggle_maximised)
        self.titlebar.close_requested.connect(self.close)
        self.titlebar.theme_changed.connect(self.set_theme)
        frame_lay.addWidget(self.titlebar)

        shell = QWidget()
        shell_lay = QHBoxLayout(shell)
        shell_lay.setContentsMargins(0, 0, 0, 0)
        shell_lay.setSpacing(0)

        self.rail = Rail(self.theme_name)
        self.rail.navigated.connect(self.navigate)
        shell_lay.addWidget(self.rail)

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        for key, cls in self.PAGES:
            page = cls(ctx)
            self.pages[key] = page
            self.stack.addWidget(page)
        shell_lay.addWidget(self.stack, 1)
        frame_lay.addWidget(shell, 1)

        status_row = QWidget()
        sl = QHBoxLayout(status_row)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        self.statusbar = AppStatusBar(self.theme_name)
        sl.addWidget(self.statusbar, 1)
        grip = QSizeGrip(status_row)
        grip.setFixedSize(16, AppStatusBar.HEIGHT)
        sl.addWidget(grip, 0, Qt.AlignmentFlag.AlignBottom)
        frame_lay.addWidget(status_row)

        self.apply_context()
        self.set_theme(self.theme_name)
        self.navigate("home")

    # ── context ───────────────────────────────────────────────────────
    def apply_context(self) -> None:
        ctx = self.ctx
        self.rail.set_online_enabled(ctx.online_enabled)
        self.titlebar.set_badge("online" if ctx.online_enabled else "offline")

        counts = ctx.corpus.counts()
        if counts.get("tasks"):
            self.statusbar.corpus.setText(
                f"Corpus  {counts['amm']:,} AMM · {counts['fim']:,} FIM locators")
        else:
            self.statusbar.corpus.setText("Corpus not built")
        self.statusbar.cases.setText(
            f"Cases  {counts['cases']:,}" if counts.get("cases") else "Cases  none")

        user = ctx.user
        self.statusbar.user.setText(
            f"Signed in  {auth.signature(user)} · {user.role}" if user
            else "Not signed in")

        manuals = ctx.corpus.manuals()
        current = next((m for m in manuals if m["is_current"]), None)
        if current:
            self.titlebar.set_context(
                f"{current['aircraft_type']}  ·  {current['manual_type']} "
                f"Rev {current['revision']}  ·  issued {current['revision_date'] or '—'}")
        else:
            self.titlebar.set_context("No manual corpus loaded")

    # ── navigation ────────────────────────────────────────────────────
    def navigate(self, key: str) -> None:
        page = self.pages.get(key)
        if page is None:
            return
        self.rail.set_current(key)
        self.stack.setCurrentWidget(page)
        # Page navigation is deliberately not audited — see auth.AUDITED_ACTIONS.
        on_shown = getattr(page, "on_shown", None)
        if callable(on_shown):
            on_shown()

    # ── theming ───────────────────────────────────────────────────────
    def set_theme(self, theme: str) -> None:
        self.theme_name = theme
        self.ctx.theme_name = theme
        self.setStyleSheet(fonts.qss(theme))
        for widget in self.findChildren(QWidget):
            fn = getattr(widget, "refresh_theme", None)
            if callable(fn):
                fn(theme)
        self.frame.refresh_theme(theme)
        try:
            store.set_setting(self.ctx.con, "theme", theme)
        except sqlite3.Error:
            pass

    def toggle_maximised(self) -> None:
        self.showNormal() if self.isMaximized() else self.showMaximized()

    # ── frameless resize from any edge ────────────────────────────────
    def event(self, ev: QEvent) -> bool:
        """Start a native resize when the cursor is on the window border.

        Handled at the window level rather than per-widget so the gutter works
        even where a child widget is painted right up to the edge.
        """
        if ev.type() in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress) \
                and not self.isMaximized():
            edges = self._edges_at(ev.globalPosition().toPoint())
            if ev.type() == QEvent.Type.MouseMove:
                self._set_resize_cursor(edges)
            elif edges and ev.button() == Qt.MouseButton.LeftButton:
                handle = self.windowHandle()
                if handle is not None:
                    handle.startSystemResize(edges)
                    return True
        return super().event(ev)

    def _edges_at(self, pos) -> Qt.Edge:
        rect = self.frameGeometry()
        edges = Qt.Edge(0)
        if abs(pos.x() - rect.left()) <= RESIZE_MARGIN:
            edges |= Qt.Edge.LeftEdge
        if abs(pos.x() - rect.right()) <= RESIZE_MARGIN:
            edges |= Qt.Edge.RightEdge
        if abs(pos.y() - rect.top()) <= RESIZE_MARGIN:
            edges |= Qt.Edge.TopEdge
        if abs(pos.y() - rect.bottom()) <= RESIZE_MARGIN:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _set_resize_cursor(self, edges: Qt.Edge) -> None:
        horizontal = Qt.Edge.LeftEdge | Qt.Edge.RightEdge
        vertical = Qt.Edge.TopEdge | Qt.Edge.BottomEdge
        if (edges & horizontal) and (edges & vertical):
            diagonal = edges in (Qt.Edge.LeftEdge | Qt.Edge.TopEdge,
                                 Qt.Edge.RightEdge | Qt.Edge.BottomEdge)
            self.setCursor(Qt.CursorShape.SizeFDiagCursor if diagonal
                           else Qt.CursorShape.SizeBDiagCursor)
        elif edges & horizontal:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif edges & vertical:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.unsetCursor()

    def closeEvent(self, event):
        if self.ctx.user is not None:
            try:
                auth.logout(self.ctx.con, self.ctx.user)
            except sqlite3.Error:
                pass
        super().closeEvent(event)


# ── bootstrap ───────────────────────────────────────────────────────────

def build_context(db_path: Path | None = None) -> AppContext:
    """Open the app database, seed accounts, verify the audit chain."""
    con = db.connect(db_path)
    auth.seed(con)
    ok, rows = audit.verify_chain(con)
    corpus_path = Path(db_path) if db_path else config.DB_PATH
    corpus = store.CorpusReader(store.open_readonly(corpus_path))
    return AppContext(
        con=con,
        corpus=corpus,
        db_path=corpus_path,
        theme_name=store.get_setting(con, "theme", T.DEFAULT_THEME),
        online_enabled=store.online_enabled(con),
        chain_ok=ok,
        chain_rows=rows,
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point. ``--db PATH`` runs against an alternative database."""
    argv = list(argv if argv is not None else sys.argv)
    db_path: Path | None = None
    if "--db" in argv:
        i = argv.index("--db")
        db_path = Path(argv[i + 1])
        del argv[i:i + 2]

    app = QApplication.instance() or QApplication(argv)
    QGuiApplication.setApplicationDisplayName("AIvionics")

    ctx = build_context(db_path)
    app.setStyleSheet(fonts.qss(ctx.theme_name))

    dialog = LoginDialog(ctx.con, ctx.theme_name)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return 0
    ctx.user = dialog.user

    window = MainWindow(ctx)
    window.show()
    return app.exec()
