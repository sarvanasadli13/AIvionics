"""Login dialog (PLAN 4.2), including secure first-run administration.

A fresh database contains an unclaimed local administrator whose password is
an unexposed random value.  This dialog detects that one-time state and opens
the password-creation panel directly.  No shared bootstrap credential is
published or accepted, and no session is returned until the operator chooses
a real password.

**Forgetting the password (BACKLOG round 2, R8).** There is no mail server
here and there may be exactly one admin, so neither of the usual answers —
a reset link, or "ask another administrator" — works. Instead every account
is handed a one-time recovery code the moment it sets a real password, shown
once and stored only as a hash. That code is what the *Forgot password* panel
spends. It is a second credential to keep safe, which is the honest cost;
the alternative on a single-admin machine is no way back in at all.
"""
from __future__ import annotations

import sqlite3

import qtawesome as qta
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from .. import config
from . import auth, fonts, nativewindow
from . import theme as T
from .widgets import (MonoLabel, Placard, StatusBadge, svg_pixmap_wide,
                      ui_font)


class LoginDialog(QDialog):
    """Frameless, but never trapping: minimise and close are always present."""

    def _window_buttons(self, pal: dict) -> QHBoxLayout:
        """Minimise and close, drawn the same way the main shell draws them.

        These were text glyphs and they rendered as nothing at all. A
        per-widget stylesheet *cascades* with the global one rather than
        replacing it, so the global `QPushButton { padding: 7px 15px }`
        applied here too, and 15 px of padding on each side of a 30 px-wide
        button leaves a content rectangle exactly 0 px across: the label was
        laid out into nothing. Hence both changes below. The icons are what
        the shell's own window buttons use (TitleBar, `#WinBtn`) and Qt
        centres an icon in the whole button rather than the content box, so
        they survive the cascade; `padding: 0px` removes the cause anyway, as
        `#WinBtn` does, so a future edit back to text cannot re-break it.
        """
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        row.addStretch(1)
        for glyph, name, slot, hover in (
                ("mdi6.window-minimize", "Minimise", self.showMinimized, pal["s3"]),
                ("mdi6.close", "Close", self.reject, pal["closehover"])):
            button = QPushButton()
            # txt2, not txt3: at this size on a white card the faint tone is
            # invisible, which defeats the point of putting them there.
            button.setIcon(qta.icon(glyph, color=pal["txt2"]))
            button.setIconSize(QSize(13, 13))
            button.setFixedSize(30, 24)
            button.setToolTip(name)
            button.setAccessibleName(name)
            button.setCursor(Qt.CursorShape.ArrowCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(slot)
            button.setStyleSheet(
                f"QPushButton{{border:none;background:transparent;padding:0px;"
                f"border-radius:3px;}}"
                f"QPushButton:hover{{background:{hover};}}")
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
        nativewindow.restore_native_frame(self)
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
            mark.setPixmap(svg_pixmap_wide(lockup, 210, self.devicePixelRatioF()))
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
        self.stack.addWidget(self._sign_in_panel())        # 0
        self.stack.addWidget(self._change_panel())         # 1
        self.stack.addWidget(self._recovery_panel())       # 2
        self.stack.addWidget(self._code_panel())           # 3
        lay.addWidget(self.stack)
        lay.addStretch(1)

        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setFont(ui_font(8.5))
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setMinimumHeight(30)
        lay.addWidget(self.message)

        # Reachable before sign-in: the product identity, safety posture and
        # licence attributions should not require an account to read.
        about_row = QHBoxLayout()
        about_row.addStretch(1)
        self.about_link = QPushButton("About AIvionics")
        self.about_link.setObjectName("LinkBtn")
        self.about_link.setFlat(True)
        self.about_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.about_link.setFont(ui_font(8.5))
        self.about_link.clicked.connect(self.show_about)
        about_row.addWidget(self.about_link)
        about_row.addStretch(1)
        lay.addLayout(about_row)

        foot = QLabel("Decision support — not part of the maintenance record")
        foot.setObjectName("Faint")
        foot.setFont(ui_font(7.5))
        foot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(foot)

        # A virgin database has no reusable setup credential.  The person at
        # this local first-run screen claims the one seeded administrator by
        # choosing its real password; subsequent launches start at sign-in.
        first_run = auth.unclaimed_setup_user(self.con)
        if first_run is not None:
            self.user = first_run
            self.setWindowTitle("AIvionics — Create administrator")
            self.stack.setCurrentIndex(1)
            self._say("Create the administrator password to continue.", "info")
            self.new_pw.setFocus()

    def show_about(self) -> None:
        """The same About page the application shows, in a dialog.

        Built from `AboutPage` rather than a second copy of the text, so the
        two can never disagree. It is given a context with no database — the
        page is written to report "Not available" for anything it cannot
        read, which is exactly the pre-sign-in situation.
        """
        from PySide6.QtWidgets import QDialog, QVBoxLayout as _V
        from .pages.about import AboutPage

        class _Ctx:
            theme_name = self.theme_name
            con = getattr(self, "con", None)
            corpus = None
            user = None
            online_enabled = None
            window = None

        dialog = QDialog(self)
        dialog.setWindowTitle("About AIvionics")
        dialog.setStyleSheet(fonts.qss(self.theme_name))
        dialog.resize(880, 720)
        box = _V(dialog)
        box.setContentsMargins(0, 0, 0, 0)
        page = AboutPage(_Ctx())
        page.on_shown()
        box.addWidget(page)
        dialog.exec()

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

        forgot_row = QHBoxLayout()
        forgot_row.addStretch(1)
        forgot = QToolButton()
        forgot.setObjectName("LinkBtn")
        forgot.setText("Forgot password?")
        forgot.setCursor(Qt.CursorShape.PointingHandCursor)
        forgot.setFont(ui_font(8.5, QFont.Weight.DemiBold))
        forgot.clicked.connect(self.open_recovery)
        forgot_row.addWidget(forgot)
        forgot_row.addStretch(1)
        lay.addLayout(forgot_row)
        return panel

    def _change_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(StatusBadge("warn", "FIRST-RUN SETUP", self.theme_name))
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

    def _recovery_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        note = QLabel(
            "Enter the recovery code issued when this account set its "
            "password, and choose a new one. The code is single-use \u2014 a "
            "fresh one is issued in its place.")
        note.setObjectName("Muted")
        note.setFont(ui_font(8.5))
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addSpacing(6)

        lay.addWidget(Placard("Username"))
        self.rec_username = QLineEdit()
        self.rec_username.setMinimumHeight(30)
        lay.addWidget(self.rec_username)

        lay.addWidget(Placard("Recovery code"))
        self.rec_code = QLineEdit()
        self.rec_code.setMinimumHeight(30)
        self.rec_code.setPlaceholderText("XXXXX-XXXXX-XXXXX-XXXXX")
        lay.addWidget(self.rec_code)

        lay.addWidget(Placard("New password"))
        self.rec_pw = QLineEdit()
        self.rec_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.rec_pw.setMinimumHeight(30)
        lay.addWidget(self.rec_pw)

        lay.addWidget(Placard("Confirm"))
        self.rec_confirm = QLineEdit()
        self.rec_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.rec_confirm.setMinimumHeight(30)
        self.rec_confirm.returnPressed.connect(self.attempt_recovery)
        lay.addWidget(self.rec_confirm)
        lay.addSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(8)
        back = QPushButton("Back")
        back.setMinimumHeight(34)
        back.clicked.connect(self.close_recovery)
        row.addWidget(back)
        go = QPushButton("Reset password")
        go.setObjectName("Primary")
        go.setMinimumHeight(34)
        go.clicked.connect(self.attempt_recovery)
        row.addWidget(go, 1)
        lay.addLayout(row)
        return panel

    def _code_panel(self) -> QWidget:
        """Shown once, and there is no way to ask for it again."""
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(StatusBadge("warn", "WRITE THIS DOWN", self.theme_name))
        row.addStretch(1)
        lay.addLayout(row)
        lay.addSpacing(10)

        note = QLabel(
            "This is the only time this code is shown. It is the way back in "
            "if the password is forgotten, and it is stored hashed \u2014 "
            "nobody, including this application, can read it back.")
        note.setObjectName("Muted")
        note.setFont(ui_font(8.5))
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addSpacing(12)

        self.code_value = MonoLabel("", 13)
        self.code_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_value.setMinimumHeight(38)
        lay.addWidget(self.code_value)
        lay.addSpacing(14)

        copy = QToolButton()
        copy.setObjectName("LinkBtn")
        copy.setText("Copy to clipboard")
        copy.setCursor(Qt.CursorShape.PointingHandCursor)
        copy.setFont(ui_font(8.5, QFont.Weight.DemiBold))
        copy.clicked.connect(self._copy_code)
        row2 = QHBoxLayout()
        row2.addStretch(1)
        row2.addWidget(copy)
        row2.addStretch(1)
        lay.addLayout(row2)
        lay.addSpacing(10)

        done = QPushButton("I have written it down")
        done.setObjectName("Primary")
        done.setMinimumHeight(37)
        done.clicked.connect(self.accept)
        lay.addWidget(done)
        return panel

    # ── behaviour ─────────────────────────────────────────────────────
    def _copy_code(self) -> None:
        from PySide6.QtWidgets import QApplication
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.code_value.text())
            self._say("Copied. It is still worth writing down.", "info")

    def open_recovery(self) -> None:
        self.rec_username.setText(self.username.text())
        self.stack.setCurrentIndex(2)
        self._say("", "info")
        (self.rec_code if self.rec_username.text() else
         self.rec_username).setFocus()

    def close_recovery(self) -> None:
        for field in (self.rec_code, self.rec_pw, self.rec_confirm):
            field.clear()
        self.stack.setCurrentIndex(0)
        self._say("", "info")
        self.password.setFocus()

    def attempt_recovery(self) -> None:
        if self.rec_pw.text() != self.rec_confirm.text():
            self._say("The two passwords do not match.")
            return
        try:
            self.user, code = auth.reset_with_recovery(
                self.con, self.rec_username.text(), self.rec_code.text(),
                self.rec_pw.text())
        except ValueError as exc:
            self._say(str(exc))
            self.rec_code.clear()
            self.rec_code.setFocus()
            return
        self._show_code(code, "Password reset.")

    def _show_code(self, code: str, headline: str) -> None:
        self.code_value.setText(code)
        self.stack.setCurrentIndex(3)
        self._say(headline, "info")

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
            self._say("This account needs a new password before continuing.",
                      "info")
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
        # The one moment an account is guaranteed to be at the keyboard with
        # a password it just chose is the moment to hand it the way back in.
        try:
            code = auth.issue_recovery_code(self.con, self.user)
        except sqlite3.Error:
            self.accept()
            return
        self._show_code(code, "Password set.")
