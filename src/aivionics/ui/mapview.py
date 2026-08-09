"""The fleet map widget: a world drawn without tiles (PLAN 4B.4).

Separated from the Ops page because it is the one piece of that screen with
real geometry in it, and because the projection maths deserves to be
reachable without dragging in a search box and two tables.

There is no tile server, no coastline asset and no network call behind the
background. The reference dots are the 1,172 large airports already bundled
offline, which trace the populated coastlines closely enough to tell you
which continent a marker is over — and that is the whole of what this map
claims to do. It is situational awareness, not a traffic display.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

from .. import config
from ..ops import adsb, airports as apt
from . import theme as T
from .widgets import ui_font

MARKER_SVG = config.ASSETS_DIR / "aircraft" / "marker-plane.svg"
MARKER_PX = 20
HIT_RADIUS = 16.0


def _svg_pixmap(path, size: int, colour: str, angle: float = 0.0) -> QPixmap:
    """Rasterise the marker in a palette colour, rotated to its track.

    The asset paints with `fill="currentColor"`, which Qt's SVG renderer does
    not resolve — substituting the literal is the whole trick, and it keeps
    one asset serving both themes and every status colour.
    """
    try:
        source = path.read_text(encoding="utf-8").replace("currentColor", colour)
    except OSError:
        return QPixmap()
    canvas = QPixmap(size * 2, size * 2)
    canvas.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(source.encode("utf-8"))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(size, size)
    painter.rotate(angle)
    renderer.render(painter, QRectF(-size / 2, -size / 2, size, size))
    painter.end()
    return canvas


class MapView(QWidget):
    """Plate-carrée world with fleet markers. No tiles, no network, no library."""

    tail_selected = Signal(str)

    def __init__(self, theme: str = T.DEFAULT_THEME, parent=None) -> None:
        super().__init__(parent)
        self.theme_name = theme
        self.snapshot: adsb.FleetSnapshot | None = None
        self.reference: list[tuple[float, float]] = []
        self.selected = ""
        self.setMinimumSize(420, 230)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip("Aircraft seen by the OpenSky receiver network. "
                        "Click a marker for that tail's record.")

    def sizeHint(self) -> QSize:
        return QSize(900, 460)

    def set_reference(self, points: list[tuple[float, float]]) -> None:
        self.reference = points
        self.update()

    def set_snapshot(self, snapshot: adsb.FleetSnapshot | None) -> None:
        self.snapshot = snapshot
        self.update()

    def set_selected(self, tail: str) -> None:
        self.selected = (tail or "").strip().upper()
        self.update()

    def refresh_theme(self, theme: str) -> None:
        self.theme_name = theme
        self.update()

    # ── geometry ──────────────────────────────────────────────────────
    def _canvas(self) -> tuple[float, float, float, float]:
        """The 2:1 rectangle the world is drawn in, centred in the widget.

        Plate carrée is 360° by 170°; forcing it into whatever aspect the
        splitter happens to give would stretch continents differently on
        every window resize.
        """
        width, height = float(self.width()), float(self.height())
        span = min(width, height * (360.0 / 170.0))
        return ((width - span) / 2.0, (height - span * 170.0 / 360.0) / 2.0,
                span, span * 170.0 / 360.0)

    def _point(self, latitude: float, longitude: float) -> tuple[float, float]:
        left, top, width, height = self._canvas()
        x, y = apt.project(latitude, longitude, width, height)
        return left + x, top + y

    def _markers(self) -> list[tuple[adsb.FleetPosition, float, float]]:
        if self.snapshot is None:
            return []
        placed = []
        for position in self.snapshot.seen:
            state = position.state
            x, y = self._point(state.latitude, state.longitude)
            placed.append((position, x, y))
        return placed

    def mousePressEvent(self, event) -> None:
        point = event.position()
        nearest, best = "", HIT_RADIUS
        for position, x, y in self._markers():
            distance = ((point.x() - x) ** 2 + (point.y() - y) ** 2) ** 0.5
            if distance < best:
                nearest, best = position.tail, distance
        if nearest:
            self.set_selected(nearest)
            self.tail_selected.emit(nearest)

    # ── painting ──────────────────────────────────────────────────────
    def paintEvent(self, event) -> None:
        pal = T.THEMES[self.theme_name]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, top, width, height = self._canvas()

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(pal["well"]))
        painter.drawRect(int(left), int(top), int(width), int(height))

        self._graticule(painter, pal, left, top, width, height)
        self._reference(painter, pal)

        painter.setPen(QPen(QColor(pal["line"]), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(int(left), int(top), int(width), int(height))

        self._markers_paint(painter, pal)
        painter.end()

    def _graticule(self, painter, pal, left, top, width, height) -> None:
        painter.setPen(QPen(QColor(pal["hair"]), 1))
        for longitude in range(-150, 180, 30):
            x = left + (longitude + 180.0) / 360.0 * width
            painter.drawLine(int(x), int(top), int(x), int(top + height))
        for latitude in (-60, -30, 0, 30, 60):
            _, y = self._point(latitude, 0)
            painter.drawLine(int(left), int(y), int(left + width), int(y))
        # The equator is the one line worth naming; the rest are scale.
        painter.setPen(QPen(QColor(pal["line"]), 1, Qt.PenStyle.DashLine))
        _, equator = self._point(0, 0)
        painter.drawLine(int(left), int(equator), int(left + width), int(equator))

    def _reference(self, painter, pal) -> None:
        """The 1,172 large airports, as a one-pixel backdrop. Not a coastline."""
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(pal["txt3"]))
        for latitude, longitude in self.reference:
            x, y = self._point(latitude, longitude)
            painter.drawRect(int(x), int(y), 2, 2)

    def _markers_paint(self, painter, pal) -> None:
        painter.setFont(ui_font(8, QFont.Weight.DemiBold))
        metrics = painter.fontMetrics()
        placed = self._markers()
        # Selected first, so that when two tails overlap — a whole fleet in
        # the same European sector is the normal case, not an edge case — the
        # one being read is the one that keeps its label.
        placed.sort(key=lambda item: item[0].tail != self.selected)
        labelled: list[tuple[float, float, float, float]] = []

        for position, x, y in placed:
            state = position.state
            chosen = position.tail == self.selected
            colour = pal["cy"] if chosen else pal["cyf"]
            if chosen:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(pal["cyq"]))
                painter.drawEllipse(int(x - 15), int(y - 15), 30, 30)
            marker = _svg_pixmap(MARKER_SVG, MARKER_PX, colour,
                                 state.true_track or 0.0)
            if not marker.isNull():
                painter.drawPixmap(int(x - MARKER_PX), int(y - MARKER_PX), marker)

            left, top = x + 13, y - 6
            right = left + metrics.horizontalAdvance(position.tail)
            bottom = top + metrics.height()
            if any(left < ox2 and right > ox1 and top < oy2 and bottom > oy1
                   for ox1, oy1, ox2, oy2 in labelled):
                continue        # would overprint a label already drawn
            labelled.append((left, top, right, bottom))
            painter.setPen(QColor(pal["txt"] if chosen else pal["txt2"]))
            painter.drawText(int(left), int(y + 4), position.tail)
