"""Shared widgets — the Qt port of the approved mockup's vocabulary.

Anything that appears on more than one page lives here so the identity is
defined once: the placard label, the ATA locator plate, the status badge
(icon + word + colour, never colour alone), the provenance line, and the
frameless shell furniture.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import qtawesome as qta
from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QSplitter, QSplitterHandle,
                               QToolButton, QVBoxLayout, QWidget)

from .. import config
from . import theme as T

# ── fonts ───────────────────────────────────────────────────────────────


def _with_tabular_numerals(font: QFont) -> QFont:
    """Enable the OpenType `tnum` feature where the Qt build supports it.

    Digits that do not change width are the difference between a column of
    numbers you can scan and one you have to read.
    """
    try:
        font.setFeature(QFont.Tag("tnum"), 1)
    except Exception:
        pass
    return font


def mono_font(size: int = 12, weight: QFont.Weight = QFont.Weight.DemiBold) -> QFont:
    f = QFont()
    f.setFamilies(T.MONO_FAMILIES)
    f.setPointSizeF(size)
    f.setWeight(weight)
    return _with_tabular_numerals(f)


def ui_font(size: int = 10, weight: QFont.Weight = QFont.Weight.Normal,
            tabular: bool = False) -> QFont:
    f = QFont()
    f.setFamilies(T.UI_FAMILIES)
    f.setPointSizeF(size)
    f.setWeight(weight)
    return _with_tabular_numerals(f) if tabular else f


def svg_pixmap(path: Path, size: int, ratio: float = 1.0) -> QPixmap:
    """Rasterise an SVG asset at `size` logical pixels."""
    px = QPixmap(int(size * ratio), int(size * ratio))
    px.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(str(path))
    if renderer.isValid():
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(p)
        p.end()
    px.setDevicePixelRatio(ratio)
    return px


# ── small building blocks ───────────────────────────────────────────────

class Placard(QLabel):
    """Cockpit-engraving label: tiny, letter-spaced, uppercase."""

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text.upper(), parent)
        self.setObjectName("Placard")
        f = ui_font(7, QFont.Weight.DemiBold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.1)
        self.setFont(f)


class MonoLabel(QLabel):
    def __init__(self, text: str = "", size: int = 11,
                 weight: QFont.Weight = QFont.Weight.DemiBold,
                 parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setFont(mono_font(size, weight))


class StatusBadge(QWidget):
    """Icon + word + colour. All three, always — §4A.1 rule 2.

    Colour alone is not permitted to carry status anywhere in this app, so
    this widget does not offer a way to render without the word.
    """

    def __init__(self, kind: str, text: str | None = None,
                 theme: str = T.DEFAULT_THEME, parent: QWidget | None = None):
        super().__init__(parent)
        self.kind = kind
        self.override = text
        lay = QHBoxLayout(self)
        lay.setContentsMargins(7, 2, 8, 2)
        lay.setSpacing(5)
        self.icon = QLabel()
        self.icon.setFixedSize(13, 13)
        self.text = QLabel()
        self.text.setFont(ui_font(8, QFont.Weight.Bold))
        lay.addWidget(self.icon)
        lay.addWidget(self.text)
        self.refresh_theme(theme)

    def refresh_theme(self, theme: str) -> None:
        spec = T.status_style(self.kind, theme)
        self.icon.setPixmap(qta.icon(spec["icon"], color=spec["color"]).pixmap(13, 13))
        self.text.setText(self.override or spec["word"])
        self.text.setStyleSheet(f"color:{spec['color']};")
        self.setStyleSheet(
            f"background:{spec['quiet']};border:1px solid {spec['color']};"
            f"border-radius:3px;")


class Tag(QLabel):
    """Neutral metadata chip — manual name, function class, counts."""

    def __init__(self, text: str, theme: str = T.DEFAULT_THEME,
                 parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setFont(ui_font(8))
        self.refresh_theme(theme)

    def refresh_theme(self, theme: str) -> None:
        pal = T.THEMES[theme]
        self.setStyleSheet(
            f"color:{pal['txt2']};background:{pal['s3']};"
            f"border:1px solid {pal['line']};border-radius:3px;padding:1px 7px;")


class AtaLocator(QWidget):
    """The ATA task number, segmented and colour-coded.

    The chapter is set in the accent and the function code in a quiet wash —
    diagnostic (`-8xx`, `-7xx`, `-2xx`) in the accent, action codes in amber
    — because that distinction is the single most load-bearing fact about a
    task locator (PLAN §5: the 7.5:1 action-to-diagnostic skew is the core
    problem this product exists to work around).
    """

    DIAGNOSTIC_PREFIXES = ("2", "7", "8")

    def __init__(self, task_number: str, theme: str = T.DEFAULT_THEME,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.task_number = task_number
        self.label = QLabel(self)
        self.label.setFont(mono_font(12))
        self.label.setTextFormat(Qt.TextFormat.RichText)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.label)
        lay.addStretch(1)
        self.refresh_theme(theme)

    @classmethod
    def is_diagnostic(cls, function_code: str | None) -> bool:
        return bool(function_code) and function_code[0] in cls.DIAGNOSTIC_PREFIXES

    def refresh_theme(self, theme: str) -> None:
        pal = T.THEMES[theme]
        parts = self.task_number.split("-")
        sep = f'<span style="color:{pal["txt3"]}">-</span>'
        if len(parts) < 4:
            self.label.setText(
                f'<span style="color:{pal["txt"]}">{self.task_number}</span>')
            return
        chapter, rest, func = parts[0], parts[1:3], parts[3]
        func_color = pal["cy"] if self.is_diagnostic(func) else pal["amb"]
        func_bg = pal["cyq"] if self.is_diagnostic(func) else pal["ambq"]
        html = (f'<span style="color:{pal["cy"]}">{chapter}</span>' + sep
                + sep.join(f'<span style="color:{pal["txt"]}">{p}</span>' for p in rest)
                + sep
                + f'<span style="color:{func_color};background:{func_bg}">'
                  f'&nbsp;{func}&nbsp;</span>')
        for tail in parts[4:]:
            html += sep + f'<span style="color:{pal["txt"]}">{tail}</span>'
        self.label.setText(html)


class ProvenanceLine(QLabel):
    """Source system · import time · staleness — standing rule 2.

    No compliance or statistics figure may render without one of these
    visible, so it is a widget rather than a formatting convention.
    """

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("Provenance")
        self.setFont(ui_font(8))
        self.setWordWrap(True)


class SectionHeader(QWidget):
    """The 32 px column header bar from the mockup."""

    def __init__(self, title: str, right: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("PageHeader")
        self.setFixedHeight(32)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(15, 0, 15, 0)
        lay.setSpacing(10)
        self.title = QLabel(title.upper())
        self.title.setObjectName("PageTitle")
        f = ui_font(7.5, QFont.Weight.Bold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        self.title.setFont(f)
        lay.addWidget(self.title)
        lay.addStretch(1)
        self.right = QLabel(right)
        self.right.setObjectName("Faint")
        self.right.setFont(ui_font(8))
        lay.addWidget(self.right)

    def set_right(self, text: str) -> None:
        self.right.setText(text)


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")


class EmptyState(QWidget):
    """What a page shows when the corpus is not there yet.

    Deliberately explicit about *what* is missing and *what to run*, because
    the alternative — a blank table — reads as a bug.
    """

    def __init__(self, icon: str, headline: str, detail: str,
                 theme: str = T.DEFAULT_THEME, parent: QWidget | None = None):
        super().__init__(parent)
        self._icon_name = icon
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 46, 40, 46)
        lay.setSpacing(11)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.headline = QLabel(headline)
        self.headline.setFont(ui_font(11, QFont.Weight.DemiBold))
        self.headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail = QLabel(detail)
        self.detail.setObjectName("Muted")
        self.detail.setFont(ui_font(9))
        self.detail.setWordWrap(True)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # A word-wrapped QLabel reports a single-line sizeHint unless the
        # layout is told to ask heightForWidth, which silently clips the last
        # lines. Fixing the width makes the height calculable and honoured.
        self.detail.setFixedWidth(480)
        self.detail.setMinimumHeight(
            self.detail.fontMetrics().boundingRect(
                0, 0, 480, 1000, int(Qt.TextFlag.TextWordWrap), detail).height() + 4)
        lay.addWidget(self.icon)
        lay.addWidget(self.headline)
        lay.addWidget(self.detail, 0, Qt.AlignmentFlag.AlignHCenter)
        self.refresh_theme(theme)

    def refresh_theme(self, theme: str) -> None:
        pal = T.THEMES[theme]
        self.icon.setPixmap(qta.icon(self._icon_name, color=pal["txt3"]).pixmap(34, 34))
        self.headline.setStyleSheet(f"color:{pal['txt2']};")


# ── world clock (PLAN 4.8: IANA zones, never fixed offsets) ─────────────

WORLD_CLOCK_ZONES: list[tuple[str, str]] = [
    ("ZULU", "UTC"),
    ("BER", "Europe/Berlin"),
    ("LHR", "Europe/London"),
    ("GYD", "Asia/Baku"),
    ("DXB", "Asia/Dubai"),
    ("SEA", "America/Los_Angeles"),
]


class WorldClock(QWidget):
    """Six-city strip, ticking every second.

    Zones are IANA names resolved through `zoneinfo` at render time, so DST
    transitions are handled by the tz database rather than by us.
    """

    def __init__(self, zones=None, theme: str = T.DEFAULT_THEME,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.zones = zones or WORLD_CLOCK_ZONES
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.cells: list[tuple[QLabel, QLabel, QLabel, str]] = []
        for i, (code, zone) in enumerate(self.zones):
            cell = QWidget()
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(16, 6, 16, 6)
            cl.setSpacing(1)
            name = Placard(code)
            name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            clock = MonoLabel("--:--:--", 15)
            clock.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            day = QLabel("")
            day.setObjectName("Faint")
            day.setFont(ui_font(7.5))
            day.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            cl.addWidget(name)
            cl.addWidget(clock)
            cl.addWidget(day)
            lay.addWidget(cell)
            if i < len(self.zones) - 1:
                sep = QFrame()
                sep.setObjectName("Hairline")
                sep.setFixedWidth(1)
                lay.addWidget(sep)
            self.cells.append((name, clock, day, zone))
        lay.addStretch(1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.tick()

    def tick(self) -> None:
        for _, clock, day, zone in self.cells:
            try:
                now = datetime.now(ZoneInfo(zone))
            except Exception:
                clock.setText("--:--:--")
                day.setText("zone unavailable")
                continue
            clock.setText(now.strftime("%H:%M:%S"))
            day.setText(now.strftime("%a %d %b"))

    def refresh_theme(self, theme: str) -> None:
        pal = T.THEMES[theme]
        for _, clock, _, _ in self.cells:
            clock.setStyleSheet(f"color:{pal['txt']};")


# ── frameless shell furniture ───────────────────────────────────────────

class TitleBar(QWidget):
    """Custom title bar: mark, wordmark, context, theme toggle, window buttons."""

    minimise_requested = Signal()
    maximise_requested = Signal()
    close_requested = Signal()
    theme_changed = Signal(str)

    HEIGHT = 58

    def __init__(self, theme: str = T.DEFAULT_THEME, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(self.HEIGHT)
        self._theme = theme

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 0, 0)
        lay.setSpacing(14)

        self.mark = QLabel()
        self.mark.setFixedSize(36, 36)
        lay.addWidget(self.mark)

        # "AI" in the accent with a serif capital I — in Segoe UI a capital I
        # and a lowercase l are the same glyph, so without the serif the name
        # reads "Alvionics" and the AI disappears (assets/LICENSES.md).
        self.name = QLabel()
        self.name.setObjectName("AppName")
        self.name.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.name)

        self.context = QLabel("")
        self.context.setObjectName("TitleContext")
        lay.addWidget(self.context)
        lay.addStretch(1)

        self.badge = QLabel("OFFLINE")
        self.badge.setObjectName("TitleBadge")
        self.badge.setFont(ui_font(7.5, QFont.Weight.DemiBold))
        self.badge.setFixedHeight(22)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)

        toggle = QFrame()
        toggle.setObjectName("ThemeToggle")
        toggle.setFixedHeight(26)
        tl = QHBoxLayout(toggle)
        tl.setContentsMargins(2, 2, 2, 2)
        tl.setSpacing(0)
        self.light_btn = QToolButton()
        self.dark_btn = QToolButton()
        for btn, name in ((self.light_btn, "light"), (self.dark_btn, "dark")):
            btn.setObjectName("ThemeBtn")
            btn.setCheckable(True)
            btn.setFixedSize(30, 20)
            btn.setIconSize(QSize(13, 13))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(f"{name.capitalize()} theme")
            btn.clicked.connect(lambda _=False, n=name: self.theme_changed.emit(n))
            tl.addWidget(btn)
        lay.addWidget(toggle, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addSpacing(8)

        self.win_buttons: list[QPushButton] = []
        for glyph, slot, name in (
                ("mdi6.window-minimize", self.minimise_requested, "min"),
                ("mdi6.window-maximize", self.maximise_requested, "max"),
                ("mdi6.close", self.close_requested, "close")):
            b = QPushButton()
            b.setObjectName("WinBtnClose" if name == "close" else "WinBtn")
            b.setProperty("glyph", glyph)
            b.setFixedSize(46, self.HEIGHT)
            b.setIconSize(QSize(13, 13))
            b.setCursor(Qt.CursorShape.ArrowCursor)
            b.clicked.connect(slot.emit)
            lay.addWidget(b)
            self.win_buttons.append(b)

        self.refresh_theme(theme)

    def set_context(self, text: str) -> None:
        self.context.setText(text)

    def set_badge(self, text: str) -> None:
        self.badge.setText(text.upper())

    def refresh_theme(self, theme: str) -> None:
        self._theme = theme
        pal = T.THEMES[theme]
        mark = config.ASSETS_DIR / "icons" / f"mark-{theme}.svg"
        if mark.exists():
            self.mark.setPixmap(svg_pixmap(mark, 36, self.devicePixelRatioF()))
        self.name.setText(
            f'<span style="color:{pal["cy"]};font-weight:800">A'
            f'<span style="font-family:Georgia,serif">I</span></span>'
            f'<span style="color:{pal["txt"]}">vionics</span>')
        self.light_btn.setChecked(theme == "light")
        self.dark_btn.setChecked(theme == "dark")
        self.light_btn.setIcon(qta.icon(
            "mdi6.weather-sunny", color=pal["cy"] if theme == "light" else pal["txt3"]))
        self.dark_btn.setIcon(qta.icon(
            "mdi6.weather-night", color=pal["cy"] if theme == "dark" else pal["txt3"]))
        for b in self.win_buttons:
            b.setIcon(qta.icon(b.property("glyph"), color=pal["txt2"]))

    # drag-to-move, and double-click to maximise
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window().windowHandle()
            if window is not None:
                window.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximise_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class Rail(QWidget):
    """Seven destinations plus Admin, pinned bottom and separated (§4A.1 rule 4).

    Ops carries the whole online-dependent surface, so it greys out as one
    item when `online_enabled` is false — standing rule 12 made visible in
    the navigation instead of buried in a settings page.
    """

    navigated = Signal(str)

    WIDTH = 58
    ITEMS = [
        ("home", "Home", "mdi6.home-outline"),
        ("diagnose", "Diagnose", "mdi6.magnify"),
        ("manuals", "Manuals", "mdi6.book-open-outline"),
        ("fleet", "Fleet", "mdi6.airplane"),
        ("reliability", "Reliability", "mdi6.chart-bar"),
        ("compliance", "Compliance", "mdi6.shield-check-outline"),
        ("ops", "Ops", "mdi6.earth"),
    ]
    ADMIN = ("admin", "Admin", "mdi6.cog-outline")

    def __init__(self, theme: str = T.DEFAULT_THEME, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Rail")
        self.setFixedWidth(self.WIDTH)
        self.buttons: dict[str, QToolButton] = {}
        self._current = "home"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 7, 0, 7)
        lay.setSpacing(3)
        lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        for key, label, glyph in self.ITEMS:
            lay.addWidget(self._make_button(key, label, glyph))
        lay.addStretch(1)

        sep = QFrame()
        sep.setObjectName("RailSep")
        sep.setFixedSize(24, 1)
        lay.addWidget(sep, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(4)
        lay.addWidget(self._make_button(*self.ADMIN))

        self.refresh_theme(theme)
        self.set_current("home")

    def _make_button(self, key: str, label: str, glyph: str) -> QToolButton:
        b = QToolButton(self)
        b.setObjectName("RailBtn")
        b.setProperty("glyph", glyph)
        b.setCheckable(True)
        b.setAutoExclusive(True)
        b.setFixedSize(44, 43)
        b.setIconSize(QSize(19, 19))
        b.setToolTip(label)
        b.setAccessibleName(label)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda _=False, k=key: self.navigated.emit(k))
        self.buttons[key] = b
        return b

    def set_current(self, key: str) -> None:
        if key in self.buttons:
            self._current = key
            self.buttons[key].setChecked(True)
            self.refresh_theme(getattr(self, "_theme", T.DEFAULT_THEME))

    def set_online_enabled(self, enabled: bool) -> None:
        """Grey the Ops item when the switch is off — but leave it reachable.

        PLAN §4A.1 rule 4 says this item greys out; PLAN 4B.5 says the airport
        page's offline fields — identifiers, runways, elevation, IANA local
        time — render always. Disabling the button would satisfy the first by
        making the second unreachable, so the item is dimmed and captioned
        rather than switched off. Only the online panels inside go dark.
        """
        self._online = enabled
        ops = self.buttons["ops"]
        ops.setToolTip(
            "Ops — live map, airport page, weather" if enabled else
            "Ops — online features are switched off in Admin; airport data, "
            "runways and local time still work offline")
        self.refresh_theme(getattr(self, "_theme", T.DEFAULT_THEME))

    def refresh_theme(self, theme: str) -> None:
        self._theme = theme
        pal = T.THEMES[theme]
        for key, b in self.buttons.items():
            active = key == self._current
            colour = pal["cy"] if active else pal["txt3"]
            if key == "ops" and not getattr(self, "_online", False) and not active:
                colour = pal["hair"]        # dimmed, still clickable
            b.setIcon(qta.icon(b.property("glyph"), color=colour,
                               color_disabled=pal["txt3"]))
        self.update()

    def paintEvent(self, event):
        """Draw the active-item marker: a bar on the rail edge."""
        super().paintEvent(event)
        btn = self.buttons.get(self._current)
        if btn is None or not btn.isChecked():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = T.THEMES[getattr(self, "_theme", T.DEFAULT_THEME)]
        p.setBrush(QColor(pal["cyf"]))
        p.setPen(Qt.PenStyle.NoPen)
        top = btn.y() + 9
        p.drawRoundedRect(QRect(0, top, 3, btn.height() - 18), 1.5, 1.5)
        p.end()


class _HairlineHandle(QSplitterHandle):
    """A splitter handle that is exactly one hairline wide.

    The platform style paints a grip mark in the middle of the handle, which
    at this weight reads as a stray dash floating between two panels. Drawing
    it ourselves is the only reliable way to suppress it.
    """

    def paintEvent(self, event):
        splitter = self.splitter()
        theme = getattr(splitter, "theme_name", T.DEFAULT_THEME)
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(T.THEMES[theme]["line"]))
        p.end()


class Splitter(QSplitter):
    """QSplitter with hairline handles that follow the theme."""

    def __init__(self, orientation: Qt.Orientation, theme: str = T.DEFAULT_THEME,
                 parent: QWidget | None = None):
        super().__init__(orientation, parent)
        self.theme_name = theme
        self.setHandleWidth(1)
        self.setChildrenCollapsible(False)

    def createHandle(self) -> QSplitterHandle:
        return _HairlineHandle(self.orientation(), self)

    def refresh_theme(self, theme: str) -> None:
        self.theme_name = theme
        for i in range(1, self.count()):
            self.handle(i).update()


class ShellBackground(QWidget):
    """Paints the ground colour and the faint engineering-paper grid.

    The grid is what stops a large light surface reading as an empty browser
    page; it is the same 34 px lattice as the mockup.
    """

    def __init__(self, theme: str = T.DEFAULT_THEME, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("WindowFrame")
        self._theme = theme

    def refresh_theme(self, theme: str) -> None:
        self._theme = theme
        self.update()

    def paintEvent(self, event):
        pal = T.THEMES[self._theme]
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(pal["bg"]))
        p.setPen(QColor(*T.GRID_RGBA[self._theme]))
        step = T.GRID_STEP
        for x in range(0, self.width(), step):
            p.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), step):
            p.drawLine(0, y, self.width(), y)
        p.end()


class AppStatusBar(QWidget):
    """Bottom strip: index provenance, counts, signed-in user, disclaimer.

    The disclaimer is not decoration — PLAN §0.2 requires the decision-support
    statement to be present in the UI, so it is pinned here on every screen.
    """

    HEIGHT = 26
    DISCLAIMER = "DECISION SUPPORT — NOT PART OF THE MAINTENANCE RECORD"

    def __init__(self, theme: str = T.DEFAULT_THEME, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("StatusBar")
        self.setFixedHeight(self.HEIGHT)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(13, 0, 13, 0)
        lay.setSpacing(0)

        self.corpus = self._cell("Corpus not built")
        self.cases = self._cell("Cases —")
        self.user = self._cell("Not signed in")
        for w in (self.corpus, self.cases, self.user):
            lay.addWidget(w)
        lay.addStretch(1)

        self.disclaimer = QLabel(self.DISCLAIMER)
        self.disclaimer.setObjectName("Disclaimer")
        f = ui_font(7.5, QFont.Weight.DemiBold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        self.disclaimer.setFont(f)
        lay.addWidget(self.disclaimer)

    def _cell(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(ui_font(8))
        lbl.setContentsMargins(0, 0, 16, 0)
        lbl.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        return lbl

    def refresh_theme(self, theme: str) -> None:
        pal = T.THEMES[theme]
        for w in (self.corpus, self.cases, self.user):
            w.setStyleSheet(f"color:{pal['txt3']};")
        self.disclaimer.setStyleSheet(f"color:{pal['txt3']};")
