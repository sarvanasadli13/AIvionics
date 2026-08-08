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

# OpenSky serves flight history with a delay and rejects windows over 7 days.
FLIGHTS_WINDOW = timedelta(hours=12)
FLIGHTS_MAX_WINDOW = timedelta(days=7)

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
        return "speed not reported" if speed is None else f"{speed} kt ground speed"

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

    def age_text(self, now: datetime | None = None) -> str:
        if self.last_contact is None:
            return "last contact not reported"
        now = now or datetime.now(timezone.utc)
        seconds = max(0, int((now - self.last_contact).total_seconds()))
        if seconds < 90:
            return f"last seen {seconds} s ago"
        return f"last seen {seconds // 60} min ago"


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
    )


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


# ── arrivals and departures ─────────────────────────────────────────────

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
class Movements:
    """Arrivals or departures for one airport, with the window they cover."""

    fetch: Fetch
    airport: str = ""
    arriving: bool = True
    flights: tuple[Flight, ...] = ()
    since: datetime | None = None
    until: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.fetch.ok

    def window_text(self) -> str:
        if self.since is None or self.until is None:
            return ""
        return (f"{self.since.strftime('%d %H:%MZ')} to "
                f"{self.until.strftime('%d %H:%MZ')}")

    def provenance(self) -> str:
        return self.fetch.provenance()


def fetch_movements(client: NetClient, airport: str, *, arriving: bool,
                    window: timedelta = FLIGHTS_WINDOW,
                    now: datetime | None = None,
                    ttl: float = FLIGHTS_TTL) -> Movements:
    """Arrivals or departures at `airport` over the last `window`.

    The airport at each end is OpenSky's estimate from where a track started
    or stopped, not a filed flight plan. `Flight.uncertain` says when even
    that estimate had competing candidates.
    """
    icao = (airport or "").strip().upper()
    if not icao:
        return Movements(fetch=Fetch(source=SOURCE, error="no airport selected"),
                         arriving=arriving)
    now = now or datetime.now(timezone.utc)
    span = min(window, FLIGHTS_MAX_WINDOW)
    since = now - span
    url = _flights_url("arrival" if arriving else "departure", icao,
                       int(since.timestamp()), int(now.timestamp()))
    fetched = client.get_json(url, SOURCE, ttl=ttl)
    if not fetched.ok:
        return Movements(fetch=fetched, airport=icao, arriving=arriving,
                         since=since, until=now)

    rows = fetched.data if isinstance(fetched.data, list) else []
    flights = [flight for flight in (parse_flight(row) for row in rows)
               if flight is not None]
    flights.sort(key=lambda f: (f.last_seen or f.first_seen or since),
                 reverse=True)
    if not flights:
        # OpenSky answers an airport with no recorded movements with an empty
        # list, which is a real answer and not a failure. Say which it is.
        return Movements(
            fetch=Fetch(source=fetched.source, url=fetched.url,
                        fetched_at=fetched.fetched_at,
                        from_cache=fetched.from_cache, stale=fetched.stale,
                        error=f"no {'arrivals' if arriving else 'departures'} "
                              f"recorded at {icao} in this window — the free "
                              f"tier's coverage is incomplete and its history "
                              f"lags by several hours"),
            airport=icao, arriving=arriving, since=since, until=now)
    return Movements(fetch=fetched, airport=icao, arriving=arriving,
                     flights=tuple(flights), since=since, until=now)


def fetch_arrivals(client: NetClient, airport: str, **kw) -> Movements:
    return fetch_movements(client, airport, arriving=True, **kw)


def fetch_departures(client: NetClient, airport: str, **kw) -> Movements:
    return fetch_movements(client, airport, arriving=False, **kw)
