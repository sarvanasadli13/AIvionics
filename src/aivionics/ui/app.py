"""Application shell (PLAN 4.1): frameless window, title bar, rail, pages."""
from __future__ import annotations

import sqlite3
import sys

# ── PyMuPDF must be loaded before Qt initialises ─────────────────────────
# Both PySide6 and PyMuPDF link their own copies of the same native
# dependencies. When MuPDF is loaded *after* `QApplication` has initialised
# Qt's native stack, it corrupts shared state, and the symptom is not an
# error: live QWidgets are destroyed underneath their Python wrappers.
#
# Reproduced 2026-08-25 — opening any document in the Manuals viewer deleted
# the ATA tree, so switching aircraft afterwards raised
# `RuntimeError: Internal C++ object (QTreeWidget) already deleted` and the
# manual list came back empty. Importing fitz first makes it deterministic.
#
# This import belongs at module scope, not inside `main()`: the preview and
# screenshot scripts construct `QApplication` themselves after importing this
# module, so a deferred import would be too late for them.
try:                                                             # pragma: no cover
    import fitz                                                  # noqa: F401
except ImportError:                                              # pragma: no cover
    fitz = None            # PDF features degrade; the shell still runs.
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QEvent, QPropertyAnimation, Qt,
                            QTimer)
from PySide6.QtGui import QFont, QGuiApplication, QIcon
from PySide6.QtWidgets import (QApplication, QDialog, QGraphicsOpacityEffect,
                               QMessageBox,
                               QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QSizeGrip, QStackedWidget, QVBoxLayout, QWidget)

from .. import audit, config, db, documents, goldreview
from . import auth, connectivity, fonts, nativewindow, printing
from . import store
from . import theme as T
from .login import LoginDialog
from .pages import (AdminPage, CompliancePage, DiagnosePage, FleetPage,
                    HomePage, ManualsPage, OpsPage, ReliabilityPage,
                    ValidationPage, AboutPage)
from .widgets import (AppStatusBar, Rail, ShellBackground, TitleBar, mono_font,
                      svg_pixmap, ui_font)

RESIZE_MARGIN = 5

# Shown beside the mark in the window chrome. It names what the product is;
# what is *loaded* belongs to the status bar and the Manuals page.
TAGLINE = "AI-assisted engineering workstation"


