"""Ops — live fleet map and airport page (PLAN 4B.4, 4B.5, standing rule 12).

The entire online-dependent surface of the application is this one screen, so
switching `online_enabled` off removes exactly one rail item and the offline
core is provably untouched.

What "offline" means here is precise, and the split runs down the middle of
the airport tab rather than around the whole page:

* **Offline fields render always.** Identifiers, position, elevation,
  runways, frequencies and IANA local time come from the bundled OurAirports
  CSVs and `timezonefinder`. They work with the cable out, and PLAN 4B.5
  requires them to, which is why the rail item is dimmed rather than
  disabled.
* **Online fields go dark visibly.** METAR, TAF, arrivals, departures and
  live positions say "unavailable offline" with the reason, and never fall
  back to a stale value presented as current.

The map draws no tiles. There is no tile-server dependency, no coastline
asset and no network call to paint the background — the reference dots are
the 1,172 large airports already bundled, which trace the populated
coastlines well enough to place a marker on a continent. That is the whole
of what this map claims to do: it is situational awareness, not a traffic
display.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QSplitter, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout,
                               QWidget)

from ... import config
from ...ops import adsb, airports as apt, weather as wx
from .. import theme as T
from ..opsservice import AirportDetail, OpsService, Ready, TailRecord
from ..widgets import (EmptyState, MonoLabel, Placard, ProvenanceLine,
                       SectionHeader, StatusBadge, Tag, mono_font, ui_font)
from .base import Page, caption, heading, scroll_host

MARKER_SVG = config.ASSETS_DIR / "aircraft" / "marker-plane.svg"
MARKER_PX = 20
HIT_RADIUS = 16.0

OFFLINE_NOTE = ("Online features are switched off. Airport identifiers, "
                "runways, elevation and local time below are read from bundled "
                "offline data and still work; weather, arrivals and live "
                "positions need the switch in Admin.")


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


class _Card(QFrame):
    """Titled block used down both tabs. Header is a Placard, never a heading."""

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(13, 10, 13, 11)
        self.box.setSpacing(6)
        self.box.addWidget(Placard(title))

    def add(self, widget: QWidget) -> QWidget:
        self.box.addWidget(widget)
        return widget

    def clear_body(self) -> None:
        while self.box.count() > 1:
            item = self.box.takeAt(1)
            widget = item.widget()
            if widget is not None:
                # Unparent before scheduling deletion: `deleteLater` alone
                # leaves the old widget painted until the event loop gets
                # round to it, which put the placeholder caption on top of
                # the card that had just replaced it.
                widget.setParent(None)
                widget.deleteLater()


def _pair(label: str, value: str, mono: bool = False) -> QWidget:
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(10)
    key = QLabel(label)
    key.setObjectName("Muted")
    key.setFont(ui_font(8.5))
    key.setFixedWidth(112)
    row.addWidget(key, 0, Qt.AlignmentFlag.AlignTop)
    text = QLabel(value)
    text.setWordWrap(True)
    text.setFont(mono_font(9, QFont.Weight.Normal) if mono else ui_font(9))
    text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    row.addWidget(text, 1)
    return host


def _table(columns: list[str], stretch: int = 0) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(T.ROW_HEIGHT - 4)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setShowGrid(False)
    table.setAlternatingRowColors(True)
    header = table.horizontalHeader()
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                               | Qt.AlignmentFlag.AlignVCenter)
    for i in range(len(columns)):
        header.setSectionResizeMode(
            i, QHeaderView.ResizeMode.Stretch if i == stretch
            else QHeaderView.ResizeMode.ResizeToContents)
    return table


class OpsPage(Page):
    """One rail item carrying the whole online surface."""

    title = "Ops"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self.service = OpsService(
            getattr(ctx, "db_path", None),
            online=lambda: bool(getattr(self.ctx, "online_enabled", False)))
        self.ready: Ready | None = None
        self.detail: AirportDetail | None = None
        self._results: list[apt.Airport] = []
        self._jobs: list = []                    # keeps signal objects alive
        self._warmed = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(SectionHeader(
            "Ops", "live fleet map · airport page · weather"))
        outer.addWidget(self._banner())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._map_tab(), "Fleet map")
        self.tabs.addTab(self._airport_tab(), "Airport")
        outer.addWidget(self.tabs, 1)

        self.provenance = ProvenanceLine("")
        self.provenance.setContentsMargins(15, 8, 15, 10)
        outer.addWidget(self.provenance)

    # ── the offline banner ────────────────────────────────────────────
    def _banner(self) -> QWidget:
        self.banner = QLabel(OFFLINE_NOTE)
        self.banner.setObjectName("Banner")
        self.banner.setWordWrap(True)
        self.banner.setFont(ui_font(9, QFont.Weight.DemiBold))
        self.banner.setContentsMargins(15, 9, 15, 9)
        self.banner.hide()
        return self.banner

    # ── map tab ───────────────────────────────────────────────────────
    def _map_tab(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.map = MapView(self.theme_name)
        self.map.tail_selected.connect(self._show_tail)
        split.addWidget(self.map)

        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(13, 12, 13, 12)
        sl.setSpacing(9)
        self.tail_card = _Card("Selected tail")
        self.tail_card.add(caption(
            "Click an aircraft on the map to see its position, its recent "
            "defects and its compliance rows.", "Muted", 8.5))
        sl.addWidget(self.tail_card)
        self.unseen_card = _Card("Not seen")
        sl.addWidget(self.unseen_card)
        sl.addStretch(1)
        split.addWidget(scroll_host(side))
        split.setSizes([880, 380])
        lay.addWidget(split, 1)

        foot = QFrame()
        foot.setObjectName("Band")
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(15, 9, 15, 9)
        fl.setSpacing(4)
        self.map_summary = caption("", "Muted", 8.5)
        fl.addWidget(self.map_summary)
        fl.addWidget(caption("⚠ " + adsb.COVERAGE_WARNING, "Faint", 8))
        lay.addWidget(foot)
        return host

    # ── airport tab ───────────────────────────────────────────────────
    def _airport_tab(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        band = QFrame()
        band.setObjectName("Band")
        bl = QHBoxLayout(band)
        bl.setContentsMargins(15, 10, 15, 10)
        bl.setSpacing(12)
        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "Search by ICAO, IATA, airport name or city — e.g. EDDF, SEA, Vienna")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(30)
        self.search.textChanged.connect(self._search)
        bl.addWidget(self.search, 1)
        self.search_count = caption("", "Faint", 8.5)
        bl.addWidget(self.search_count)
        lay.addWidget(band)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.results = QListWidget()
        self.results.setMinimumWidth(230)
        self.results.setAlternatingRowColors(True)
        self.results.currentRowChanged.connect(self._pick)
        split.addWidget(self.results)

        body = QWidget()
        self.airport_box = QVBoxLayout(body)
        self.airport_box.setContentsMargins(13, 12, 13, 12)
        self.airport_box.setSpacing(9)
        self.airport_empty = EmptyState(
            "mdi6.airport", "No airport selected",
            "Search above. Identifiers, runways, elevation and local time come "
            "from bundled offline data and render with the network unplugged; "
            "weather and movements need online features switched on.",
            theme=self.theme_name)
        self.airport_box.addWidget(self.airport_empty)
        self.airport_box.addStretch(1)
        split.addWidget(scroll_host(body))
        split.setSizes([280, 980])
        lay.addWidget(split, 1)
        return host

    # ── lifecycle ─────────────────────────────────────────────────────
    def on_shown(self) -> None:
        online = bool(getattr(self.ctx, "online_enabled", False))
        self.banner.setVisible(not online)
        if not online:
            pal = T.THEMES[self.theme_name]
            self.banner.setStyleSheet(
                f"background:{pal['ambq']};color:{pal['amb']};"
                f"border-bottom:1px solid {pal['amb']};")

        if not self._warmed:
            self._warmed = True
            self.provenance.setText("Loading bundled airport data…")
            self._run(self.service.warm, self._on_ready)
        else:
            self._refresh_provenance()
        if online:
            self._run(self.service.fleet, self._on_fleet)
        else:
            self.map.set_snapshot(None)
            self.map_summary.setText(
                "Live positions are unavailable offline — switch online "
                "features on in Admin to populate the map.")
            self._render_unseen(None)

    def _run(self, work, done) -> None:
        """Every call in this file goes through here — never the UI thread.

        The signals object is held until it fires: nothing else references
        it, and a garbage-collected receiver means the result of a fetch
        already paid for is dropped. It is released afterwards so a long
        session does not accumulate one per search.
        """
        signals = self.service.submit(work)
        self._jobs.append(signals)

        def finish(payload, handler=done, holder=signals) -> None:
            handler(payload)
            if holder in self._jobs:
                self._jobs.remove(holder)

        signals.done.connect(finish)
        signals.failed.connect(lambda message, holder=signals: (
            self._on_failed(message),
            self._jobs.remove(holder) if holder in self._jobs else None))

    def _on_failed(self, message: str) -> None:
        self.provenance.setText(f"Ops: background work failed — {message}")

    def _on_ready(self, ready: Ready) -> None:
        self.ready = ready
        if ready.ok:
            self.map.set_reference(apt.index().map_reference_points())
        self._refresh_provenance()

    def _refresh_provenance(self) -> None:
        parts = [self.service.offline_provenance()]
        if self.ready is not None and self.ready.reason:
            parts.append("⚠ " + self.ready.reason)
        if not bool(getattr(self.ctx, "online_enabled", False)):
            parts.append("Online sources: not contacted — the switch is off.")
        self.provenance.setText(" | ".join(parts))

    # ── map data ──────────────────────────────────────────────────────
    def _on_fleet(self, snapshot: adsb.FleetSnapshot) -> None:
        self.map.set_snapshot(snapshot)
        self.map_summary.setText(
            f"{snapshot.summary()} · {snapshot.provenance()}"
            if snapshot.ok else
            f"No live positions — {snapshot.fetch.error}. "
            f"{snapshot.summary()}")
        self._render_unseen(snapshot)

    def _render_unseen(self, snapshot: adsb.FleetSnapshot | None) -> None:
        self.unseen_card.clear_body()
        if snapshot is None:
            self.unseen_card.add(caption(
                "Live positions are switched off, so no tail is being tracked.",
                "Muted", 8.5))
            return
        if not snapshot.unseen and not snapshot.untracked:
            self.unseen_card.add(caption(
                "Every tracked tail was seen in this fetch.", "Muted", 8.5))
            return
        for position in snapshot.unseen:
            self.unseen_card.add(_pair(position.tail, position.reason))
        for tail in snapshot.untracked:
            self.unseen_card.add(_pair(tail, "no ICAO 24-bit address on file"))
        self.unseen_card.add(caption(
            "Not seen is not the same as not flying — see the coverage note "
            "under the map.", "Faint", 8))

    def _show_tail(self, tail: str) -> None:
        self._run(lambda: self.service.tail_record(tail), self._on_tail)

    def _on_tail(self, record: TailRecord) -> None:
        card = self.tail_card
        card.clear_body()
        snapshot = self.map.snapshot
        position = next((p for p in (snapshot.positions if snapshot else ())
                         if p.tail == record.tail), None)
        card.add(heading(record.tail, 12))
        if position is not None and position.state is not None:
            state = position.state
            card.add(_pair("Callsign", state.callsign or "not transmitted", True))
            card.add(_pair("Altitude", state.altitude_text()))
            card.add(_pair("Ground speed", state.speed_text()))
            card.add(_pair("Track", state.heading_text()))
            card.add(_pair("Vertical", state.vertical_text()))
            card.add(_pair("Position", f"{state.latitude:.3f}, {state.longitude:.3f} "
                                       f"· {state.age_text()}", True))
        if record.reason:
            card.add(caption(record.reason, "Muted", 8.5))

        card.add(Placard(f"Recent defects ({record.total_defects:,} on file)"))
        if record.defects:
            for row in record.defects:
                line = " · ".join(part for part in (
                    row["reported_at"][:10],
                    f"ATA {row['ata_ref']}" if row["ata_ref"] else "",
                    row["defect_text"][:90]) if part)
                card.add(caption(line, "Muted", 8.5))
        else:
            card.add(caption("No defect recorded against this tail.",
                             "Faint", 8.5))

        card.add(Placard("Compliance"))
        if record.compliance_rows:
            for row in record.compliance_rows:
                item = QWidget()
                line = QHBoxLayout(item)
                line.setContentsMargins(0, 0, 0, 0)
                line.setSpacing(8)
                line.addWidget(StatusBadge(row.badge_kind, row.badge_word,
                                           self.theme_name), 0,
                               Qt.AlignmentFlag.AlignTop)
                text = caption(f"{row.title()} — {row.due.remaining_text()}",
                               "Muted", 8.5)
                text.setToolTip(row.provenance.line())
                line.addWidget(text, 1)
                card.add(item)
        else:
            card.add(caption(
                "No compliance row for this tail. Import a CAMO export on the "
                "Compliance screen.", "Faint", 8.5))

    # ── airport search ────────────────────────────────────────────────
    def _search(self, text: str) -> None:
        query = (text or "").strip()
        self.results.clear()
        self._results = []
        if len(query) < 2:
            self.search_count.setText("")
            return
        if self.ready is None or not self.ready.ok:
            self.search_count.setText("airport data still loading…")
            return
        self._results = self.service.search_airports(query)
        for airport in self._results:
            item = QListWidgetItem(f"{airport.code_line()}\n{airport.name}")
            item.setToolTip(f"{airport.type_label} · {airport.where()}")
            self.results.addItem(item)
        self.search_count.setText(
            f"{len(self._results)} match{'es' if len(self._results) != 1 else ''}"
            if self._results else "no match")

    def _pick(self, row: int) -> None:
        if not 0 <= row < len(self._results):
            return
        airport = self._results[row]
        self._run(lambda: self.service.airport_detail(airport.ident),
                  self._on_airport)

    def _on_airport(self, detail: AirportDetail | None) -> None:
        if detail is None:
            return
        self.detail = detail
        while self.airport_box.count():
            item = self.airport_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        # Two columns, and the split is the point: everything on the left is
        # bundled data that renders with the cable out, everything on the
        # right needs the network. Stacked in one column the weather panel
        # sat below a fifteen-row frequency table and off the screen.
        columns = QWidget()
        row = QHBoxLayout(columns)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(11)

        offline = QVBoxLayout()
        offline.setSpacing(9)
        offline.addWidget(self._identity(detail))
        offline.addWidget(self._runways(detail))
        offline.addWidget(self._frequencies(detail))
        offline.addStretch(1)
        row.addLayout(offline, 1)

        online = QVBoxLayout()
        online.setSpacing(9)
        self.metar_card = _Card("Weather — METAR and TAF")
        online.addWidget(self.metar_card)
        self.movements_card = _Card("Movements — arrivals and departures")
        online.addWidget(self.movements_card)
        online.addStretch(1)
        row.addLayout(online, 1)

        self.airport_box.addWidget(columns)
        self.airport_box.addStretch(1)

        if bool(getattr(self.ctx, "online_enabled", False)):
            self._offline_placeholder(self.metar_card, "Fetching METAR and TAF…")
            self._offline_placeholder(self.movements_card, "Fetching movements…")
            icao = detail.icao
            self._run(lambda: self.service.weather(icao), self._on_weather)
            self._run(lambda: self.service.movements(icao), self._on_movements)
        else:
            self._offline_placeholder(self.metar_card, None)
            self._offline_placeholder(self.movements_card, None)

    def _offline_placeholder(self, card: _Card, message: str | None) -> None:
        card.clear_body()
        if message is None:
            card.setEnabled(False)
            card.add(caption(
                "Unavailable offline. This panel needs online features, which "
                "are switched off in Admin. The airport data above does not.",
                "Muted", 8.5))
        else:
            card.setEnabled(True)
            card.add(caption(message, "Muted", 8.5))

    def _identity(self, detail: AirportDetail) -> QWidget:
        airport = detail.airport
        card = _Card("Airport")
        card.add(heading(airport.name, 13))
        codes = QWidget()
        row = QHBoxLayout(codes)
        row.setContentsMargins(0, 2, 0, 4)
        row.setSpacing(7)
        row.addWidget(MonoLabel(airport.code_line(), 13))
        row.addWidget(Tag(airport.type_label, self.theme_name))
        if airport.scheduled_service:
            row.addWidget(Tag("scheduled service", self.theme_name))
        row.addStretch(1)
        card.add(codes)
        card.add(_pair("Location", airport.where() or "not recorded"))
        card.add(_pair("Position", airport.position_text(), True))
        card.add(_pair("Elevation", airport.elevation_text()))
        card.add(_pair("Local time", detail.local_time_text(), True))
        card.add(caption(
            "Identifiers, position, elevation, runways and frequencies are "
            "bundled offline data; the time zone is derived from the position "
            "and resolved through the tz database, so DST is handled.",
            "Faint", 8))
        return card

    def _runways(self, detail: AirportDetail) -> QWidget:
        card = _Card(f"Runways ({len(detail.runways)})")
        if not detail.runways:
            card.add(caption("No runway recorded for this airport.",
                             "Muted", 8.5))
            return card
        table = _table(["Runway", "Dimensions", "Surface", "Lit", "State"], 1)
        table.setRowCount(len(detail.runways))
        for i, runway in enumerate(detail.runways):
            cells = [runway.designation, runway.dimension_text(),
                     runway.surface_text(), "yes" if runway.lighted else "no",
                     "CLOSED" if runway.closed else "in use"]
            for c, value in enumerate(cells):
                item = QTableWidgetItem(value)
                if c == 0:
                    item.setFont(mono_font(9))
                table.setItem(i, c, item)
        table.setFixedHeight(
            table.horizontalHeader().sizeHint().height()
            + len(detail.runways) * (T.ROW_HEIGHT - 4) + 4)
        card.add(table)
        return card

    def _frequencies(self, detail: AirportDetail) -> QWidget:
        shown = detail.frequencies[:14]
        card = _Card(f"Frequencies ({len(detail.frequencies)})")
        if not shown:
            card.add(caption("No frequency recorded for this airport.",
                             "Muted", 8.5))
            return card
        table = _table(["Type", "MHz", "Description"], 2)
        table.setRowCount(len(shown))
        for i, frequency in enumerate(shown):
            for c, value in enumerate((frequency.type, frequency.mhz_text(),
                                       frequency.description)):
                item = QTableWidgetItem(value)
                if c == 1:
                    item.setFont(mono_font(9))
                table.setItem(i, c, item)
        table.setFixedHeight(
            table.horizontalHeader().sizeHint().height()
            + len(shown) * (T.ROW_HEIGHT - 4) + 4)
        card.add(table)
        return card

    # ── online panels ─────────────────────────────────────────────────
    def _on_weather(self, reports) -> None:
        metar_report, taf_report = reports
        card = self.metar_card
        card.clear_body()
        card.setEnabled(True)

        metar = metar_report.metar
        if metar is None:
            card.add(caption(f"METAR unavailable — {metar_report.error}",
                             "Muted", 8.5))
        else:
            # The raw string comes first and is never conditional. A decoder
            # bug must not be able to hide the observation it misread.
            raw = MonoLabel(metar.raw, 9, QFont.Weight.Normal)
            raw.setWordWrap(True)
            raw.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            card.add(raw)
            if metar.decoded:
                category = metar.flight_category
                if category:
                    badge = QWidget()
                    line = QHBoxLayout(badge)
                    line.setContentsMargins(0, 4, 0, 2)
                    line.setSpacing(8)
                    line.addWidget(StatusBadge(wx.CATEGORY_BADGE[category],
                                               category, self.theme_name))
                    line.addWidget(caption(wx.CATEGORY_NOTE, "Faint", 8), 1)
                    card.add(badge)
                card.add(_pair("Observed", metar.issued_text(), True))
                card.add(_pair("Wind", metar.wind.text()))
                card.add(_pair("Visibility", metar.visibility_text()))
                card.add(_pair("Cloud", metar.cloud_text()))
                card.add(_pair("Ceiling", metar.ceiling_text()))
                card.add(_pair("Weather", metar.weather_text()))
                card.add(_pair("Temperature", metar.temperature_text()))
                card.add(_pair("QNH", metar.qnh_text()))
                if metar.undecoded:
                    card.add(_pair("Not decoded", " ".join(metar.undecoded), True))
            else:
                card.add(caption(
                    "This report could not be decoded. The raw text above is "
                    "what the station issued and is unaltered.", "Muted", 8.5))
            card.add(caption(metar_report.provenance(), "Faint", 8))

        taf = taf_report.taf
        card.add(Placard("TAF"))
        if taf is None:
            card.add(caption(f"TAF unavailable — {taf_report.error}",
                             "Muted", 8.5))
        else:
            card.add(caption(taf.validity_text(), "Muted", 8.5))
            for line in taf.lines():
                entry = MonoLabel(line, 9, QFont.Weight.Normal)
                entry.setWordWrap(True)
                card.add(entry)
            card.add(caption(
                "Change groups are shown as issued and are not decoded — a "
                "BECMG group read as current conditions is a wrong answer that "
                "looks right.", "Faint", 8))

    def _on_movements(self, movements) -> None:
        arrivals, departures = movements
        card = self.movements_card
        card.clear_body()
        card.setEnabled(True)
        for panel, arriving in ((arrivals, True), (departures, False)):
            card.add(Placard("Arrivals" if arriving else "Departures"))
            if not panel.ok:
                card.add(caption(panel.fetch.error, "Muted", 8.5))
                continue
            card.add(caption(f"{len(panel.flights)} recorded · window "
                             f"{panel.window_text()}", "Faint", 8))
            for flight in panel.flights[:8]:
                other = flight.other_end(panel.airport)
                mark = " (airport inferred, several candidates)" \
                    if flight.uncertain(arriving) else ""
                card.add(_pair(
                    flight.callsign or flight.icao24,
                    f"{flight.time_text(arriving)} · "
                    f"{'from' if arriving else 'to'} {other}{mark}", True))
        card.add(caption(
            "Airports at each end are OpenSky's estimate from where a track "
            "started or stopped, not a filed flight plan.", "Faint", 8))
        card.add(caption(arrivals.provenance(), "Faint", 8))

    def refresh_theme(self, theme: str) -> None:
        super().refresh_theme(theme)
        self.map.refresh_theme(theme)
