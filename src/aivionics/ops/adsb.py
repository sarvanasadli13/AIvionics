"""Live positions and airport movements from OpenSky (PLAN 4B.4).

OpenSky's free tier is a **volunteer receiver network**, and two consequences
run through everything below because pretending otherwise is how a map like
this becomes dangerous:

* **Coverage is incomplete by design.** Reception is dense over Europe and
  patchy elsewhere, and there is no coverage at all over most oceans. A tail
  that returns no position has *not been seen by this network* — it is not
  on the ground, not parked, and not missing. `FleetSnapshot.unseen` carries
  that distinction as a first-class result rather than an empty row, and
  every screen that renders this must say `COVERAGE_WARNING`.
* **The budget is small.** Anonymous access is roughly 400 credits a day. A
  five-minute cache TTL is 288 refreshes in 24 h, which fits with room for
  the arrivals and departures panels; a 30-second one would be 2,880 and
  would exhaust the allowance before lunch. The TTL is derived from that
  arithmetic, not chosen for feel.

* **The movement endpoints are not live and never were.** `flights/arrival`
  and `flights/departure` are rebuilt by a batch process at night, per
  OpenSky's own documentation. Everything built on them carries `recorded` in
  its name (Phase 8) because they used to sit in a panel called "Movements"
  next to live positions, which reads as the current picture and is not. A
  registered account raises the credit allowance from 400 to 4,000 a day and
  changes the lag not at all.

Tails are matched to transponder addresses through `aircraft.icao24`, the
column `compliance.ensure_schema` adds. An aircraft with no `icao24` on file
cannot be tracked, and that too is reported rather than silently skipped.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .net import Fetch, NetClient

SOURCE = "OpenSky Network (free tier)"
BASE = "https://opensky-network.org/api"

# See the module note: 400 credits/day anonymous, 288 refreshes at this TTL.
STATES_TTL = 300.0
FLIGHTS_TTL = 900.0

# The longest a caller may block waiting out the rate limiter before giving
# up and letting the panel say so. Only ever paid on a worker thread.
SOURCE_WAIT_CEILING = 6.0

# OpenSky serves flight history with a delay and rejects windows over 7 days.
FLIGHTS_WINDOW = timedelta(hours=12)
FLIGHTS_MAX_WINDOW = timedelta(days=7)

# What the flights endpoints are actually worth, anonymously. This is not a
# hedge: EDDF returned a single arrival for a twelve-hour window in testing.
# A panel that prints "1 recorded" without this line is telling the reader
# something false about Frankfurt.
#
# The first sentence was added in Phase 8 and is the one that matters most,
# because it is a fact from OpenSky's own documentation rather than an
# inference from a test: the flights tables are *"updated by a batch process
# at night"*. Nothing built on them is live, whatever the rest of the screen
# happens to be doing, and a registered account raises the credit allowance
# without touching the lag.
MOVEMENTS_WARNING = (
    "RECORDED HISTORY, NOT LIVE. OpenSky rebuilds its arrival and departure "
    "tables in a nightly batch, so this list lags by hours and today's "
    "movements may be missing entirely. "
    "OpenSky attributes movements from where a track started or stopped, and "
    "the anonymous free tier sees only a fraction of them, hours late. A short "
    "list is a limit of this feed, not a quiet airport \u2014 do not read the "
    "count as traffic.")

# The heading a recorded-movements panel carries. Kept beside the warning so
# the two cannot drift apart.
MOVEMENTS_LABEL = "Recorded movements \u2014 nightly batch, hours behind"

COVERAGE_WARNING = (
    "OpenSky is a volunteer ADS-B receiver network with incomplete coverage "
    "by design — dense over Europe, patchy elsewhere, absent over most "
    "oceans. An aircraft missing from this map has not been seen by the "
    "network; it is not necessarily on the ground. This is situational "
    "awareness, not a traffic display, and nothing here is airworthiness "
    "or separation data.")

_ICAO24 = re.compile(r"^[0-9a-f]{6}$")

METRES_TO_FEET = 3.280839895
MS_TO_KNOTS = 1.943844
MS_TO_FPM = 196.850394

# After this long without a contact, a marker is drawn as stale rather than
# as current. Two minutes is a compromise from the feeds themselves: adsb.lol
# refreshes positions every few seconds where it has receivers, so a gap this
# long means the aircraft has left coverage rather than that the network is
# slow. Below a minute the markers at the edge of a receiver's range would
# flicker between fresh and stale on every fetch.
STALE_AFTER_S = 120.0


def normalise_icao24(value: object) -> str:
    """Lowercase 6-hex transponder address, or "" when it is not one."""
    text = str(value or "").strip().lower().replace("0x", "")
    return text if _ICAO24.match(text) else ""


@dataclass(frozen=True, slots=True)
class StateVector:
    """One aircraft as OpenSky last saw it. Units converted, gaps kept as None."""

    icao24: str
    callsign: str = ""
    origin_country: str = ""
    longitude: float | None = None
    latitude: float | None = None
    baro_altitude_m: float | None = None
    geo_altitude_m: float | None = None
    on_ground: bool = False
    velocity_ms: float | None = None
    true_track: float | None = None
    vertical_rate_ms: float | None = None
    squawk: str = ""
    last_contact: datetime | None = None
    # Published by community feeds (adsb.lol), never by OpenSky's anonymous
    # state vectors. Blank means "this feed did not say", not "unregistered".
    registration: str = ""
    aircraft_type: str = ""
    # Which feed said this. Carried on the vector rather than inferred at the
    # call site because two feeds now populate the same model, they disagree
    # about which fields exist, and a readout that cannot name its source
    # cannot be checked by the person reading it.
    source: str = ""

    @property
    def identity(self) -> str:
        """What to call this aircraft on screen, best available first."""
        return (self.registration or self.callsign.strip()
                or self.icao24.upper())

    @property
    def has_position(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def altitude_ft(self) -> int | None:
        metres = self.baro_altitude_m
        if metres is None:
            metres = self.geo_altitude_m
        return None if metres is None else int(round(metres * METRES_TO_FEET))

    @property
    def speed_kt(self) -> int | None:
        return (None if self.velocity_ms is None
                else int(round(self.velocity_ms * MS_TO_KNOTS)))

    @property
    def vertical_rate_fpm(self) -> int | None:
        return (None if self.vertical_rate_ms is None
                else int(round(self.vertical_rate_ms * MS_TO_FPM)))

    def altitude_text(self) -> str:
        if self.on_ground:
            return "on ground"
        altitude = self.altitude_ft
        if altitude is None:
            return "altitude not reported"
        source = "barometric" if self.baro_altitude_m is not None else "GNSS"
        return f"{altitude:,} ft ({source})"

    def speed_text(self) -> str:
        speed = self.speed_kt
        return "not reported" if speed is None else f"{speed} kt"

    def heading_text(self) -> str:
        if self.true_track is None:
            return "track not reported"
        return f"{self.true_track:03.0f}° true"

    def vertical_text(self) -> str:
        rate = self.vertical_rate_fpm
        if rate is None:
            return "vertical rate not reported"
        if abs(rate) < 100:
            return "level"
        return f"{'climbing' if rate > 0 else 'descending'} {abs(rate):,} fpm"

    def age_seconds(self, now: datetime | None = None) -> float | None:
        if self.last_contact is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.last_contact).total_seconds())

    def age_text(self, now: datetime | None = None) -> str:
        seconds = self.age_seconds(now)
        if seconds is None:
            return "last contact not reported"
        seconds = int(seconds)
        if seconds < 90:
            return f"last seen {seconds} s ago"
        return f"last seen {seconds // 60} min ago"

    def is_stale(self, now: datetime | None = None,
                 limit: float = None) -> bool:
        """True when this position is too old to be treated as where it is now.

        A vector with no `last_contact` counts as stale rather than fresh. The
        feed not saying when it saw an aircraft is not evidence that it saw it
        just now, and the safe reading of an unknown age is the pessimistic
        one — a marker drawn as current on a five-minute-old position is a
        marker in the wrong place with nothing on screen to say so.
        """
        seconds = self.age_seconds(now)
        return True if seconds is None else seconds > (
            STALE_AFTER_S if limit is None else limit)

    def position_text(self) -> str:
        if not self.has_position:
            return "position not reported"
        return f"{self.latitude:.4f}, {self.longitude:.4f}"

    def source_text(self) -> str:
        return self.source or "source not recorded"


def _f(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _stamp(value: object) -> datetime | None:
    seconds = _f(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_state(row: list) -> StateVector | None:
    """One OpenSky state vector. Positional by protocol, so guard the length.

    The API returns a bare array whose meaning is its index. A short row is a
    protocol change, and the right response is to drop that aircraft rather
    than to read field 9 as if it were field 10.
    """
    if not isinstance(row, (list, tuple)) or len(row) < 12:
        return None
    icao24 = normalise_icao24(row[0])
    if not icao24:
        return None
    return StateVector(
        icao24=icao24,
        callsign=str(row[1] or "").strip(),
        origin_country=str(row[2] or "").strip(),
        longitude=_f(row[5]),
        latitude=_f(row[6]),
        baro_altitude_m=_f(row[7]),
        on_ground=bool(row[8]),
        velocity_ms=_f(row[9]),
        true_track=_f(row[10]),
        vertical_rate_ms=_f(row[11]),
        geo_altitude_m=_f(row[13]) if len(row) > 13 else None,
        squawk=str(row[14] or "").strip() if len(row) > 14 else "",
        last_contact=_stamp(row[4]),
        source=SOURCE,
    )


# ── finding one aircraft among several hundred (Phase 8) ────────────────
# A busy European view is three hundred chevrons, and picking the one you
# came for by eye is not a thing anybody does twice. Four identifiers are
# accepted because four are in circulation and people arrive with whichever
# one their paperwork used: the tail, the callsign, the transponder address
# and the type.

def state_rank(state: StateVector, query: str) -> int:
    """How well `state` answers `query`. Lower is better, -1 is no match.

    Exact identifiers beat prefixes and prefixes beat the type, because a
    type search matches every 737 in the sky and an exact tail matches one
    aircraft. Sorting on this rather than filtering keeps the best answer at
    the top instead of whichever aircraft the feed happened to list first.
    """
    wanted = (query or "").strip().upper().replace("-", "")
    if len(wanted) < 2:
        return -1
    tail = state.registration.upper().replace("-", "")
    callsign = state.callsign.strip().upper()
    address = state.icao24.upper()
    kind = state.aircraft_type.upper()

    if wanted in (tail, address, callsign):
        return 0
    if tail.startswith(wanted) or address.startswith(wanted):
        return 1
    if callsign.startswith(wanted):
        return 2
    if wanted in tail or wanted in callsign:
        return 3
    if kind and (kind == wanted or kind.startswith(wanted)):
        return 4
    return -1


def search_states(states, query: str, limit: int = 40) -> list[StateVector]:
    """Every aircraft in `states` matching `query`, best first."""
    scored = []
    for state in states or ():
        rank = state_rank(state, query)
        if rank >= 0:
            scored.append((rank, state.identity, state))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [state for _rank, _identity, state in scored[:limit]]


# ── tails ───────────────────────────────────────────────────────────────

def tail_to_icao24(con: sqlite3.Connection | None) -> dict[str, str]:
    """Tail -> transponder address, for tails that have one on file.

    Read-only and tolerant: this runs against a database that may be missing,
    locked by an ingest, or predate `compliance.ensure_schema`.
    """
    if con is None:
        return {}
    try:
        rows = con.execute(
            "SELECT tail, icao24 FROM aircraft WHERE icao24 IS NOT NULL "
            "AND TRIM(icao24) <> ''").fetchall()
    except sqlite3.Error:
        return {}
    mapping = {}
    for tail, address in rows:
        normalised = normalise_icao24(address)
        if tail and normalised:
            mapping[str(tail).strip().upper()] = normalised
    return mapping


def untracked_tails(con: sqlite3.Connection | None) -> list[str]:
    """Tails with no transponder address on file — trackable only once set."""
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT tail FROM aircraft WHERE icao24 IS NULL "
            "OR TRIM(icao24) = '' ORDER BY tail").fetchall()
    except sqlite3.Error:
        return []
    return [str(tail).strip().upper() for (tail,) in rows if tail]


# ── live positions ──────────────────────────────────────────────────────

def states_url(icao24s: list[str] | tuple[str, ...]) -> str:
    """States for specific aircraft. Filtered, never the whole world feed.

    `states/all` unfiltered is both a large response and an expensive one in
    credits, and this application only ever wants its own fleet.
    """
    addresses = sorted({normalise_icao24(a) for a in icao24s} - {""})
    query = urlencode([("icao24", address) for address in addresses])
    return f"{BASE}/states/all?{query}"


# Area queries (BACKLOG R2). The module note is emphatic that `states/all`
# *unfiltered* is not to be used, and that stands: what follows is always
# bounded by the rectangle currently on screen, which is a small fraction of
# the world and gets smaller the further in you go. The zoom floor below is
# the practical expression of that rule - a continent-sized box is refused
# rather than sent.
AREA_TTL = 60.0
AREA_MAX_SPAN_DEG = 30.0        # refuse a box larger than this on either axis
AREA_MAX_AIRCRAFT = 900         # a sanity cap on what gets drawn


def area_url(lat_min: float, lon_min: float,
             lat_max: float, lon_max: float) -> str:
    """States inside one bounding box. Never the unfiltered world feed."""
    query = urlencode({"lamin": round(float(lat_min), 4),
                       "lomin": round(float(lon_min), 4),
                       "lamax": round(float(lat_max), 4),
                       "lomax": round(float(lon_max), 4)})
    return f"{BASE}/states/all?{query}"


def area_too_large(lat_min: float, lon_min: float,
                   lat_max: float, lon_max: float) -> bool:
    return (abs(lat_max - lat_min) > AREA_MAX_SPAN_DEG
            or abs(lon_max - lon_min) > AREA_MAX_SPAN_DEG)


@dataclass(frozen=True)
class AreaTraffic:
    """Everything the network saw inside one box, plus why it might be empty."""

    fetch: Fetch
    states: tuple = ()
    bounds: tuple = ()

    @property
    def ok(self) -> bool:
        return self.fetch.ok

    def summary(self) -> str:
        if not self.fetch.ok:
            return self.fetch.error or "not fetched"
        return (f"{len(self.states):,} aircraft in view"
                if self.states else "no aircraft in view")


def area_traffic(client: NetClient, lat_min: float, lon_min: float,
                 lat_max: float, lon_max: float, *,
                 ttl: float = AREA_TTL) -> AreaTraffic:
    """Live traffic inside the visible rectangle (R2).

    Returns an `AreaTraffic` in every case, offline included, so the caller
    renders a reason rather than an empty map with no explanation.
    """
    bounds = (lat_min, lon_min, lat_max, lon_max)
    if area_too_large(*bounds):
        return AreaTraffic(
            fetch=Fetch(source=SOURCE,
                        error="zoom in to load live traffic - the visible "
                              "area is too large to request"),
            bounds=bounds)

    fetched = client.get_json(area_url(*bounds), SOURCE, ttl=ttl)
    if not fetched.ok:
        return AreaTraffic(fetch=fetched, bounds=bounds)

    payload = fetched.data if isinstance(fetched.data, dict) else {}
    states = []
    for row in payload.get("states") or []:
        state = parse_state(row)
        if state is not None and state.has_position:
            states.append(state)
        if len(states) >= AREA_MAX_AIRCRAFT:
            break
    return AreaTraffic(fetch=fetched, states=tuple(states), bounds=bounds)


# ── what an empty map means (Phase 8) ───────────────────────────────────
# A map with no markers on it has at least five different causes and they
# call for five different responses, but they all look identical: an empty
# rectangle. Left unlabelled, the reader supplies the most dangerous reading
# themselves — "there are no aircraft here". Every one of these states is
# rendered on the map, in words, over the empty space.

TRACKING_OK = "ok"
TRACKING_LOADING = "loading"
TRACKING_OFFLINE = "offline"
TRACKING_STALE = "stale"
TRACKING_NO_COVERAGE = "no-coverage"
TRACKING_ZOOMED_OUT = "zoomed-out"
TRACKING_ERROR = "error"


@dataclass(frozen=True)
class TrackingState:
    """Why the map looks the way it does, in words the map itself renders."""

    state: str = TRACKING_LOADING
    headline: str = ""
    detail: str = ""

    @property
    def is_ok(self) -> bool:
        return self.state == TRACKING_OK

    @property
    def blank_is_explained(self) -> bool:
        """True when this state gives the reader a reason for an empty map."""
        return bool(self.headline) and self.state != TRACKING_OK

    def line(self) -> str:
        return " — ".join(part for part in (self.headline, self.detail) if part)


def tracking_state(*, online: bool, traffic: "AreaTraffic | None" = None,
                   loading: bool = False,
                   now: datetime | None = None) -> TrackingState:
    """Classify the traffic layer. Pure, so the wording can be tested.

    Ordered by what the reader most needs to know first: a switched-off
    application explains every other symptom, so it is checked before
    anything that would otherwise look like a coverage problem.
    """
    if not online:
        return TrackingState(
            TRACKING_OFFLINE, "Live tracking is off",
            "Online features are switched off in Admin. Nothing is being "
            "fetched, so this map is not showing an absence of aircraft — it "
            "is not looking.")
    if loading or traffic is None:
        return TrackingState(TRACKING_LOADING, "Loading live traffic…", "")
    if not traffic.ok:
        error = traffic.fetch.error or "not fetched"
        if "zoom in" in error.lower():
            return TrackingState(
                TRACKING_ZOOMED_OUT, "Zoomed out too far to load traffic",
                "The visible area is wider than one request may cover. Zoom "
                "in; nothing has been fetched for this view.")
        return TrackingState(
            TRACKING_ERROR, "Live traffic unavailable", error)
    if not traffic.states:
        return TrackingState(
            TRACKING_NO_COVERAGE, "Nothing seen in this area",
            "The network returned no aircraft here. Volunteer receiver "
            "coverage is dense over Europe and North America and absent "
            "elsewhere — Baku returned zero aircraft on every network tried. "
            "This is not a report that the sky is empty.")
    if traffic.fetch.stale:
        return TrackingState(
            TRACKING_STALE, "Showing the last positions that arrived",
            "The live fetch failed and these came from the cache. Every "
            "marker is where an aircraft was, not where it is.")
    stale = [s for s in traffic.states if s.is_stale(now)]
    if len(stale) == len(traffic.states):
        return TrackingState(
            TRACKING_STALE, "Positions are stale",
            f"No contact newer than {int(STALE_AFTER_S)} s. Markers are where "
            f"these aircraft were last seen, not where they are.")
    return TrackingState(TRACKING_OK, "", "")


@dataclass(frozen=True)
class FleetPosition:
    """One tail, with its position or the reason there is not one."""

    tail: str
    icao24: str
    state: StateVector | None = None
    reason: str = ""

    @property
    def seen(self) -> bool:
        return self.state is not None and self.state.has_position


@dataclass(frozen=True)
class FleetSnapshot:
    """What the map draws, plus everything it must say about what is missing."""

    fetch: Fetch
    positions: tuple[FleetPosition, ...] = ()
    untracked: tuple[str, ...] = ()
    at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.fetch.ok

    @property
    def seen(self) -> tuple[FleetPosition, ...]:
        return tuple(p for p in self.positions if p.seen)

    @property
    def unseen(self) -> tuple[FleetPosition, ...]:
        return tuple(p for p in self.positions if not p.seen)

    def provenance(self) -> str:
        return self.fetch.provenance()

    def summary(self) -> str:
        """The count line under the map. Never says "on the ground"."""
        if not self.positions and not self.untracked:
            return "No aircraft in the fleet register."
        parts = [f"{len(self.seen)} of {len(self.positions)} tracked tails "
                 f"seen by the network"]
        if self.untracked:
            parts.append(f"{len(self.untracked)} tail"
                         f"{'s' if len(self.untracked) > 1 else ''} with no "
                         f"ICAO 24-bit address on file")
        return " · ".join(parts)


def fleet_positions(client: NetClient, con: sqlite3.Connection | None, *,
                    ttl: float = STATES_TTL) -> FleetSnapshot:
    """Current positions for every tail in the register that has an address.

    Returns a snapshot in every case, including offline: `fetch.error` says
    why there are no positions and the map renders that instead of blank.
    """
    mapping = tail_to_icao24(con)
    untracked = tuple(untracked_tails(con))
    if not mapping:
        return FleetSnapshot(
            fetch=Fetch(source=SOURCE,
                        error="no tail in the fleet register has an ICAO "
                              "24-bit address on file — add one in Admin"),
            untracked=untracked)

    url = states_url(list(mapping.values()))
    fetched = client.get_json(url, SOURCE, ttl=ttl)
    if not fetched.ok:
        return FleetSnapshot(
            fetch=fetched,
            positions=tuple(FleetPosition(tail, address,
                                          reason=fetched.error or "not fetched")
                            for tail, address in sorted(mapping.items())),
            untracked=untracked)

    payload = fetched.data if isinstance(fetched.data, dict) else {}
    rows = payload.get("states") or []
    by_address: dict[str, StateVector] = {}
    for row in rows:
        state = parse_state(row)
        if state is not None:
            by_address[state.icao24] = state

    positions = []
    for tail, address in sorted(mapping.items()):
        state = by_address.get(address)
        positions.append(FleetPosition(
            tail=tail, icao24=address, state=state,
            reason="" if state is not None and state.has_position
            else "not seen by the network in this fetch"))
    return FleetSnapshot(fetch=fetched, positions=tuple(positions),
                         untracked=untracked, at=_stamp(payload.get("time")))


# ── recorded arrivals and departures ────────────────────────────────────
# Renamed in Phase 8, and the rename is the point. These endpoints were being
# rendered beside live positions in a panel called "Movements", which reads as
# "what is happening at this airport now". They are not that: OpenSky's own
# documentation says the flights tables are updated by a batch process at
# night. Everything here now carries `recorded` in its name so that a caller
# cannot wire it into a live view without noticing what it is doing. The
# provider-independent domain that decides when to use this at all lives in
# `ops/movements.py`.


@dataclass(frozen=True)
class Flight:
    """One movement OpenSky attributes to an airport. Estimated, and labelled so."""

    icao24: str
    callsign: str = ""
    departure: str = ""
    arrival: str = ""
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    departure_candidates: int = 0
    arrival_candidates: int = 0

    def other_end(self, airport: str) -> str:
        """The airport at the far end of this movement, as far as it is known."""
        here = (airport or "").strip().upper()
        far = self.departure if self.arrival == here else self.arrival
        return far or "unknown"

    def time_text(self, arriving: bool) -> str:
        stamp = self.last_seen if arriving else self.first_seen
        return stamp.strftime("%H:%MZ") if stamp else "—"

    def uncertain(self, arriving: bool) -> bool:
        """True when OpenSky had more than one candidate for this end.

        The airport is *inferred* from where the track started or stopped.
        More than one candidate means the answer is a guess, and a guess
        rendered like a fact is the failure mode this flag exists to stop.
        """
        count = self.arrival_candidates if arriving else self.departure_candidates
        return count > 1


def _flights_url(kind: str, airport: str, begin: int, end: int) -> str:
    query = urlencode({"airport": airport.strip().upper(),
                       "begin": int(begin), "end": int(end)})
    return f"{BASE}/flights/{kind}?{query}"


def parse_flight(row: dict) -> Flight | None:
    if not isinstance(row, dict):
        return None
    icao24 = normalise_icao24(row.get("icao24"))
    if not icao24:
        return None
    return Flight(
        icao24=icao24,
        callsign=str(row.get("callsign") or "").strip(),
        departure=str(row.get("estDepartureAirport") or "").strip().upper(),
        arrival=str(row.get("estArrivalAirport") or "").strip().upper(),
        first_seen=_stamp(row.get("firstSeen")),
        last_seen=_stamp(row.get("lastSeen")),
        departure_candidates=int(row.get("departureAirportCandidatesCount") or 0),
        arrival_candidates=int(row.get("arrivalAirportCandidatesCount") or 0),
    )


@dataclass(frozen=True)
class RecordedMovements:
    """Recorded arrivals or departures for one airport, and the window covered.

    `label()` is not decoration: this object is the only thing a panel has to
    tell it what it is holding, and the one mistake worth engineering against
    is a screen presenting a nightly batch as the current picture.
    """

    fetch: Fetch
    airport: str = ""
    arriving: bool = True
    flights: tuple[Flight, ...] = ()
    since: datetime | None = None
    until: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.fetch.ok

    def label(self) -> str:
        return MOVEMENTS_LABEL

    def window_text(self) -> str:
        if self.since is None or self.until is None:
            return ""
        return (f"{self.since.strftime('%d %H:%MZ')} to "
                f"{self.until.strftime('%d %H:%MZ')}")

    def provenance(self) -> str:
        return self.fetch.provenance()


def fetch_recorded_movements(client: NetClient, airport: str, *,
                             arriving: bool,
                             window: timedelta = FLIGHTS_WINDOW,
                             now: datetime | None = None,
                             ttl: float = FLIGHTS_TTL) -> RecordedMovements:
    """Recorded arrivals or departures at `airport` over the last `window`.

    Recorded, not current: see `MOVEMENTS_WARNING`. The airport at each end is
    OpenSky's estimate from where a track started or stopped, not a filed
    flight plan, and `Flight.uncertain` says when even that estimate had
    competing candidates.
    """
    icao = (airport or "").strip().upper()
    if not icao:
        return RecordedMovements(
            fetch=Fetch(source=SOURCE, error="no airport selected"),
            arriving=arriving)
    now = now or datetime.now(timezone.utc)
    span = min(window, FLIGHTS_MAX_WINDOW)
    since = now - span
    url = _flights_url("arrival" if arriving else "departure", icao,
                       int(since.timestamp()), int(now.timestamp()))
    fetched = client.get_json(url, SOURCE, ttl=ttl)
    if not fetched.ok:
        return RecordedMovements(fetch=fetched, airport=icao,
                                 arriving=arriving, since=since, until=now)

    rows = fetched.data if isinstance(fetched.data, list) else []
    flights = [flight for flight in (parse_flight(row) for row in rows)
               if flight is not None]
    flights.sort(key=lambda f: (f.last_seen or f.first_seen or since),
                 reverse=True)
    if not flights:
        # OpenSky answers an airport with no recorded movements with an empty
        # list, which is a real answer and not a failure. Say which it is.
        return RecordedMovements(
            fetch=Fetch(source=fetched.source, url=fetched.url,
                        fetched_at=fetched.fetched_at,
                        from_cache=fetched.from_cache, stale=fetched.stale,
                        error=f"no {'arrivals' if arriving else 'departures'} "
                              f"recorded at {icao} in this window — this is a "
                              f"nightly batch, so today's movements may not "
                              f"be in it yet; the free tier's coverage is "
                              f"incomplete and its history lags by hours"),
            airport=icao, arriving=arriving, since=since, until=now)
    return RecordedMovements(fetch=fetched, airport=icao, arriving=arriving,
                             flights=tuple(flights), since=since, until=now)


def fetch_recorded_arrivals(client: NetClient, airport: str,
                            **kw) -> RecordedMovements:
    return fetch_recorded_movements(client, airport, arriving=True, **kw)


def fetch_recorded_departures(client: NetClient, airport: str,
                              **kw) -> RecordedMovements:
    return fetch_recorded_movements(client, airport, arriving=False, **kw)