@dataclass
class AppContext:
    """Everything a page needs, passed in rather than reached for globally."""

    con: sqlite3.Connection
    corpus: store.CorpusReader
    db_path: Path = field(default_factory=lambda: config.DB_PATH)
    theme_name: str = T.DEFAULT_THEME
    user: auth.User | None = None
    online_enabled: bool = False
    rail_expanded: bool = True
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
        ("validation", ValidationPage),
        ("about", AboutPage),
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
        # The frame is ours to paint, but minimising, restoring and snapping
        # belong to the desktop. See nativewindow — BACKLOG item 3.
        nativewindow.restore_native_frame(self)

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

        self.rail = Rail(self.theme_name, expanded=ctx.rail_expanded)
        self.rail.navigated.connect(self.navigate)
        self.rail.expanded_changed.connect(self.remember_rail_state)
        shell_lay.addWidget(self.rail)

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        self.current_key = "home"
        for key, cls in self.PAGES:
            page = cls(ctx)
            self.pages[key] = page
            self.stack.addWidget(page)
        self.apply_permissions()
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

        # The badge reports a live fact, so something has to tell it when the
        # fact changes. Nothing is polled — see connectivity.
        self.reachability = connectivity.Reachability(self)
        self.reachability.changed.connect(self.refresh_online_badge)

        self.apply_context()
        self.set_theme(self.theme_name)
        self.navigate("home")

    # ── context ───────────────────────────────────────────────────────
    def apply_context(self) -> None:
        ctx = self.ctx
        self.rail.set_online_enabled(ctx.online_enabled)
        self.refresh_online_badge()

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

        # The chrome names the product, not the corpus. Naming the loaded
        # manuals here made a multi-type tool read as a single-type one — the
        # line said "737-8" because that is what happens to be installed. The
        # corpus is still stated, in the status bar and on the Manuals page,
        # where it describes the data rather than the product.
        self.titlebar.set_context(TAGLINE)
        self.titlebar.context.setToolTip(corpus_context(ctx.corpus.manuals()))

    def refresh_online_badge(self, *_) -> None:
        """ONLINE when the application can actually reach the network.

        That needs both halves — permission from Admin *and* a route off the
        machine — and the badge is wrong if it claims either one alone.
        Showing ONLINE with the switch off would announce a connection on a
        machine that is deliberately making none; showing ONLINE with the
        cable out would be a straightforward lie. Both failures read OFFLINE
        and the tooltip says which one it is.
        """
        permitted = bool(self.ctx.online_enabled)
        reachable = self.reachability.is_reachable()
        live = permitted and reachable
        self.titlebar.set_badge("online" if live else "offline", live=live)

        if not permitted:
            why = ("Online features are switched off in Admin — this machine "
                   "makes no outbound connection at all.")
        elif not reachable:
            why = ("Online features are on, but this machine has no route to "
                   "the network. Everything except the live Ops panels is "
                   "unaffected.")
        elif not self.reachability.supported:
            why = ("Online features are on. This build cannot read the network "
                   "state from the system, so reachability is assumed.")
        else:
            why = "Online features are on and the network is reachable."
        self.titlebar.badge.setToolTip(why)

    # ── navigation ────────────────────────────────────────────────────
    FADE_MS = 130

    def navigate(self, key: str, *, user_initiated: bool = True) -> bool:
        """Switch destination, subject to the target's permission and the
        current page's unsaved work.

        Returns True when the move happened. Hiding a rail item is a
        courtesy; this is the control — a direct call for a destination the
        signed-in user may not open is refused here too.
        """
        page = self.pages.get(key)
        if page is None:
            return False
        current = self.stack.currentWidget()

        may_open = getattr(page, "may_open", None)
        if callable(may_open) and not may_open():
            self.rail.set_current(self.current_key)
            if user_initiated:
                QMessageBox.information(
                    self, "Not available",
                    f"{getattr(page, 'title', key)} needs a permission this "
                    f"account does not hold. An administrator grants it on "
                    f"the role.")
            return False

        if page is not current:
            can_leave = getattr(current, "can_leave", None)
            if callable(can_leave) and not can_leave():
                self.rail.set_current(self.current_key)
                return False

        changed = page is not current
        self.rail.set_current(key)
        self.stack.setCurrentWidget(page)
        # Page navigation is deliberately not audited — see auth.AUDITED_ACTIONS.
        self.current_key = key
        on_shown = getattr(page, "on_shown", None)
        if callable(on_shown):
            on_shown()
        if changed:
            self.fade_in(page)
        return True

    def fade_in(self, page: QWidget) -> None:
        """Bring the incoming page up over 130 ms.

        Two dense screens swapped in a single frame read as a flicker rather
        than as a move. The effect is taken off again the moment the fade
        ends: leaving one attached costs an off-screen repaint of the whole
        page on every subsequent update.
        """
        if not self.isVisible():
            return
        previous = getattr(self, "_fade_anim", None)
        if previous is not None:
            previous.stop()
        previous_page = getattr(self, "_fade_page", None)
        if previous_page is not None:
            previous_page.setGraphicsEffect(None)

        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(self.FADE_MS)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(
            lambda p=page: QTimer.singleShot(0, lambda: self.clear_page_effect(p)))
        self._fade_anim = anim
        self._fade_page = page
        anim.start()

    def clear_page_effect(self, page: QWidget) -> None:
        """Detach the opacity effect, a tick after the fade ends.

        Deferring is not decoration: the effect is the animation's own target
        object, and destroying it from inside `finished` is not honoured — the
        effect stays attached and every later repaint of that page goes
        through an off-screen buffer for nothing.
        """
        page.setGraphicsEffect(None)
        if getattr(self, "_fade_page", None) is page:
            self._fade_page = None

    # ── theming ───────────────────────────────────────────────────────
    def apply_permissions(self) -> None:
        """Show only the destinations this account may actually open.

        Driven by each page's own `may_open`, so a new gated destination is
        picked up without touching the rail. This is presentation only —
        `navigate` refuses regardless of what is visible.
        """
        for key, page in self.pages.items():
            may_open = getattr(page, "may_open", None)
            if callable(may_open):
                self.rail.set_item_visible(key, bool(may_open()))

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

    def remember_rail_state(self, expanded: bool) -> None:
        """The rail's mode is a preference, not a session state — it survives
        the window closing."""
        self.ctx.rail_expanded = expanded
        try:
            store.set_setting(self.ctx.con, "rail_expanded", "1" if expanded else "0")
        except sqlite3.Error:
            pass

    def toggle_maximised(self) -> None:
        self.showNormal() if self.isMaximized() else self.showMaximized()

    # ── native window behaviour ───────────────────────────────────────
    def showEvent(self, event):
        """Re-assert the frame styles: Qt rebuilds the native window on some
        flag and screen changes, and takes them off again when it does."""
        super().showEvent(event)
        nativewindow.restore_native_frame(self)

    def nativeEvent(self, event_type, message):
        handled = nativewindow.handle_native_event(self, event_type, message)
        return handled if handled is not None else super().nativeEvent(
            event_type, message)

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
        current = self.stack.currentWidget()
        can_leave = getattr(current, "can_leave", None)
        if callable(can_leave) and not can_leave():
            event.ignore()
            return
        if self.ctx.user is not None:
            try:
                auth.logout(self.ctx.con, self.ctx.user)
            except sqlite3.Error:
                pass
        super().closeEvent(event)


