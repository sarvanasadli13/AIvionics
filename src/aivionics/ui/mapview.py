"""The fleet map widget: a world drawn without tiles (PLAN 4B.4).

Separated from the Ops page because it is the one piece of that screen with
real geometry in it, and because the projection maths deserves to be
reachable without dragging in a search box and two tables.

There is no tile server, no coastline asset and no network call behind the
background. The reference dots are bundled airports — 1,172 large ones at a
glance, and more of the 48,000 as you go in — which trace the populated
coastlines closely enough to tell you which continent a marker is over.

**Pan and zoom (BACKLOG round 2, R1).** The map used to be a fixed picture of
the whole world, which is defensible for "is my fleet in Europe or Asia" and
useless for anything else. It now has a viewport: scroll to zoom about the
cursor, drag to pan, double-click to go in, `0` to reset. The projection is
unchanged — plate carrée, so a zoom is a scale and an offset and nothing more.

Two things are deliberate:

* **The viewport is stored in world fractions, not pixels.** Resizing the
  window, or dragging the splitter, then keeps you looking at the same place
  instead of throwing you back to the mid-Atlantic.
* **Reference density is tied to zoom.** All 48,000 airports at world scale is
  a grey smear; 1,172 at street scale is an empty page. The tier changes with
  the zoom and the points are culled to the viewport with numpy, because doing
  it in a Python loop on every repaint is visible as lag while dragging.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QPainter, QPen, QPixmap, QPolygonF)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QToolButton, QWidget

from .. import config
from ..ops import adsb, airports as apt, radar
from . import theme as T
from .widgets import ui_font

MARKER_SVG = config.ASSETS_DIR / "aircraft" / "marker-plane.svg"
MARKER_PX = 20
HIT_RADIUS = 16.0

# Trails (Phase 8). Held in memory, bounded twice — points per aircraft and
# aircraft remembered — because this widget lives for the whole session and
# an unbounded history of a busy European sector is a leak with a nice name.
# Sixty points at one fetch every ~20 s is about twenty minutes of track,
# which is as far back as a trail is worth reading on a map at this scale.
TRAIL_MAX_POINTS = 60
TRAIL_MAX_KEYS = 48

MIN_ZOOM = 1.0
MAX_ZOOM = 256.0
WHEEL_STEP = 1.25           # per notch
DRAG_SLOP = 4.0             # pixels of movement that still counts as a click

# Graticule spacing, coarse to fine. The first row whose zoom the view has
# reached wins, so the lines stay roughly the same distance apart on screen.
GRATICULE_STEPS = ((2.0, 30), (4.0, 15), (8.0, 10), (16.0, 5), (48.0, 2), (1e9, 1))

# Reference tiers: (zoom below which this applies, airport types drawn).
# Horizontal slices per radar tile. A Mercator tile covers a latitude band
# that is not linear in y, so it cannot be blitted into a plate-carree
# rectangle as one piece. Sixteen bands puts the residual error well under a
# pixel at every zoom this map reaches, and costs sixteen draw calls.
RADAR_BANDS = 16
RADAR_OPACITY = 0.62

# Reference tiers: (zoom below which this applies, airport types drawn).
REFERENCE_TIERS = (
    (2.5, ("large_airport",)),
    (6.0, ("large_airport", "medium_airport")),
    (1e9, ("large_airport", "medium_airport", "small_airport")),
)


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
    aircraft_selected = Signal(object)
    viewport_changed = Signal()

    def __init__(self, theme: str = T.DEFAULT_THEME, parent=None) -> None:
        super().__init__(parent)
        self.theme_name = theme
        self.snapshot: adsb.FleetSnapshot | None = None
        self.reference: list[tuple[float, float]] = []
        self.selected = ""
        self.highlight: tuple[float, float, str] | None = None
        # Traffic the network saw in this box that is not ours (R2). Held
        # separately from `snapshot` because the two are not equivalent: a
        # fleet tail is a thing this application knows about, and one of
        # these is a contact.
        self.traffic: tuple = ()
        self.traffic_selected = ""
        self.traffic_credit = ""
        # Contacts can be hidden without being thrown away: an engineer
        # looking for one of six tails among three hundred chevrons wants the
        # rest gone, and wants them back when the search is over (Phase 8).
        self.fleet_only = False
        # Why the map looks the way it does. Never None: an unexplained empty
        # map is the failure this field exists to prevent.
        self.tracking: adsb.TrackingState = adsb.TrackingState()
        self._trails: dict[str, list[tuple[float, float]]] = {}
        # Radar imagery, held as bytes and decoded lazily, so a repaint while
        # dragging never decodes a PNG it already has (R3).
        self._radar_bytes: dict = {}
        self._radar_pixmaps: dict = {}
        self.radar_label = ""

        # Viewport: zoom, and the centre of the view as a fraction of the
        # world rectangle. Fractions rather than pixels so a resize keeps you
        # where you were.
        self._zoom = MIN_ZOOM
        self._cx = 0.5
        self._cy = 0.5

        self._dense: dict[str, np.ndarray] = {}
        self._drag_from: QPoint | None = None
        self._drag_origin: tuple[float, float] | None = None
        self._dragged = False

        self.setMinimumSize(420, 230)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip("Scroll to zoom · drag to pan · double-click to zoom in "
                        "· 0 to fit the world.\nClick a marker for that tail's "
                        "record.")
        self._build_controls()

    def sizeHint(self) -> QSize:
        return QSize(900, 460)

    # ── content ───────────────────────────────────────────────────────
    def set_reference(self, points: list[tuple[float, float]]) -> None:
        self.reference = points
        self.update()

    def set_reference_index(self, index) -> None:
        """Hand the map the airport index so it can thicken the backdrop as
        the view goes in. Optional — without it the base tier is all there is.
        """
        try:
            index.load()
            airports = index._airports
        except Exception:
            return
        for _limit, types in REFERENCE_TIERS:
            key = "+".join(types)
            if key in self._dense:
                continue
            chosen = [(a.latitude, a.longitude) for a in airports if a.type in types]
            if chosen:
                array = np.asarray(chosen, dtype=np.float32)
                self._dense[key] = array
        self.update()

    def set_snapshot(self, snapshot: adsb.FleetSnapshot | None) -> None:
        self.snapshot = snapshot
        # Our own tails always get a trail: there are a handful of them, they
        # are the ones anybody follows, and having the history already there
        # when a marker is clicked is the difference between a trail and a
        # promise of one.
        for position in (snapshot.seen if snapshot else ()):
            self._record_trail(position.tail, position.state)
        self.update()

    def set_fleet_only(self, on: bool) -> None:
        """Hide everything that is not ours, without discarding it."""
        self.fleet_only = bool(on)
        self.update()

    def set_tracking_state(self, state: adsb.TrackingState | None) -> None:
        """Why the map looks the way it does. Rendered, not stored quietly."""
        self.tracking = state or adsb.TrackingState()
        self.update()

    def set_traffic(self, states, attribution: str = "") -> None:
        """Live contacts inside the current view. Fleet tails are removed —
        an aircraft that is ours is drawn as ours, once.

        `attribution` is not decoration: the feed is ODbL, which requires the
        credit to appear wherever the data does. It is therefore kept for as
        long as *any* of that feed's data is on the map, which includes the
        fleet markers — the earlier version cleared the credit whenever the
        contact list came back empty, and the fleet markers beside it come
        from the same feed and were left uncredited.
        """
        ours = set()
        if self.snapshot is not None:
            ours = {p.icao24 for p in self.snapshot.positions if p.icao24}
            ours |= {p.tail for p in self.snapshot.positions if p.tail}
        self.traffic = tuple(st for st in (states or ())
                             if st.icao24 not in ours
                             and not (st.registration and st.registration in ours))
        if attribution:
            self.traffic_credit = attribution
        self.update()

    # ── trails ────────────────────────────────────────────────────────
    def _record_trail(self, key: str, state) -> None:
        if not key or state is None or not getattr(state, "has_position", False):
            return
        point = (float(state.latitude), float(state.longitude))
        track = self._trails.get(key)
        if track is None:
            if len(self._trails) >= TRAIL_MAX_KEYS:
                # Drop whichever aircraft has the shortest history: it is the
                # one whose trail is worth least, and dropping by insertion
                # order would evict the tail somebody has been watching.
                shortest = min(self._trails, key=lambda k: len(self._trails[k]))
                self._trails.pop(shortest, None)
            self._trails[key] = [point]
            return
        if track and track[-1] == point:
            return                  # a repeat fetch, not a new position
        track.append(point)
        del track[:-TRAIL_MAX_POINTS]

    def trail(self, key: str) -> tuple[tuple[float, float], ...]:
        return tuple(self._trails.get(key or "", ()))

    def clear_trails(self) -> None:
        self._trails.clear()
        self.update()

    def set_radar(self, tiles: dict | None, label: str = "") -> None:
        """Radar imagery for the current view, or None to clear it."""
        self._radar_bytes = dict(tiles or {})
        self._radar_pixmaps = {key: pixmap
                               for key, pixmap in self._radar_pixmaps.items()
                               if key in self._radar_bytes}
        self.radar_label = label if self._radar_bytes else ""
        self.update()

    @property
    def has_radar(self) -> bool:
        return bool(self._radar_bytes)

    def set_selected(self, tail: str) -> None:
        self.selected = (tail or "").strip().upper()
        self.update()

    # ── finding one aircraft (Phase 8) ────────────────────────────────
    def all_states(self) -> tuple:
        """Everything on the map as state vectors — ours first, then contacts.

        Ours first because a search for "N101AV" that returns a contact with a
        similar callsign before the fleet tail of the same name is a search
        that has to be done twice.
        """
        ours = tuple(p.state for p in (self.snapshot.seen if self.snapshot
                                       else ()) if p.state is not None)
        return ours + tuple(self.traffic)

    def find(self, query: str) -> list:
        """Aircraft matching a tail, callsign, transponder address or type."""
        return adsb.search_states(self.all_states(), query)

    def select_match(self, state) -> bool:
        """Select an aircraft found by search and put it in the middle.

        Returns whether it turned out to be one of ours, because the caller
        renders a different panel for a contact than for a tail and guessing
        from the state vector alone would get a fleet aircraft wrong whenever
        the feed happened to publish its registration.
        """
        if state is None or not state.has_position:
            return False
        tail = next((p.tail for p in (self.snapshot.positions if self.snapshot
                                      else ())
                     if p.state is not None and p.state.icao24 == state.icao24),
                    "")
        if tail:
            self.traffic_selected = ""
            self.selected = tail
        else:
            self.selected = ""
            self.traffic_selected = state.icao24
            self._record_trail(state.icao24, state)
        self.focus_on(state.latitude, state.longitude,
                      max(self._zoom, 24.0))
        return bool(tail)

    def set_highlight(self, latitude: float | None, longitude: float | None,
                      label: str = "") -> None:
        """Mark one place — the airport currently being read (R4)."""
        self.highlight = (None if latitude is None or longitude is None
                          else (float(latitude), float(longitude), label))
        self.update()

    def refresh_theme(self, theme: str) -> None:
        self.theme_name = theme
        self._style_controls()
        self.update()

    # ── viewport ──────────────────────────────────────────────────────
    @property
    def zoom(self) -> float:
        return self._zoom

    def visible_bounds(self) -> tuple[float, float, float, float]:
        """(lat_min, lon_min, lat_max, lon_max) of what is on screen.

        This is what an area query to a traffic feed is built from, so it is
        public and it is clamped to the real world rather than to the widget.
        """
        top_lat, left_lon = self._latlon_at(0.0, 0.0)
        bottom_lat, right_lon = self._latlon_at(float(self.width()),
                                                float(self.height()))
        return (max(-90.0, min(top_lat, bottom_lat)),
                max(-180.0, min(left_lon, right_lon)),
                min(90.0, max(top_lat, bottom_lat)),
                min(180.0, max(left_lon, right_lon)))

    def reset_view(self) -> None:
        self._set_view(MIN_ZOOM, 0.5, 0.5)

    def zoom_by(self, factor: float, anchor: QPoint | None = None) -> None:
        """Zoom about `anchor`, keeping the ground under it in place."""
        target = max(MIN_ZOOM, min(MAX_ZOOM, self._zoom * factor))
        if target == self._zoom:
            return
        if anchor is None:
            anchor = QPoint(self.width() // 2, self.height() // 2)
        latitude, longitude = self._latlon_at(float(anchor.x()), float(anchor.y()))
        width, height = self._world_size(target)
        x, y = apt.project(latitude, longitude, width, height)
        cx = (x - (anchor.x() - self.width() / 2.0)) / width
        cy = (y - (anchor.y() - self.height() / 2.0)) / height
        self._set_view(target, cx, cy)

    def focus_on(self, latitude: float, longitude: float,
                 zoom: float = 24.0) -> None:
        """Put a position in the middle of the view at a readable scale (R4)."""
        target = max(MIN_ZOOM, min(MAX_ZOOM, float(zoom)))
        width, height = self._world_size(target)
        x, y = apt.project(latitude, longitude, width, height)
        self._set_view(target, x / width, y / height)

    def _set_view(self, zoom: float, cx: float, cy: float) -> None:
        self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        self._cx, self._cy = self._clamp(cx, cy)
        self._sync_controls()
        self.update()
        self.viewport_changed.emit()

    def _clamp(self, cx: float, cy: float) -> tuple[float, float]:
        """Keep the world under the widget — no dragging it off into space."""
        width, height = self._world_size(self._zoom)
        half_w = self.width() / 2.0 / width if width else 0.5
        half_h = self.height() / 2.0 / height if height else 0.5
        cx = 0.5 if half_w >= 0.5 else min(max(cx, half_w), 1.0 - half_w)
        cy = 0.5 if half_h >= 0.5 else min(max(cy, half_h), 1.0 - half_h)
        return cx, cy

    # ── geometry ──────────────────────────────────────────────────────
    def _base_size(self) -> tuple[float, float]:
        """The 2:1-ish rectangle the whole world fits in at zoom 1.

        Plate carrée is 360° by 170°; forcing it into whatever aspect the
        splitter happens to give would stretch continents differently on
        every window resize.
        """
        width, height = float(self.width()), float(self.height())
        span = min(width, height * (360.0 / 170.0))
        return span, span * 170.0 / 360.0

    def _world_size(self, zoom: float | None = None) -> tuple[float, float]:
        base_w, base_h = self._base_size()
        factor = self._zoom if zoom is None else zoom
        return base_w * factor, base_h * factor

    def _origin(self) -> tuple[float, float]:
        width, height = self._world_size()
        return (self.width() / 2.0 - self._cx * width,
                self.height() / 2.0 - self._cy * height)

    def _point(self, latitude: float, longitude: float) -> tuple[float, float]:
        left, top = self._origin()
        width, height = self._world_size()
        x, y = apt.project(latitude, longitude, width, height)
        return left + x, top + y

    def _latlon_at(self, px: float, py: float) -> tuple[float, float]:
        left, top = self._origin()
        width, height = self._world_size()
        return apt.unproject(px - left, py - top, width, height)

    def _traffic_markers(self) -> list[tuple[object, float, float]]:
        if self.fleet_only:
            return []
        placed = []
        for state in self.traffic:
            x, y = self._point(state.latitude, state.longitude)
            if -30 <= x <= self.width() + 30 and -30 <= y <= self.height() + 30:
                placed.append((state, x, y))
        return placed

    def _markers(self) -> list[tuple[adsb.FleetPosition, float, float]]:
        if self.snapshot is None:
            return []
        placed = []
        for position in self.snapshot.seen:
            state = position.state
            x, y = self._point(state.latitude, state.longitude)
            placed.append((position, x, y))
        return placed

    # ── input ─────────────────────────────────────────────────────────
    def wheelEvent(self, event) -> None:
        notches = event.angleDelta().y() / 120.0
        if notches:
            self.zoom_by(WHEEL_STEP ** notches, event.position().toPoint())
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._drag_from = event.position().toPoint()
        self._drag_origin = (self._cx, self._cy)
        self._dragged = False
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_from is None or self._drag_origin is None:
            return
        delta = event.position().toPoint() - self._drag_from
        if not self._dragged and (abs(delta.x()) + abs(delta.y())) > DRAG_SLOP:
            self._dragged = True
        if self._dragged:
            width, height = self._world_size()
            self._cx, self._cy = self._clamp(
                self._drag_origin[0] - delta.x() / width,
                self._drag_origin[1] - delta.y() / height)
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_from is None:
            super().mouseReleaseEvent(event)
            return
        point = event.position()
        dragged, self._dragged = self._dragged, False
        self._drag_from = self._drag_origin = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if dragged:
            self.viewport_changed.emit()
            return
        # A click that never moved is a selection, not a pan.
        nearest, best = "", HIT_RADIUS
        for position, x, y in self._markers():
            distance = ((point.x() - x) ** 2 + (point.y() - y) ** 2) ** 0.5
            if distance < best:
                nearest, best = position.tail, distance
        if nearest:
            self.traffic_selected = ""
            self.set_selected(nearest)
            self.tail_selected.emit(nearest)
            return

        # Our own fleet is checked first and wins ties: on a busy map the
        # tail an engineer is looking for must not be stolen by a contact
        # that happens to be two pixels closer.
        contact, best = None, HIT_RADIUS
        for state, x, y in self._traffic_markers():
            distance = ((point.x() - x) ** 2 + (point.y() - y) ** 2) ** 0.5
            if distance < best:
                contact, best = state, distance
        if contact is not None:
            self.traffic_selected = contact.icao24
            self.update()
            self.aircraft_selected.emit(contact)

    def mouseDoubleClickEvent(self, event) -> None:
        self.zoom_by(WHEEL_STEP ** 2, event.position().toPoint())
        event.accept()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        step = 0.08 / max(1.0, self._zoom / 4.0)
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_by(WHEEL_STEP)
        elif key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.zoom_by(1.0 / WHEEL_STEP)
        elif key in (Qt.Key.Key_0, Qt.Key.Key_Home):
            self.reset_view()
        elif key == Qt.Key.Key_Left:
            self._set_view(self._zoom, self._cx - step, self._cy)
        elif key == Qt.Key.Key_Right:
            self._set_view(self._zoom, self._cx + step, self._cy)
        elif key == Qt.Key.Key_Up:
            self._set_view(self._zoom, self._cx, self._cy - step)
        elif key == Qt.Key.Key_Down:
            self._set_view(self._zoom, self._cx, self._cy + step)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._cx, self._cy = self._clamp(self._cx, self._cy)
        self._place_controls()

    # ── the zoom controls ─────────────────────────────────────────────
    def _build_controls(self) -> None:
        """Visible controls, because a map whose only affordance is the scroll
        wheel is a map half the people using it think is a picture."""
        self._controls: list[QToolButton] = []
        for glyph, tip, slot in (
                ("+", "Zoom in", lambda: self.zoom_by(WHEEL_STEP ** 2)),
                ("−", "Zoom out", lambda: self.zoom_by(1.0 / WHEEL_STEP ** 2)),
                ("⤢", "Fit the whole world", self.reset_view)):
            button = QToolButton(self)
            button.setObjectName("MapControl")
            button.setText(glyph)
            button.setFixedSize(26, 26)
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(slot)
            self._controls.append(button)
        self._style_controls()
        self._place_controls()

    def _style_controls(self) -> None:
        pal = T.THEMES[self.theme_name]
        for button in getattr(self, "_controls", ()):
            button.setStyleSheet(
                f"QToolButton#MapControl{{background:{pal['s1']};"
                f"border:1px solid {pal['line']};border-radius:4px;"
                f"color:{pal['txt2']};font-size:14px;font-weight:600;}}"
                f"QToolButton#MapControl:hover{{background:{pal['s3']};"
                f"color:{pal['txt']};}}")

    def _place_controls(self) -> None:
        for i, button in enumerate(getattr(self, "_controls", ())):
            button.move(self.width() - 36, 10 + i * 30)

    def _sync_controls(self) -> None:
        controls = getattr(self, "_controls", ())
        if len(controls) == 3:
            controls[0].setEnabled(self._zoom < MAX_ZOOM)
            controls[1].setEnabled(self._zoom > MIN_ZOOM)
            controls[2].setEnabled(self._zoom > MIN_ZOOM)

    # ── painting ──────────────────────────────────────────────────────
    def paintEvent(self, event) -> None:
        pal = T.THEMES[self.theme_name]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, top = self._origin()
        width, height = self._world_size()

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(pal["well"]))
        painter.drawRect(int(left), int(top), int(width) + 1, int(height) + 1)

        painter.setClipRect(self.rect())
        self._graticule(painter, pal)
        self._reference(painter, pal)
        self._radar_paint(painter)

        painter.setPen(QPen(QColor(pal["line"]), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(int(left), int(top), int(width), int(height))

        self._highlight_paint(painter, pal)
        self._trail_paint(painter, pal)
        self._traffic_paint(painter, pal)
        self._markers_paint(painter, pal)
        self._radar_note(painter, pal)
        self._scale_note(painter, pal)
        self._state_paint(painter, pal)
        painter.end()

    def _selected_key(self) -> str:
        return self.selected or self.traffic_selected

    def _trail_paint(self, painter, pal) -> None:
        """Where the selected aircraft has been, drawn under everything else.

        Only the selected one. A trail per contact turns a busy sector into a
        ball of wool, and the question a trail answers — "which way has this
        one been going" — is only ever asked about one aircraft at a time.
        """
        points = self._trails.get(self._selected_key(), ())
        if len(points) < 2:
            return
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(pal["cyf"] if self.selected else pal["amb"]),
                            1.4, Qt.PenStyle.DashLine))
        path = QPolygonF([QPointF(*self._point(lat, lon))
                          for lat, lon in points])
        painter.drawPolyline(path)
        painter.restore()

    def _state_paint(self, painter, pal) -> None:
        """The reason for what is, or is not, on the map — over the map.

        Drawn last so nothing can cover it, and drawn even when there are
        markers: a stale layer with aircraft on it is the case where silence
        is most expensive, because everything looks normal.
        """
        state = self.tracking
        if not state.blank_is_explained:
            return
        margin = 18
        width = min(self.width() - margin * 2, 460)
        if width < 140:
            return                     # narrower than the words; say nothing
        empty = not self._markers() and not self._traffic_markers()
        colour = QColor(pal["amb"] if state.state != adsb.TRACKING_ERROR
                        else pal["red"])
        quiet = QColor(pal["ambq"] if state.state != adsb.TRACKING_ERROR
                       else pal["redq"])

        painter.setFont(ui_font(9, QFont.Weight.DemiBold))
        head_h = painter.fontMetrics().height()
        body_rect = QRectF(0, 0, width - 22, 400)
        painter.setFont(ui_font(8.5))
        body = painter.boundingRect(
            body_rect, int(Qt.TextFlag.TextWordWrap), state.detail)
        height = head_h + int(body.height()) + 24

        # Centred when the map has nothing on it, out of the way when it has.
        left = (self.width() - width) / 2.0
        top = ((self.height() - height) / 2.0 if empty
               else self.height() - height - 34)
        painter.setPen(QPen(colour, 1))
        painter.setBrush(quiet)
        painter.drawRoundedRect(QRectF(left, top, width, height), 5, 5)
        painter.setPen(colour)
        painter.setFont(ui_font(9, QFont.Weight.DemiBold))
        painter.drawText(QRectF(left + 11, top + 8, width - 22, head_h),
                         int(Qt.AlignmentFlag.AlignLeft), state.headline)
        painter.setFont(ui_font(8.5))
        painter.drawText(
            QRectF(left + 11, top + 10 + head_h, width - 22, body.height() + 2),
            int(Qt.TextFlag.TextWordWrap), state.detail)

    def _graticule(self, painter, pal) -> None:
        step = next(size for limit, size in GRATICULE_STEPS if self._zoom < limit)
        lat_min, lon_min, lat_max, lon_max = self.visible_bounds()
        painter.setPen(QPen(QColor(pal["hair"]), 1))
        first = int(lon_min // step) * step
        longitude = first
        while longitude <= lon_max + step:
            if -180.0 <= longitude <= 180.0:
                x, _ = self._point(0.0, longitude)
                painter.drawLine(int(x), 0, int(x), self.height())
            longitude += step
        latitude = int(lat_min // step) * step
        while latitude <= lat_max + step:
            if -85.0 <= latitude <= 85.0:
                _, y = self._point(latitude, 0.0)
                painter.drawLine(0, int(y), self.width(), int(y))
            latitude += step
        # The equator is the one line worth naming; the rest are scale.
        painter.setPen(QPen(QColor(pal["line"]), 1, Qt.PenStyle.DashLine))
        _, equator = self._point(0.0, 0.0)
        painter.drawLine(0, int(equator), self.width(), int(equator))

    def _reference(self, painter, pal) -> None:
        """Bundled airports as a backdrop. Not a coastline, and never claimed
        to be one — but enough of them to tell you where you are."""
        types = next(t for limit, t in REFERENCE_TIERS if self._zoom < limit)
        array = self._dense.get("+".join(types))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(pal["txt3"]))
        size = 2 if self._zoom < 6 else 3

        if array is None:
            for latitude, longitude in self.reference:
                x, y = self._point(latitude, longitude)
                if -8 <= x <= self.width() + 8 and -8 <= y <= self.height() + 8:
                    painter.drawRect(int(x), int(y), 2, 2)
            return

        lat_min, lon_min, lat_max, lon_max = self.visible_bounds()
        lats, lons = array[:, 0], array[:, 1]
        mask = ((lats >= lat_min - 1) & (lats <= lat_max + 1)
                & (lons >= lon_min - 1) & (lons <= lon_max + 1))
        visible = array[mask]
        # A viewport that somehow selects half the world is a bug in the
        # clamp, not a reason to spend a second painting; cap and move on.
        for latitude, longitude in visible[:20000]:
            x, y = self._point(float(latitude), float(longitude))
            painter.drawRect(int(x), int(y), size, size)

    def _radar_paint(self, painter) -> None:
        """Draw each Mercator tile as a stack of correctly-placed bands.

        Drawn over the reference dots and under everything that means
        something operational: an aircraft must never be obscured by weather
        imagery, which is decoration next to a position report.
        """
        if not self._radar_bytes:
            return
        painter.save()
        painter.setOpacity(RADAR_OPACITY)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        for (z, x, y), raw in self._radar_bytes.items():
            pixmap = self._radar_pixmaps.get((z, x, y))
            if pixmap is None:
                pixmap = QPixmap()
                if not pixmap.loadFromData(raw):
                    continue
                self._radar_pixmaps[(z, x, y)] = pixmap
            if pixmap.isNull():
                continue
            _, lon_west, _, lon_east = radar.tile_bounds(x, y, z)
            left, _ = self._point(0.0, lon_west)
            right, _ = self._point(0.0, lon_east)
            if right < -2 or left > self.width() + 2:
                continue
            n = 2 ** z
            height = pixmap.height()
            for band in range(RADAR_BANDS):
                f0 = band / RADAR_BANDS
                f1 = (band + 1) / RADAR_BANDS
                lat0 = radar.tile_bounds(x, y, z)[0] if band == 0 else \
                    _band_lat(y + f0, n)
                lat1 = _band_lat(y + f1, n)
                _, top = self._point(lat0, lon_west)
                _, bottom = self._point(lat1, lon_west)
                if bottom < -2 or top > self.height() + 2:
                    continue
                painter.drawPixmap(
                    QRectF(left, top, right - left, max(1.0, bottom - top)),
                    pixmap,
                    QRectF(0.0, f0 * height, pixmap.width(),
                           (f1 - f0) * height))
        painter.restore()

    def _highlight_paint(self, painter, pal) -> None:
        if not self.highlight:
            return
        latitude, longitude, label = self.highlight
        x, y = self._point(latitude, longitude)
        painter.setPen(QPen(QColor(pal["amb"]), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(int(x - 9), int(y - 9), 18, 18)
        painter.drawLine(int(x), int(y - 15), int(x), int(y - 9))
        painter.drawLine(int(x), int(y + 9), int(x), int(y + 15))
        painter.drawLine(int(x - 15), int(y), int(x - 9), int(y))
        painter.drawLine(int(x + 9), int(y), int(x + 15), int(y))
        if label:
            painter.setFont(ui_font(8, QFont.Weight.Bold))
            painter.setPen(QColor(pal["amb"]))
            painter.drawText(int(x + 18), int(y + 4), label)

    def _traffic_paint(self, painter, pal) -> None:
        """Contacts: a quiet chevron, no label. They are context, not content
        - the fleet has to stay findable on top of them."""
        placed = self._traffic_markers()
        if not placed:
            return
        painter.setFont(ui_font(7.5, QFont.Weight.DemiBold))
        for state, x, y in placed:
            chosen = state.icao24 == self.traffic_selected
            colour = QColor(pal["amb"] if chosen else pal["txt3"])
            if chosen:
                painter.setPen(QPen(QColor(pal["amb"]), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(int(x - 11), int(y - 11), 22, 22)
            painter.save()
            painter.translate(x, y)
            painter.rotate(state.true_track or 0.0)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            size = 5.0 if not chosen else 6.5
            painter.drawPolygon(QPolygonF([
                QPointF(0.0, -size), QPointF(size * 0.72, size * 0.8),
                QPointF(0.0, size * 0.35), QPointF(-size * 0.72, size * 0.8)]))
            painter.restore()
            if chosen and state.identity:
                painter.setPen(QColor(pal["amb"]))
                painter.drawText(int(x + 13), int(y + 4), state.identity)

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
            if not (-40 <= x <= self.width() + 40 and -40 <= y <= self.height() + 40):
                continue
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

    def _radar_note(self, painter, pal) -> None:
        """The attributions the data licences require, on the map itself.

        RainViewer asks for one and ODbL obliges another; both render here
        rather than in a menu, because "wherever the data appears" is what
        the licences say and a map is where this data appears.
        """
        painter.setFont(ui_font(7.5))
        painter.setPen(QColor(pal["txt3"]))
        for line, credit in enumerate(
                [c for c in (self.radar_label, self.traffic_credit) if c]):
            painter.drawText(10, 16 + line * 13, credit)

    def _scale_note(self, painter, pal) -> None:
        """The zoom, in words, bottom left. Without it there is no way to tell
        a 4× view from a 40× one on an ocean."""
        if self._zoom <= MIN_ZOOM:
            return
        painter.setFont(ui_font(8, QFont.Weight.DemiBold))
        painter.setPen(QColor(pal["txt3"]))
        lat_min, lon_min, lat_max, lon_max = self.visible_bounds()
        painter.drawText(10, self.height() - 10,
                         f"{self._zoom:.0f}×   "
                         f"{abs(lat_max - lat_min):.1f}° × {abs(lon_max - lon_min):.1f}°")


def _band_lat(tile_y: float, n: int) -> float:
    """Latitude of a fractional row inside the Mercator tile grid."""
    import math
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * tile_y / n))))
