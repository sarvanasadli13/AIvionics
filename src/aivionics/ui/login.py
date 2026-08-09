"""Login dialog (PLAN 4.2), including the forced first-password change.

The setup account exists so a fresh install can be opened at all. It ships
with `must_change_pw = 1`, and this dialog will not hand back a session
until that flag is cleared — a documented default password that survives
first login is the single most common way a departmental tool ends up with
a shared account.
"""
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from .. import config
from . import auth, fonts
from . import theme as T
from .widgets import (Placard, StatusBadge, svg_pixmap_wide, ui_font)


class LoginDialog(QDialog):
    """Frameless, but never trapping: minimise and close are always present."""

    def _window_buttons(self, pal: dict) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        row.addStretch(1)
        for glyph, tip, slot, hover in (
                ("–", "Minimise", self.showMinimized, pal["s3"]),
                ("✕", "Close", self.reject, pal["closehover"])):
            button = QPushButton(glyph)
            button.setFlat(True)
            button.setFixedSize(30, 24)
            button.setToolTip(tip)
            button.setCursor(Qt.CursorShape.ArrowCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(slot)
            # txt2, not txt3: at 13 px on a white card the faint tone is
            # invisible, which defeats the point of putting them there.
            button.setStyleSheet(
                f"QPushButton{{border:none;background:transparent;"
                f"color:{pal['txt2']};font-size:15px;font-weight:600;"
                f"border-radius:3px;}}"
                f"QPushButton:hover{{background:{hover};color:{pal['txt']};}}")
            row.addWidget(button)
        return row

    def mousePressEvent(self, event):
        """Frameless windows are not draggable unless we move them ourselves."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        origin = getattr(self, "_drag_from", None)
        if origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - origin)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_from = None
        super().mouseReleaseEvent(event)

    def __init__(self, con: sqlite3.Connection, theme: str = T.DEFAULT_THEME,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.con = con
        self.theme_name = theme
        self.user: auth.User | None = None

        self.setWindowTitle("AIvionics — Sign in")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        # Without Window in the flags a frameless dialog has no taskbar entry,
        # and showMinimized() has nowhere to minimise to.
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setFixedSize(430, 470)
        self.setStyleSheet(fonts.qss(theme))

        pal = T.THEMES[theme]
        frame = QFrame(self)
        frame.setObjectName("Card")
        frame.setGeometry(0, 0, 430, 470)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(38, 12, 38, 30)
        lay.setSpacing(0)

        # A frameless dialog has no system buttons, so someone who cannot sign
        # in has no visible way out except Task Manager. Escape already
        # rejected the dialog; this makes that possible to discover.
        lay.addLayout(self._window_buttons(pal))
        lay.addSpacing(10)

        # The approved horizontal lockup (logo-sheet.html: "Final. Concept B"),
        # not the square tile plus a retyped wordmark. The tile is the icon
        # form — using it here and setting the name in live text beside it gave
        # two versions of the logo on one screen, and the serifed capital I
        # that the lockup exists to guarantee was being reproduced by hand.
        mark = QLabel()
        lockup = config.ASSETS_DIR / "icons" / f"logo-{theme}.svg"
        if lockup.exists():
            mark.setPixmap(svg_pixmap_wide(lockup, 268, self.devicePixelRatioF()))
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(mark)
        lay.addSpacing(4)

        tagline = QLabel("Reliability analysis and manual retrieval")
        tagline.setObjectName("Muted")
        tagline.setFont(ui_font(9))
        tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(tagline)
        lay.addSpacing(24)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._sign_in_panel())
        self.stack.addWidget(self._change_panel())
        lay.addWidget(self.stack)
        lay.addStretch(1)

        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setFont(ui_font(8.5))
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setMinimumHeight(30)
        lay.addWidget(self.message)

        foot = QLabel("Decision support — not part of the maintenance record")
        foot.setObjectName("Faint")
        foot.setFont(ui_font(7.5))
        foot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(foot)

    # ── panels ────────────────────────────────────────────────────────
    def _sign_in_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        lay.addWidget(Placard("Username"))
        self.username = QLineEdit()
        self.username.setMinimumHeight(34)
        self.username.setAccessibleName("Username")
        lay.addWidget(self.username)
        lay.addSpacing(6)

        lay.addWidget(Placard("Password"))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setMinimumHeight(34)
        self.password.setAccessibleName("Password")
        self.password.returnPressed.connect(self.attempt_login)
        lay.addWidget(self.password)
        lay.addSpacing(16)

        btn = QPushButton("Sign in")
        btn.setObjectName("Primary")
        btn.setMinimumHeight(37)
        btn.setDefault(True)
        btn.clicked.connect(self.attempt_login)
        lay.addWidget(btn)
        return panel

    def _change_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(StatusBadge("warn", "SETUP PASSWORD", self.theme_name))
        row.addStretch(1)
        lay.addLayout(row)
        lay.addSpacing(8)

        note = QLabel("Choose a new password before continuing. "
                      "Minimum 10 characters.")
        note.setObjectName("Muted")
        note.setFont(ui_font(8.5))
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addSpacing(8)

        lay.addWidget(Placard("New password"))
        self.new_pw = QLineEdit()
        self.new_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.new_pw.setMinimumHeight(34)
        lay.addWidget(self.new_pw)
        lay.addSpacing(6)

        lay.addWidget(Placard("Confirm"))
        self.confirm_pw = QLineEdit()
        self.confirm_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_pw.setMinimumHeight(34)
        self.confirm_pw.returnPressed.connect(self.attempt_change)
        lay.addWidget(self.confirm_pw)
        lay.addSpacing(14)

        btn = QPushButton("Set password and continue")
        btn.setObjectName("Primary")
        btn.setMinimumHeight(37)
        btn.clicked.connect(self.attempt_change)
        lay.addWidget(btn)
        return panel

    # ── behaviour ─────────────────────────────────────────────────────
    def _say(self, text: str, kind: str = "alert") -> None:
        pal = T.THEMES[self.theme_name]
        colour = {"alert": pal["red"], "info": pal["txt2"]}[kind]
        self.message.setStyleSheet(f"color:{colour};")
        self.message.setText(text)

    def attempt_login(self) -> None:
        result = auth.authenticate(self.con, self.username.text(),
                                   self.password.text())
        if not result.ok:
            self._say(result.reason)
            self.password.clear()
            self.password.setFocus()
            return
        self.user = result.user
        if result.user.must_change_pw:
            self._say("This account still uses the setup password.", "info")
            self.stack.setCurrentIndex(1)
            self.new_pw.setFocus()
            return
        self.accept()

    def attempt_change(self) -> None:
        if self.new_pw.text() != self.confirm_pw.text():
            self._say("The two passwords do not match.")
            return
        try:
            self.user = auth.change_password(self.con, self.user, self.new_pw.text())
        except ValueError as exc:
            self._say(str(exc))
            return
        self.accept()