# ── bootstrap ───────────────────────────────────────────────────────────

def corpus_context(manuals: list[dict]) -> str:
    """Describe the whole loaded corpus, not one manual out of it.

    This line used to be `next(m for m in manuals if m["is_current"])` — the
    first current manual in sort order, rendered as though it were the corpus.
    Two consequences, both wrong:

    * It named a single **aircraft type** in the application chrome, which
      makes a multi-type tool look like a single-type one.
    * It **hid every other manual**. With a 737-8 AMM and a 737-8 FIM both
      current, AMM sorted first and the FIM vanished from the interface —
      5,768 of the 8,194 task locators, 70% of the corpus, silently unnamed.

    So the line scales with what is actually loaded, and never implies a
    narrower or wider corpus than exists.
    """
    current = [m for m in manuals if m["is_current"]]
    if not current:
        return "No manual corpus loaded"

    types = sorted({m["aircraft_type"] for m in current if m["aircraft_type"]})
    kinds = sorted({m["manual_type"] for m in current if m["manual_type"]})

    if len(types) == 1 and len(current) == 1:
        one = current[0]
        return (f"{one['aircraft_type']}  ·  {one['manual_type']} "
                f"Rev {one['revision']}  ·  issued "
                f"{one['revision_date'] or '—'}")
    if len(types) == 1:
        return (f"{types[0]}  ·  {' + '.join(kinds)}  ·  "
                f"{len(current)} current manuals")
    scope = (f"{len(types)} aircraft types" if len(types) > 3
             else ", ".join(types))
    return f"{scope}  ·  {' + '.join(kinds)}  ·  {len(current)} current manuals"


def build_context(db_path: Path | None = None) -> AppContext:
    """Open the app database, seed accounts, verify the audit chain."""
    con = db.connect(db_path)
    auth.seed(con)
    # Additive questionnaire schema, applied once here rather than from a
    # widget: schema writes belong to startup, not to painting or refresh.
    # It creates no queue rows, clears no labels and resets no done flags.
    goldreview.migrate(con)
    # `manual.doc_class` — training material must be distinguishable
    # from maintenance data by a column, not by its filename.
    documents.migrate(con)
    ok, rows = audit.verify_chain(con)
    corpus_path = Path(db_path) if db_path else config.DB_PATH
    corpus = store.CorpusReader(store.open_readonly(corpus_path))
    return AppContext(
        con=con,
        corpus=corpus,
        db_path=corpus_path,
        theme_name=store.get_setting(con, "theme", T.DEFAULT_THEME),
        online_enabled=store.online_enabled(con),
        rail_expanded=store.get_setting(con, "rail_expanded", "1") in ("1", "true", "True"),
        chain_ok=ok,
        chain_rows=rows,
    )


def application_icon() -> QIcon:
    """The app mark, at the sizes Windows asks for.

    Built from both marks rather than one: `mark-small-*` drops the pitch
    rungs and the bank pointer, which below about 24 px are noise rather than
    detail. Windows picks a different size for the taskbar, the Alt-Tab list
    and the window corner, so all of them are supplied.
    """
    icon = QIcon()
    small = config.ASSETS_DIR / "icons" / "mark-small-light.svg"
    full = config.ASSETS_DIR / "icons" / "mark-light.svg"
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        source = small if size <= 24 and small.exists() else full
        if source.exists():
            icon.addPixmap(svg_pixmap(source, size))
    return icon


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
    # Both halves, or the taskbar keeps showing the interpreter's icon.
    nativewindow.set_taskbar_identity()
    app.setWindowIcon(application_icon())

    ctx = build_context(db_path)
    app.setStyleSheet(fonts.qss(ctx.theme_name))

    dialog = LoginDialog(ctx.con, ctx.theme_name)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return 0
    ctx.user = dialog.user

    window = MainWindow(ctx)
    # Maximised, not borderless-fullscreen: the owner asked for "full screen"
    # on a tool that is read next to other windows, and a shell with no
    # taskbar and no way out is not that (R6).
    window.showMaximized()
    return app.exec()
