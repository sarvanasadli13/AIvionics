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

The map itself lives in `ui/mapview.py`; it draws no tiles and reaches for
no network to paint its background.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFrame,
                               QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QSplitter, QTableWidget,
                               QTableWidgetItem, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget)

from ...ops import (adsb, adsblol, airports as apt, movements as mv,
                    net as opsnet, radar as wxradar, weather as wx)
from .. import theme as T
from ..mapview import MapView
from ..opsservice import AirportDetail, OpsService, Ready, TailRecord
from ..widgets import (EmptyState, MonoLabel, Placard, ProvenanceLine,
                       SectionHeader, StatusBadge, Tag, mono_font, ui_font)
from .base import Page, caption, heading, scroll_host

OFFLINE_NOTE = ("Online features are switched off. Airport identifiers, "
                "runways, elevation and local time below are read from bundled "
                "offline data and still work; weather, arrivals and live "
                "positions need the switch in Admin.")


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
        # The last thing the summary line said about the fleet, so that
        # ticking "Fleet only" and unticking it again puts back what was
        # there rather than an empty strip.
        self._fleet_summary = ""

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

        controls = QFrame()
        controls.setObjectName("Band")
        cl = QHBoxLayout(controls)
        cl.setContentsMargins(15, 8, 15, 8)
        cl.setSpacing(12)

        # Finding one aircraft among three hundred chevrons. Typing updates
        # the count only; Enter is what moves the map. A search that recentred
        # on every keystroke walked the view across the world while somebody
        # typed a five-character tail, and cost a traffic fetch at each stop.
        self.find_box = QLineEdit()
        self.find_box.setPlaceholderText(
            "Find aircraft — tail, callsign, transponder address or type")
        self.find_box.setClearButtonEnabled(True)
        self.find_box.setMinimumHeight(28)
        self.find_box.setMaximumWidth(340)
        self.find_box.textChanged.connect(self._count_matches)
        self.find_box.returnPressed.connect(self._find_aircraft)
        cl.addWidget(self.find_box)
        self.find_state = caption("", "Faint", 8.5)
        cl.addWidget(self.find_state)

        self.fleet_toggle = QCheckBox("Fleet only")
        self.fleet_toggle.setToolTip(
            "Hide every contact that is not one of ours. Nothing is discarded "
            "and nothing is re-fetched — unticking brings them straight back.")
        self.fleet_toggle.toggled.connect(self._toggle_fleet_only)
        cl.addWidget(self.fleet_toggle)

        self.radar_toggle = QCheckBox("Weather radar")
        self.radar_toggle.setToolTip(
            "Ground-based precipitation radar over the visible area")
        self.radar_toggle.toggled.connect(self._toggle_radar)
        cl.addWidget(self.radar_toggle)
        self.radar_state = caption("", "Faint", 8.5)
        cl.addWidget(self.radar_state, 1)
        lay.addWidget(controls)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.map = MapView(self.theme_name)
        self.map.tail_selected.connect(self._show_tail)
        self.map.aircraft_selected.connect(self._show_contact)
        # Panning fires continuously; a fetch per frame would burn the free
        # tier in a minute. The view has to settle first.
        self._traffic_timer = QTimer(self)
        self._traffic_timer.setSingleShot(True)
        self._traffic_timer.setInterval(700)
        self._traffic_timer.timeout.connect(self._fetch_traffic)
        self.map.viewport_changed.connect(self._traffic_timer.start)
        self._radar_timer = QTimer(self)
        self._radar_timer.setSingleShot(True)
        self._radar_timer.setInterval(900)
        self._radar_timer.timeout.connect(self._fetch_radar)
        self.map.viewport_changed.connect(self._maybe_radar)
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
        self.traffic_card = _Card("Live traffic in view")
        self.traffic_card.add(caption(
            "Zoom in to load what else the network can see here. Contacts are "
            "drawn as small chevrons; click one to read it. Nothing about a "
            "contact is airworthiness data \u2014 it is a position report.",
            "Muted", 8.5))
        sl.addWidget(self.traffic_card)
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

        # Once an airport is chosen the search has done its job, and the band
        # plus a 280 px result list is a third of the screen spent on a box
        # nobody is typing in any more (R5). It folds into one line, and the
        # way back is the same control.
        self.search_back = QToolButton()
        self.search_back.setObjectName("LinkBtn")
        self.search_back.setText("←  Search again")
        self.search_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.search_back.setFont(ui_font(9, QFont.Weight.DemiBold))
        self.search_back.clicked.connect(self._reopen_search)
        self.search_back.hide()
        bl.addWidget(self.search_back)
        self.search_chosen = caption("", "Muted", 9.5)
        self.search_chosen.hide()
        bl.addWidget(self.search_chosen, 1)
        lay.addWidget(band)

        split = self.airport_split = QSplitter(Qt.Orientation.Horizontal)
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
        self.photo_card = _Card("Photograph")
        self.photo_card.hide()
        self.airport_box.addWidget(self.photo_card)
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
            self._fleet_summary = (
                "Live positions are unavailable offline — switch online "
                "features on in Admin to populate the map.")
            self.map_summary.setText(self._fleet_summary)
            self._render_unseen(None)
        # The map explains itself in both cases, and "the switch is off" is
        # the state that explains every other symptom under it.
        self._fetch_traffic()

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
            # The denser tiers only matter once the view goes in; handing the
            # index over lets the map thicken its own backdrop with zoom.
            self.map.set_reference_index(apt.index())
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
        self._fleet_summary = (
            f"{snapshot.summary()} · {snapshot.provenance()} · "
            f"{adsblol.ATTRIBUTION}"
            if snapshot.ok else
            f"No live positions — {snapshot.fetch.error}. "
            f"{snapshot.summary()}")
        if not self.fleet_toggle.isChecked():
            self.map_summary.setText(self._fleet_summary)
        self._render_unseen(snapshot)
        # A fetch may have brought in the aircraft somebody is looking for.
        self._count_matches()

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

    # ── precipitation radar (R3) ──────────────────────────────────────
    def _toggle_radar(self, on: bool) -> None:
        if not on:
            self.map.set_radar(None)
            self.radar_state.setText("")
            return
        self._fetch_radar()

    def _maybe_radar(self) -> None:
        if self.radar_toggle.isChecked():
            self._radar_timer.start()

    def _fetch_radar(self) -> None:
        if not self.radar_toggle.isChecked():
            return
        if not bool(getattr(self.ctx, "online_enabled", False)):
            self.map.set_radar(None)
            self.radar_state.setText(
                "Radar needs online features, which are off in Admin.")
            return
        bounds = self.map.visible_bounds()
        width = max(1, self.map.width())
        self.radar_state.setText("Loading radar\u2026")
        self._run(lambda: self.service.radar(bounds, width), self._on_radar)

    def _on_radar(self, result) -> None:
        index, tiles = result
        if not tiles.ok:
            self.map.set_radar(None)
            self.radar_state.setText(f"Radar unavailable \u2014 {tiles.error}")
            return
        stamp = tiles.frame.label() if tiles.frame else ""
        self.map.set_radar(
            tiles.tiles, f"{wxradar.ATTRIBUTION}  \u00b7  frame {stamp}")
        self.radar_state.setText(
            f"Radar {stamp} \u00b7 {len(tiles.tiles)} tiles \u00b7 "
            f"{wxradar.COVERAGE_WARNING}")

    # ── finding one aircraft, and hiding the rest (Phase 8) ───────────
    def _toggle_fleet_only(self, on: bool) -> None:
        self.map.set_fleet_only(on)
        self.map_summary.setText(
            "Showing fleet tails only. Contacts are still loaded and still "
            "counted below — they are hidden, not discarded."
            if on else self._fleet_summary)

    def _count_matches(self) -> None:
        """How many aircraft on this map answer to what has been typed.

        In memory over what is already loaded, so it costs nothing and opens
        nothing. It searches the visible area only, and says so when it finds
        nothing — otherwise "no match" reads as "that aircraft is not flying".
        """
        query = self.find_box.text().strip()
        if len(query) < 2:
            self.find_state.setText("")
            return
        matches = self.map.find(query)
        self.find_state.setText(
            f"{len(matches)} match{'' if len(matches) == 1 else 'es'} · "
            f"Enter to go to the first"
            if matches else
            "no match on this map — only what is loaded for the visible "
            "area is searched")

    def _find_aircraft(self) -> None:
        matches = self.map.find(self.find_box.text().strip())
        if not matches:
            self._count_matches()
            return
        state = matches[0]
        # `select_match` reports whether it turned out to be one of ours,
        # because a fleet tail gets the maintenance panel and a contact must
        # not — there is no record behind a contact to show.
        if self.map.select_match(state):
            self._show_tail(self.map.selected)
        else:
            self._show_contact(state)
        self.find_state.setText(
            f"{state.identity} · {len(matches)} "
            f"match{'' if len(matches) == 1 else 'es'}")

    def _set_tracking(self, state: adsb.TrackingState) -> None:
        """Why the map looks the way it does — on the map, and in the panel.

        Both, because they answer for different things: the words over the
        empty rectangle stop it reading as "no aircraft here", and the panel
        line is what stays visible once markers appear.
        """
        self.map.set_tracking_state(state)
        if state.blank_is_explained:
            self._traffic_note(state.line())

    # ── live traffic in the visible box (R2) ──────────────────────────
    def _fetch_traffic(self) -> None:
        if not bool(getattr(self.ctx, "online_enabled", False)):
            self.map.set_traffic(())
            self._set_tracking(adsb.tracking_state(online=False))
            return
        bounds = self.map.visible_bounds()
        if adsblol.area_too_large(*bounds):
            # The same refusal `adsblol.area_traffic` would return, raised
            # here so no worker thread is spent learning it. Handing it
            # through `tracking_state` keeps one copy of the wording.
            self.map.set_traffic(())
            self._set_tracking(adsb.tracking_state(
                online=True,
                traffic=adsb.AreaTraffic(
                    fetch=opsnet.Fetch(
                        source=adsblol.SOURCE,
                        error="zoom in to load live traffic — the visible "
                              "area is wider than one request may cover"),
                    bounds=bounds)))
            return
        self._set_tracking(adsb.tracking_state(online=True, loading=True))
        self._run(lambda: self.service.area_traffic(bounds), self._on_traffic)

    def _on_traffic(self, traffic) -> None:
        self.map.set_traffic(traffic.states if traffic.ok else (),
                             adsblol.ATTRIBUTION if traffic.ok else "")
        # Classified rather than summarised: "no aircraft in view" and "the
        # network has no receivers over this country" produce the same empty
        # rectangle, and only one of them is about the sky.
        state = adsb.tracking_state(online=True, traffic=traffic)
        self._set_tracking(state)
        if state.is_ok:
            self._traffic_note(
                f"{traffic.summary()}.  Click a chevron to read one.")
        # What was typed in the finder may now match something that has just
        # arrived, or may have stopped matching what has just left.
        self._count_matches()

    def _traffic_note(self, text: str) -> None:
        self.traffic_card.clear_body()
        self.traffic_card.add(caption(text, "Muted", 8.5))
        # ODbL 1.0 requires the credit wherever the data is shown, and this
        # panel shows that data in words even when the map has no markers on
        # it to carry the credit the map paints.
        self.traffic_card.add(caption(adsblol.ATTRIBUTION, "Faint", 8))

    def _show_contact(self, state) -> None:
        """A contact is not one of ours, and the panel says so first.

        Everything below comes from one ADS-B position report and nothing
        else \u2014 there is no maintenance record behind it, and implying
        otherwise by rendering it in the same shape as a tail would be a
        quiet lie.
        """
        card = self.tail_card
        card.clear_body()
        card.add(heading(state.identity, 13))
        card.add(StatusBadge("info", "NOT IN YOUR FLEET", self.theme_name))
        if state.aircraft_type:
            card.add(_pair("Type", state.aircraft_type, True))
        if state.callsign.strip() and state.callsign.strip() != state.identity:
            card.add(_pair("Callsign", state.callsign.strip(), True))
        card.add(_pair("Transponder", state.icao24.upper(), True))
        if state.origin_country:
            card.add(_pair("Registered", state.origin_country))
        card.add(_pair("Position", f"{state.latitude:.4f}, {state.longitude:.4f}", True))
        card.add(_pair("Altitude", state.altitude_text(), True))
        card.add(_pair("Speed", state.speed_text(), True))
        card.add(_pair("Track", state.heading_text(), True))
        card.add(_pair("Vertical", state.vertical_text(), True))
        if state.squawk:
            card.add(_pair("Squawk", state.squawk, True))
        card.add(_pair("Contact", state.age_text()))
        # Which feed said this. Two now populate the same model and they
        # disagree about which fields exist, so a readout that cannot name
        # its source cannot be checked by the person reading it.
        card.add(_pair("Source", state.source_text()))
        if state.is_stale():
            # Said as a state rather than left to the reader to work out from
            # the contact age: a position drawn as current is a marker in the
            # wrong place, and the marker itself cannot say so.
            card.add(caption(
                "STALE — no contact newer than "
                f"{int(adsb.STALE_AFTER_S)} s. This is where the aircraft "
                "was when it was last heard, not where it is now.",
                "Muted", 8.5))
        card.add(caption(
            "One position report from a volunteer ADS-B receiver. This "
            "application holds no maintenance record for this aircraft.",
            "Faint", 8))
        card.add(caption(adsblol.ATTRIBUTION, "Faint", 8))

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
            if state.squawk:
                card.add(_pair("Squawk", state.squawk, True))
            card.add(_pair("Source", state.source_text()))
            if state.is_stale():
                card.add(caption(
                    "STALE — this position is older than "
                    f"{int(adsb.STALE_AFTER_S)} s. The marker is where this "
                    "tail was last heard, not where it is.", "Muted", 8.5))
            # The position block above is adsb.lol data rendered in words,
            # and ODbL 1.0 wants the credit wherever that data appears — not
            # only on the map that draws it.
            card.add(caption(adsblol.ATTRIBUTION, "Faint", 8))
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
            # The city is what people actually search by, so it goes in
            # the row rather than a tooltip nobody hovers for (R4).
            where = airport.where()
            item = QListWidgetItem(
                f"{airport.code_line()}\n{airport.name}"
                + (f"\n{where}" if where else ""))
            item.setToolTip(
                f"{airport.type_label} · {where or 'location not recorded'}")
            self.results.addItem(item)
        self.search_count.setText(
            f"{len(self._results)} match{'es' if len(self._results) != 1 else ''}"
            if self._results else "no match")

    def _pick(self, row: int) -> None:
        if not 0 <= row < len(self._results):
            return
        airport = self._results[row]
        self._collapse_search(airport)
        self._run(lambda: self.service.airport_detail(airport.ident),
                  self._on_airport)

    def _collapse_search(self, airport: apt.Airport) -> None:
        """Fold the search away and give the space to the airport (R5)."""
        where = airport.where()
        sep = "  ·  "
        self.search.hide()
        self.search_count.hide()
        self.search_back.show()
        self.search_chosen.setText(
            f"{airport.code_line()}{sep}{airport.name}"
            + (f"{sep}{where}" if where else ""))
        self.search_chosen.show()
        self.airport_split.setSizes([0, max(1, self.airport_split.width())])

    def _reopen_search(self) -> None:
        self.search_back.hide()
        self.search_chosen.hide()
        self.search.show()
        self.search_count.show()
        self.airport_split.setSizes([280, 980])
        self.search.setFocus()
        self.search.selectAll()

    def _on_airport(self, detail: AirportDetail | None) -> None:
        if detail is None:
            return
        self.detail = detail
        # "Where is it" is half of what an airport page is for (R4). The map
        # is marked as soon as the airport is read, so switching tabs shows
        # it already placed rather than starting another search.
        airport = detail.airport
        self.map.set_highlight(airport.latitude, airport.longitude,
                               airport.code_line())
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

        # The photograph goes back on top: it is the fastest way to know you
        # picked the right airport, which is what the space freed by folding
        # the search away is for (R5).
        self.photo_card = _Card("Photograph")
        self.airport_box.insertWidget(0, self.photo_card)

        if bool(getattr(self.ctx, "online_enabled", False)):
            self._offline_placeholder(self.metar_card, "Fetching METAR and TAF…")
            self._offline_placeholder(self.movements_card, "Fetching movements…")
            icao = detail.icao
            self._run(lambda: self.service.weather(icao), self._on_weather)
            self._run(lambda: self.service.movement_boards(icao),
                      self._on_movements)
            name, where = airport.name, airport.where()
            self._offline_placeholder(self.photo_card,
                                      "Looking for a photograph\u2026")
            self._run(lambda: self.service.airport_photo(name, where),
                      self._on_photo)
        else:
            self._offline_placeholder(self.metar_card, None)
            self._offline_placeholder(self.movements_card, None)
            self._offline_placeholder(self.photo_card, None)

    def _on_photo(self, result) -> None:
        """Render the picture and, always, who took it and under what licence.

        The credit is not decoration and not a tooltip: CC-BY and CC-BY-SA are
        the terms these images come under, and displaying the image without
        the attribution is simply using it outside its licence.
        """
        photo, raw = result
        card = self.photo_card
        card.clear_body()
        card.setEnabled(True)
        if not photo.ok or not raw:
            card.add(caption(
                "No photograph found for this airport. Most airfields do not "
                "have one published \u2014 this is a normal result, not a "
                "failed fetch.", "Muted", 8.5))
            return

        pixmap = QPixmap()
        if not pixmap.loadFromData(raw):
            card.add(caption("The image could not be decoded.", "Muted", 8.5))
            return
        view = QLabel()
        view.setPixmap(pixmap.scaledToWidth(
            min(760, pixmap.width()), Qt.TransformationMode.SmoothTransformation))
        view.setAlignment(Qt.AlignmentFlag.AlignLeft)
        card.add(view)
        card.add(caption(f"{photo.credit()}  \u00b7  via Wikimedia Commons",
                         "Muted", 8.5))
        card.add(caption(
            f"{photo.title}  \u00b7  from the article \u201c{photo.article}"
            f"\u201d. Fetched now, not bundled with this application.",
            "Faint", 8))

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

        show = QToolButton()
        show.setObjectName("LinkBtn")
        show.setText("Show on the map")
        show.setCursor(Qt.CursorShape.PointingHandCursor)
        show.setFont(ui_font(9, QFont.Weight.DemiBold))
        show.setToolTip("Centre the fleet map on this airport")
        show.clicked.connect(lambda: self._show_on_map(detail))
        card.add(show)

        card.add(caption(
            "Identifiers, position, elevation, runways and frequencies are "
            "bundled offline data; the time zone is derived from the position "
            "and resolved through the tz database, so DST is handled.",
            "Faint", 8))
        return card

    def _show_on_map(self, detail: AirportDetail) -> None:
        """Jump to the map with this airport centred and marked."""
        airport = detail.airport
        self.map.set_highlight(airport.latitude, airport.longitude,
                               airport.code_line())
        self.map.focus_on(airport.latitude, airport.longitude, 28.0)
        self.tabs.setCurrentIndex(0)

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

    def _on_movements(self, result) -> None:
        """Arrivals and departures from whichever level could answer.

        The panel used to be wired straight to OpenSky's `flights/*`
        endpoints under the title "Movements", which reads as "now" and is a
        nightly batch. It now renders whatever `movements.select` chose, and
        three things are on screen before any row is: which level answered,
        what every level above it said about itself, and what kind of claim
        each row actually is. A reader who cannot tell a confirmed arrival
        from a transponder that stopped reporting airborne near an airport
        has been given the wrong screen, however tidy it looks.
        """
        selection, arrivals, departures = result
        card = self.movements_card
        card.clear_body()
        card.setEnabled(True)

        # Which level answered, and why not the ones above it. This is the
        # "source unavailable" state, and it is stated once at the top rather
        # than left implicit in two identical empty lists below.
        card.add(caption(selection.summary(), "Muted", 8.5))
        for line in selection.reasons():
            card.add(caption("· " + line, "Faint", 8))

        for board, arriving in ((arrivals, True), (departures, False)):
            card.add(Placard("Arrivals" if arriving else "Departures"))
            self._movement_board(card, board, arriving)

        # The notes belong to whichever level answered, so they are read off
        # the board rather than hard-coded to OpenSky's batch warning.
        for note in dict.fromkeys(arrivals.notes + departures.notes):
            card.add(caption(note, "Faint", 8))
        card.add(caption(arrivals.provenance(), "Faint", 8))

    def _movement_board(self, card: _Card, board, arriving: bool) -> None:
        """One direction: its state first, then whatever rows survived it.

        The state is rendered even when there are rows, because the one that
        matters most — a cached list served after a failed fetch — is the
        state where rows *do* exist and every time on them has stopped
        moving.
        """
        state = mv.board_state(board)
        if not state.is_ok:
            card.add(caption(state.line(), "Muted", 8.5))
        if not board.movements:
            return
        card.add(caption(board.headline(), "Faint", 8))
        for movement in board.movements[:8]:
            # `time_text` carries its own basis — "scheduled", "estimated" or
            # "actual" — because a bare clock time beside the word "landed"
            # reads as an on-time report that nothing here measured.
            card.add(_pair(
                movement.identity,
                f"{movement.time_text()} · "
                f"{'from' if arriving else 'to'} {movement.other_end} · "
                f"{movement.status}", True))
            # Per row, not per panel: a board can mix a confirmed movement
            # with an inferred one the moment a second level is connected.
            card.add(caption("   " + movement.provenance(), "Faint", 8))
            if movement.gate or movement.terminal:
                card.add(caption("   " + movement.place_text(), "Faint", 8))
        if len(board.movements) > 8:
            card.add(caption(
                f"   {len(board.movements) - 8} more not shown.", "Faint", 8))

    def refresh_theme(self, theme: str) -> None:
        super().refresh_theme(theme)
        self.map.refresh_theme(theme)
