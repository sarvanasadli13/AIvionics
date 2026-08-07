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
from .widgets import Placard, StatusBadge, svg_pixmap, ui_font


class LoginDialog(QDialog):
    def __init__(self, con: sqlite3.Connection, theme: str = T.DEFAULT_THEME,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.con = con
        self.theme_name = theme
        self.user: auth.User | None = None

        self.setWindowTitle("AIvionics — Sign in")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setFixedSize(430, 470)
        self.setStyleSheet(fonts.qss(theme))

        pal = T.THEMES[theme]
        frame = QFrame(self)
        frame.setObjectName("Card")
        frame.setGeometry(0, 0, 430, 470)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(38, 34, 38, 30)
        lay.setSpacing(0)

        mark = QLabel()
        mark_path = config.ASSETS_DIR / "icons" / f"mark-{theme}.svg"
        if mark_path.exists():
            mark.setPixmap(svg_pixmap(mark_path, 54, self.devicePixelRatioF()))
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(mark)
        lay.addSpacing(13)

        name = QLabel(
            f'<span style="color:{pal["cy"]};font-weight:800">A'
            f'<span style="font-family:Georgia,serif">I</span></span>'
            f'<span style="color:{pal["txt"]}">vionics</span>')
        name.setFont(ui_font(21, QFont.Weight.DemiBold))
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(name)

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
