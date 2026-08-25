"""Airport movements, independent of who publishes them (Phase 8).

The arrivals and departures panel was wired straight to OpenSky, and that was
a category error rather than a bug: *what a movement is* and *who told us
about it* are different questions, and the second one decides how much the
first one is worth. This module separates them.

There are three levels a movement can come from, and they are not
interchangeable:

* **Operational** — a FIDS, an AODB, or the operator's own flight-ops system.
  This is the only level that is authoritative for a *scheduled* time, a
  gate, a delay code, a cancellation or a diversion, because those facts are
  decisions somebody made, not things anybody can observe from outside.
  **No such system exists to integrate with here**, so what this module
  provides is the interface plus an explicit `UnavailableProvider` that says
  so on screen. There is no adapter pretending to be an AODB, and there must
  never be one: a fabricated gate number is worse than a blank field.
* **Commercial flight information** — a paid vendor. Configurable on purpose:
  the domain model below names no vendor, and `CommercialProvider` takes the
  URL builders and the response adapter from its configuration. Two things
  keep a credential out of the clear, and both are enforced rather than
  documented: the secret lives in `Credential`, whose `repr` is redacted and
  which is never written to the settings table or the audit chain; and a URL
  that carries the secret in its query string is **refused before it is
  fetched**, because `net.DiskCache` writes the URL it fetched into the cache
  file in plaintext.
* **Public ADS-B** — takeoffs and landings *inferred* from tracks. This is
  real, it is buildable from the position feed the map already pulls, and it
  is the weakest of the three. Every movement it produces is labelled
  observed and inferred, carries the time it was **seen** rather than a time
  anybody scheduled, and never claims a gate. An observed landing means a
  transponder stopped reporting airborne near an airport. It does not mean
  the flight arrived, and it certainly does not mean it was on time.

A fourth thing exists and is not a level at all: OpenSky's `flights/arrival`
and `flights/departure` endpoints, which OpenSky's own documentation
describes as *updated by a batch process at night*. It is a **recorded**
history that lags by hours, it is not live, and `adsb.RecordedMovements`
carries that word in its name so that no caller can wire it into a board
labelled "now" by accident. Measured: EDDF — well over a thousand movements
a day — returned **one arrival** for a twelve-hour window, then HTTP 429.

Nothing in this module opens a socket. Every provider that fetches takes a
`NetClient`, which is the one object in the application permitted to.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol, runtime_checkable

from . import adsb, adsblol
from .net import Fetch, NetClient

# ── the vocabulary ──────────────────────────────────────────────────────
# Levels, in the order a caller should prefer them. The order is the whole
# point: an operational feed answers questions the ones below it cannot, and
# an inferred landing must never be chosen over a confirmed one.

LEVEL_OPERATIONAL = "operational"
LEVEL_COMMERCIAL = "commercial"
LEVEL_OBSERVED = "observed"
LEVEL_RECORDED = "recorded"

LEVEL_ORDER = (LEVEL_OPERATIONAL, LEVEL_COMMERCIAL, LEVEL_OBSERVED,
               LEVEL_RECORDED)

LEVEL_LABEL = {
    LEVEL_OPERATIONAL: "operational system (FIDS / AODB / flight ops)",
    LEVEL_COMMERCIAL: "commercial flight-information provider",
    LEVEL_OBSERVED: "public ADS-B, observed",
    LEVEL_RECORDED: "recorded history, nightly batch",
}

# What a movement is worth. `kind` says where it came from; `confidence` says
# how far it may be trusted. They are separate because a commercial provider
# can report a *scheduled* time it has not confirmed, and a recorded batch can
# be certain about something that happened six hours ago.
KIND_CONFIRMED = "provider-confirmed"
KIND_OBSERVED = "ads-b observed"
KIND_RECORDED = "historical batch"

CONFIDENCE_CONFIRMED = "confirmed"
CONFIDENCE_REPORTED = "reported"
CONFIDENCE_INFERRED = "inferred"

# Statuses a movement may carry. Deliberately small: a status this
# application cannot substantiate is not in the list, so nothing can render
# "ON TIME" from a position report.
STATUS_SCHEDULED = "scheduled"
STATUS_ESTIMATED = "estimated"
STATUS_ACTIVE = "active"
STATUS_LANDED = "landed"
STATUS_DEPARTED = "departed"
STATUS_CANCELLED = "cancelled"
STATUS_DIVERTED = "diverted"
STATUS_OBSERVED_LANDING = "observed landing"
STATUS_OBSERVED_TAKEOFF = "observed takeoff"
STATUS_UNKNOWN = "unknown"

# What a board *is* when it is not a list of movements. A panel needs these
# as four separate answers rather than one empty list, because "nobody has
# connected a FIDS", "the vendor timed out", "the source answered and had
# nothing" and "this is what the cache held an hour ago" call for four
# different reactions and only one of them is worth retrying.
BOARD_OK = "ok"
BOARD_UNAVAILABLE = "unavailable"
BOARD_NO_COVERAGE = "no-coverage"
BOARD_STALE = "stale"
BOARD_ERROR = "error"

# Said wherever an inferred movement is rendered. It is not a disclaimer to
# be tucked into a tooltip: the difference between "landed" and "stopped
# reporting airborne near an airport" is the entire honesty of this feature.
OBSERVED_NOTE = (
    "Observed from public ADS-B tracks, not from any airport or airline "
    "system. A landing here means a transponder stopped reporting airborne "
    "near this airport, and a takeoff that one started; neither is a "
    "confirmed movement, neither carries a gate, and neither says anything "
    "about whether a flight was on time, diverted or cancelled.")

OPERATIONAL_ABSENT = (
    "No operational movement system is connected. Scheduled and estimated "
    "times, gates, delay codes, cancellations and diversions come from a "
    "FIDS, an AODB or the operator's flight-ops system, and this "
    "installation is not connected to one. The interface exists and an "
    "adapter can be written against it; nothing below is a substitute for "
    "it, and nothing here invents those fields.")

COMMERCIAL_ABSENT = (
    "No commercial flight-information provider is configured. This is a "
    "paid, per-vendor integration: it needs a subscription, a URL builder "
    "and a response adapter for that vendor, an outbound allow-list row in "
    "ops/net.py, and a credential supplied at runtime.")

# What each feed is owed when its data is rendered. adsb.lol publishes under
# ODbL 1.0, which wants the credit *wherever the data appears* — and an
# inferred landing is that data, one derivation removed. The credit therefore
# has to follow the row onto the arrivals board, not stop at the map that
# drew the position it came from. Keyed by feed so a board that mixes two of
# them credits both and neither one twice.
FEED_ATTRIBUTION = {
    adsblol.SOURCE: adsblol.ATTRIBUTION,
}


def _utc(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


# ── credentials ─────────────────────────────────────────────────────────

class Credential:
    """A secret held in memory for the life of the process, and nowhere else.

    Three rules, each of which is enforced here rather than trusted to the
    caller, because every one of them has a plausible way of being broken by
    an ordinary-looking change:

    * **Never rendered.** `repr` and `str` return a redaction. A dataclass
      with a plain `str` field would print the key the first time anything
      logged the configuration object, and the LLM config in `llm/client.py`
      carries the same note for the same reason.
    * **Never persisted.** Nothing here writes to the settings table, the
      audit chain or a file. `from_environment` reads a variable; it does not
      copy it anywhere.
    * **Never in a URL.** `appears_in` lets the caller refuse a URL that
      carries the secret as a query parameter, which matters because
      `net.DiskCache.put` writes the fetched URL into the cache file as
      plaintext JSON and `net.Fetch.url` is rendered in provenance lines.
    """

    __slots__ = ("_value", "origin")

    def __init__(self, value: str = "", origin: str = "") -> None:
        self._value = str(value or "")
        # Where it came from, which is safe to show and is the only thing
        # about a credential an operator actually needs on screen.
        self.origin = origin or ""

    @classmethod
    def from_environment(cls, variable: str) -> "Credential":
        """Read a secret from the environment. Not stored, not echoed."""
        return cls(os.environ.get(variable, ""), origin=variable)

    @property
    def present(self) -> bool:
        return bool(self._value)

    def appears_in(self, text: str) -> bool:
        """True when the secret is embedded in `text` — a URL, usually."""
        return bool(self._value) and self._value in str(text or "")

    def header(self) -> dict[str, str]:
        """The Authorization header a vendor would want.

        Unused today and deliberately kept: `net.NetClient.get` takes a URL
        and nothing else, so there is no way to send this yet. That is the
        real blocker on wiring a commercial vendor, and it is named here
        rather than worked around with a query-string key.
        """
        return {"Authorization": f"Bearer {self._value}"} if self._value else {}

    def describe(self) -> str:
        if not self._value:
            return ("not configured"
                    + (f" — set {self.origin}" if self.origin else ""))
        return ("configured"
                + (f" (from {self.origin})" if self.origin else "")
                + " — the value is never shown, logged or written to disk")

    def __repr__(self) -> str:            # pragma: no cover - trivial
        return f"Credential(origin={self.origin!r}, present={self.present})"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return self.present


# ── the movement ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Movement:
    """One arrival or departure, with its provenance attached to it.

    Scheduled, estimated and actual are **three separate fields** and are
    never collapsed into one "time" column. Collapsing them is how a screen
    ends up showing a scheduled time beside the word "landed": the reader
    takes the pair as an on-time report, and nothing in the data said that.
    `time_basis` names which of the three the rendered time actually is.
    """

    airport: str = ""                    # the airport this is attributed to
    arriving: bool = True
    flight_number: str = ""
    callsign: str = ""
    registration: str = ""
    aircraft_type: str = ""
    origin: str = ""
    destination: str = ""

    scheduled_at: datetime | None = None
    estimated_at: datetime | None = None
    actual_at: datetime | None = None

    terminal: str = ""
    gate: str = ""
    status: str = STATUS_UNKNOWN

    source: str = ""                     # the feed, named as a human reads it
    level: str = LEVEL_OBSERVED
    kind: str = KIND_OBSERVED
    confidence: str = CONFIDENCE_INFERRED
    observed_at: datetime | None = None  # when this application saw it
    last_updated: datetime | None = None  # when the source last changed it
    note: str = ""

    @property
    def identity(self) -> str:
        """What to call this movement on screen, best available first."""
        return (self.flight_number or self.callsign.strip()
                or self.registration or "unidentified")

    @property
    def other_end(self) -> str:
        """The far end of the movement, from this airport's point of view."""
        far = self.origin if self.arriving else self.destination
        return far or "unknown"

    @property
    def best_time(self) -> datetime | None:
        return self.actual_at or self.estimated_at or self.scheduled_at

    def time_basis(self) -> str:
        if self.actual_at is not None:
            return "actual"
        if self.estimated_at is not None:
            return "estimated"
        if self.scheduled_at is not None:
            return "scheduled"
        return "no time reported"

    def time_text(self) -> str:
        """The time, always with the word that says which time it is."""
        stamp = self.best_time
        if stamp is None:
            return "no time reported"
        return f"{stamp.strftime('%H:%MZ')} {self.time_basis()}"

    def delay(self) -> timedelta | None:
        """Late by how much — only when there is a schedule to be late against.

        A movement with no scheduled time cannot be delayed, and returning
        zero here would let a panel print "on time" for an ADS-B observation
        that never had a schedule in the first place.
        """
        if self.scheduled_at is None:
            return None
        against = self.actual_at or self.estimated_at
        return None if against is None else against - self.scheduled_at

    def delay_text(self) -> str:
        delay = self.delay()
        if delay is None:
            return "no scheduled time to compare against"
        minutes = int(round(delay.total_seconds() / 60.0))
        if abs(minutes) < 1:
            return "on the scheduled time"
        return f"{abs(minutes)} min {'late' if minutes > 0 else 'early'}"

    def place_text(self) -> str:
        """Terminal and gate, or the reason there is not one."""
        parts = [p for p in (self.terminal and f"Terminal {self.terminal}",
                             self.gate and f"Gate {self.gate}") if p]
        if parts:
            return " · ".join(parts)
        return ("no gate — this level of data does not carry one"
                if self.level in (LEVEL_OBSERVED, LEVEL_RECORDED)
                else "not reported")

    def provenance(self) -> str:
        """One line naming the source, the kind and the confidence."""
        seen = (f" · seen {self.observed_at.strftime('%H:%MZ')}"
                if self.observed_at else "")
        return f"{self.source or LEVEL_LABEL.get(self.level, self.level)} · " \
               f"{self.kind} · {self.confidence}{seen}"


@dataclass(frozen=True)
class Availability:
    """Whether one provider can answer at all, and why not when it cannot."""

    level: str
    name: str
    available: bool = False
    reason: str = ""

    def line(self) -> str:
        label = LEVEL_LABEL.get(self.level, self.level)
        return (f"{label}: {self.name} — available" if self.available
                else f"{label}: {self.reason or 'unavailable'}")


@dataclass(frozen=True)
class MovementBoard:
    """Arrivals or departures for one airport, at one level, with its caveats.

    Returned in every case, failures included, for the same reason `Fetch` is:
    a caller that has to handle an exception to render "unavailable" will
    sooner or later render a blank panel instead.
    """

    airport: str = ""
    arriving: bool = True
    level: str = LEVEL_OBSERVED
    provider: str = ""
    movements: tuple[Movement, ...] = ()
    fetch: Fetch | None = None
    since: datetime | None = None
    until: datetime | None = None
    error: str = ""
    notes: tuple[str, ...] = ()
    # Which of the four non-answers this is, *declared by whoever built the
    # board* rather than guessed from the error text downstream. Sniffing a
    # message for the word "no" is how a rate-limit error ends up rendered as
    # "this airport had no arrivals".
    state: str = BOARD_OK

    @property
    def ok(self) -> bool:
        return not self.error

    def headline(self) -> str:
        """The line under the panel title. Names the level, never "live"."""
        label = LEVEL_LABEL.get(self.level, self.level)
        direction = "Arrivals" if self.arriving else "Departures"
        if not self.ok:
            return f"{direction} unavailable — {self.error}"
        count = len(self.movements)
        return (f"{direction}: {count} from {self.provider or label} "
                f"({label})")

    def provenance(self) -> str:
        if self.fetch is not None:
            return self.fetch.provenance()
        return f"Source: {self.provider or LEVEL_LABEL.get(self.level, self.level)}"


@dataclass(frozen=True)
class BoardState:
    """Why a board looks the way it does, in words the panel itself renders.

    The twin of `adsb.TrackingState`, and here for the same reason: an empty
    arrivals list with nothing beside it reads as "this airport is quiet",
    which is a claim this application is never in a position to make.
    """

    state: str = BOARD_OK
    headline: str = ""
    detail: str = ""

    @property
    def is_ok(self) -> bool:
        return self.state == BOARD_OK

    @property
    def blank_is_explained(self) -> bool:
        """True when this state gives the reader a reason for an empty panel."""
        return bool(self.headline) and self.state != BOARD_OK

    def line(self) -> str:
        return " — ".join(part for part in (self.headline, self.detail) if part)


def board_state(board: MovementBoard) -> BoardState:
    """Classify one board. Pure, so the wording can be tested.

    Ordered by what the reader most needs first: a level nobody has connected
    explains every symptom under it, and a list served from the cache
    explains why the times on screen have stopped moving — both before the
    question of what is actually in the list.
    """
    level = LEVEL_LABEL.get(board.level, board.level)
    if board.state == BOARD_UNAVAILABLE:
        return BoardState(BOARD_UNAVAILABLE, f"No {level} is connected",
                          board.error)

    cached = ""
    if board.fetch is not None and board.fetch.stale:
        stamp = (board.fetch.fetched_at.strftime("%d %H:%MZ")
                 if board.fetch.fetched_at else "an earlier fetch")
        cached = (f"The live fetch failed and this came from the cache, "
                  f"written {stamp}; nothing in it has been confirmed since.")

    if board.state == BOARD_NO_COVERAGE:
        return BoardState(BOARD_NO_COVERAGE,
                          "Nothing recorded for this airport",
                          _sentences(board.error, cached))
    if not board.ok:
        # The provider by name where there is one: "the recorded history,
        # nightly batch could not answer" is the level label read aloud, and
        # what the reader needs is which system failed.
        return BoardState(BOARD_ERROR,
                          f"{board.provider or level} could not answer",
                          _sentences(board.error, cached))
    if not board.movements:
        # `ok` and empty. Whoever built this did not say which it was, so say
        # the careful thing rather than let the panel render a blank.
        return BoardState(
            BOARD_NO_COVERAGE, "Nothing recorded for this airport",
            _sentences("The source answered and had no movement to report. "
                       "That is what it saw, not what happened.", cached))
    if cached:
        return BoardState(BOARD_STALE, "Showing a cached list", cached)
    return BoardState(BOARD_OK, "", "")


def _sentences(*parts: str) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip())


# ── the provider interface ──────────────────────────────────────────────

@runtime_checkable
class MovementProvider(Protocol):
    """What every movement source must offer, at any of the three levels."""

    name: str
    level: str

    def availability(self) -> Availability: ...

    def arrivals(self, airport: str) -> MovementBoard: ...

    def departures(self, airport: str) -> MovementBoard: ...


@dataclass
class UnavailableProvider:
    """A level that is defined but not connected, saying so out loud.

    This is the honest answer for the operational level, and the default for
    the commercial one. It exists so the interface can be complete without
    anything being faked: the screen renders a provider that reports why it
    cannot answer, which is a state, not an error and not a blank.
    """

    level: str
    name: str
    reason: str

    def availability(self) -> Availability:
        return Availability(level=self.level, name=self.name, available=False,
                            reason=self.reason)

    def arrivals(self, airport: str) -> MovementBoard:
        return self._board(airport, True)

    def departures(self, airport: str) -> MovementBoard:
        return self._board(airport, False)

    def _board(self, airport: str, arriving: bool) -> MovementBoard:
        return MovementBoard(airport=(airport or "").strip().upper(),
                             arriving=arriving, level=self.level,
                             provider=self.name, error=self.reason,
                             state=BOARD_UNAVAILABLE)


def operational_provider() -> UnavailableProvider:
    """The operational level, which nothing here is connected to.

    Kept as a function rather than a constant so a real adapter can replace
    it at one call site the day an operator has a system to point it at.
    """
    return UnavailableProvider(LEVEL_OPERATIONAL, "no operational system",
                               OPERATIONAL_ABSENT)


# ── commercial: configurable, vendor-neutral, credential-safe ───────────

@dataclass(frozen=True)
class CommercialConfig:
    """Everything a vendor integration needs, and nothing vendor-specific.

    The URL builders and the adapter are supplied rather than written here on
    purpose: schedule vendors disagree about almost everything — field names,
    time formats, whether a cancellation is a status or a flag — and baking
    one of them into the domain model is how the model stops being a domain
    model.
    """

    vendor: str = ""
    arrivals_url: Callable[[str], str] | None = None
    departures_url: Callable[[str], str] | None = None
    # (payload, airport, arriving) -> movements. Vendor-specific, injected.
    adapter: Callable[[Any, str, bool], tuple[Movement, ...]] | None = None
    credential: Credential = field(default_factory=Credential)
    ttl: float = 120.0

    @property
    def configured(self) -> bool:
        return bool(self.vendor and self.arrivals_url
                    and self.departures_url and self.adapter)


@dataclass
class CommercialProvider:
    """A paid flight-information vendor, whichever one an operator buys.

    Unconfigured by default, and unavailable rather than silent when it is.
    """

    client: NetClient
    config: CommercialConfig = field(default_factory=CommercialConfig)
    level: str = LEVEL_COMMERCIAL

    @property
    def name(self) -> str:
        return self.config.vendor or "no commercial provider"

    def availability(self) -> Availability:
        reason = self._unavailable_reason()
        return Availability(level=self.level, name=self.name,
                            available=not reason, reason=reason)

    def _unavailable_reason(self) -> str:
        if not self.config.configured:
            return COMMERCIAL_ABSENT
        if not self.config.credential.present:
            return (f"{self.config.vendor} is configured but has no "
                    f"credential — {self.config.credential.describe()}")
        return ""

    def arrivals(self, airport: str) -> MovementBoard:
        return self._board(airport, True)

    def departures(self, airport: str) -> MovementBoard:
        return self._board(airport, False)

    def _board(self, airport: str, arriving: bool) -> MovementBoard:
        icao = (airport or "").strip().upper()
        reason = self._unavailable_reason()
        if reason or not icao:
            return MovementBoard(
                airport=icao, arriving=arriving, level=self.level,
                provider=self.name,
                error=reason or "no airport selected",
                state=BOARD_UNAVAILABLE)

        build = (self.config.arrivals_url if arriving
                 else self.config.departures_url)
        url = build(icao)
        if self.config.credential.appears_in(url):
            # Refused before the fetch, not after: `net.DiskCache.put` writes
            # the URL it fetched into a plaintext JSON file under data/, and
            # `Fetch.url` is rendered in provenance lines. A vendor that
            # authenticates by query parameter cannot be used through this
            # client without leaking the key to disk, and saying so is the
            # correct outcome.
            return MovementBoard(
                airport=icao, arriving=arriving, level=self.level,
                provider=self.name,
                error="refused: the request URL carries the credential in "
                      "plaintext, and fetched URLs are written to the disk "
                      "cache — this vendor needs header authentication",
                state=BOARD_UNAVAILABLE)

        fetched = self.client.get_json(url, self.name, ttl=self.config.ttl)
        if not fetched.ok:
            return MovementBoard(airport=icao, arriving=arriving,
                                 level=self.level, provider=self.name,
                                 fetch=fetched, error=fetched.error,
                                 state=BOARD_ERROR)
        movements = tuple(self.config.adapter(fetched.data, icao, arriving))
        return MovementBoard(airport=icao, arriving=arriving, level=self.level,
                             provider=self.name, movements=movements,
                             fetch=fetched)


def commercial_provider(client: NetClient,
                        config: CommercialConfig | None = None
                        ) -> CommercialProvider:
    return CommercialProvider(client=client,
                              config=config or CommercialConfig())


# ── observed: takeoffs and landings inferred from ADS-B ─────────────────

EARTH_RADIUS_NM = 3440.065
# How close a transponder has to be to an airport before a change of ground
# state is attributed to it. Twelve miles is generous for a landing rollout
# and tight enough that a cruise-altitude dropout over a city does not get
# blamed on the nearest field. It is a heuristic and it is labelled as one.
AIRPORT_RADIUS_NM = 12.0
# An aircraft above this cannot be landing at anything below it, whatever its
# on-ground flag says. ADS-B ground bits do flicker.
MAX_GROUND_ALTITUDE_FT = 1500
# How many movements to keep. This is a session view, not a log.
MAX_OBSERVED = 200
# And how many aircraft to remember the previous state of, so that a busy
# European sector cannot grow the dictionary without bound.
MAX_TRACKED = 4000


def distance_nm(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    """Great-circle distance. Haversine, because the legs here are short."""
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    d_phi = math.radians(lat_b - lat_a)
    d_lam = math.radians(lon_b - lon_a)
    h = (math.sin(d_phi / 2) ** 2
         + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lam / 2) ** 2)
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(h)))


def _default_locate(latitude: float, longitude: float) -> str:
    """Nearest airport with a paved identity, or "" — built lazily.

    Imported and searched here rather than at module import: the airport
    index costs ~1.4 s to build and this module must stay usable, and
    testable, without paying for it.
    """
    try:
        from . import airports as apt
        index = apt.index()
        index.load()
        best, best_distance = "", AIRPORT_RADIUS_NM
        for airport in index._airports:
            if airport.type not in ("large_airport", "medium_airport"):
                continue
            gap = distance_nm(latitude, longitude,
                              airport.latitude, airport.longitude)
            if gap < best_distance:
                best, best_distance = (airport.icao or airport.ident), gap
        return best
    except Exception:                               # pragma: no cover
        return ""


@dataclass
class _Seen:
    """The last thing this watcher knew about one transponder."""

    on_ground: bool
    altitude_ft: int | None
    latitude: float
    longitude: float
    at: datetime


class ObservedProvider:
    """Takeoffs and landings inferred from the position feed the map pulls.

    Push-based rather than fetching: the Ops page already asks adsb.lol for
    everything in the visible box on a timer, and running a second query for
    the same aircraft would spend a request to learn nothing new. Hand the
    states in with `observe`, read the movements back out per airport.

    What it can and cannot see follows from that, and both are said on
    screen: it only sees aircraft inside a box somebody was looking at, only
    while the application is running, and only where volunteer receivers
    cover the ground. Baku returned zero aircraft on every network tried.
    """

    name = "public ADS-B (observed)"
    level = LEVEL_OBSERVED

    def __init__(self, source: str = adsb.SOURCE,
                 locate: Callable[[float, float], str] | None = None) -> None:
        self.source = source
        self.locate = locate or _default_locate
        self._last: dict[str, _Seen] = {}
        self._movements: list[Movement] = []

    def availability(self) -> Availability:
        if not self._movements:
            return Availability(
                level=self.level, name=self.name, available=False,
                reason="no movement has been observed yet — this level only "
                       "sees aircraft while the map is open on them, and only "
                       "where the network has receivers")
        return Availability(level=self.level, name=self.name, available=True)

    def observe(self, states, now: datetime | None = None) -> tuple[Movement, ...]:
        """Fold one fetch of positions in. Returns what changed, if anything."""
        now = _utc(now)
        found: list[Movement] = []
        for state in states or ():
            if not getattr(state, "has_position", False):
                continue
            movement = self._step(state, now)
            if movement is not None:
                found.append(movement)
        if found:
            self._movements.extend(found)
            del self._movements[:-MAX_OBSERVED]
        if len(self._last) > MAX_TRACKED:
            # Drop the oldest half rather than clearing: a clear would make
            # every aircraft look newly seen and re-fire its next transition.
            for key in sorted(self._last,
                              key=lambda k: self._last[k].at)[:MAX_TRACKED // 2]:
                self._last.pop(key, None)
        return tuple(found)

    def _step(self, state, now: datetime) -> Movement | None:
        key = state.icao24 or state.registration
        if not key:
            return None
        altitude = state.altitude_ft
        current = _Seen(on_ground=bool(state.on_ground), altitude_ft=altitude,
                        latitude=float(state.latitude),
                        longitude=float(state.longitude),
                        at=state.last_contact or now)
        previous, self._last[key] = self._last.get(key), current
        if previous is None or previous.on_ground == current.on_ground:
            return None
        # A ground bit that flickers at altitude is a bad report, not a
        # landing. Refuse to attribute anything above circuit height.
        settled = current.on_ground
        check = current.altitude_ft if settled else previous.altitude_ft
        if check is not None and check > MAX_GROUND_ALTITUDE_FT:
            return None
        airport = self.locate(current.latitude, current.longitude)
        if not airport:
            return None
        arriving = settled
        return Movement(
            airport=airport, arriving=arriving,
            callsign=state.callsign.strip(),
            registration=state.registration,
            aircraft_type=state.aircraft_type,
            origin=airport if not arriving else "",
            destination=airport if arriving else "",
            actual_at=current.at,
            status=(STATUS_OBSERVED_LANDING if arriving
                    else STATUS_OBSERVED_TAKEOFF),
            # The vector names its own feed, and that is the one to believe:
            # two feeds populate `StateVector` and the caller hands states in
            # rather than fetching them, so a provider-level default is a
            # guess about somebody else's fetch. It guessed wrong — the Ops
            # screen folds in adsb.lol positions and every row came out
            # stamped OpenSky, crediting a network that supplied none of it.
            source=(getattr(state, "source", "") or self.source),
            level=self.level, kind=KIND_OBSERVED,
            confidence=CONFIDENCE_INFERRED,
            observed_at=now, last_updated=now, note=OBSERVED_NOTE)

    def board(self, airport: str, arriving: bool) -> MovementBoard:
        icao = (airport or "").strip().upper()
        rows = tuple(sorted(
            (m for m in self._movements
             if m.airport == icao and m.arriving == arriving),
            key=lambda m: m.actual_at or m.observed_at or datetime.min.replace(
                tzinfo=timezone.utc), reverse=True))
        if not rows:
            return MovementBoard(
                airport=icao, arriving=arriving, level=self.level,
                provider=self.name, notes=(OBSERVED_NOTE,),
                error=f"no {'arrival' if arriving else 'departure'} has been "
                      f"observed at {icao or 'this airport'} while this "
                      f"session has been running — an empty list here means "
                      f"nothing was seen, not that nothing happened",
                state=BOARD_NO_COVERAGE)
        # Credit whichever feeds actually produced these rows, once each.
        # Read off the rows rather than off the provider: what is owed
        # depends on whose data is on screen, and only the rows know that.
        credits = tuple(dict.fromkeys(
            FEED_ATTRIBUTION[m.source] for m in rows
            if m.source in FEED_ATTRIBUTION))
        return MovementBoard(airport=icao, arriving=arriving, level=self.level,
                             provider=self.name, movements=rows,
                             notes=(OBSERVED_NOTE,) + credits)

    def arrivals(self, airport: str) -> MovementBoard:
        return self.board(airport, True)

    def departures(self, airport: str) -> MovementBoard:
        return self.board(airport, False)


# ── recorded: OpenSky's nightly batch, named for what it is ─────────────

class RecordedProvider:
    """OpenSky's `flights/*` endpoints, wrapped and labelled as history.

    Kept because a six-hour-old list of real movements is worth something
    that nothing else here provides. Presented as history because that is
    what it is: OpenSky's own documentation says the flights tables are
    updated by a batch process at night.
    """

    name = adsb.SOURCE
    level = LEVEL_RECORDED

    def __init__(self, client: NetClient) -> None:
        self.client = client

    def availability(self) -> Availability:
        # Nothing to probe without spending a request, and a request that
        # exists only to answer "are you there" is a credit spent on nothing.
        # Availability here means "this is wired up", never "this is fresh".
        return Availability(level=self.level, name=self.name, available=True,
                            reason="")

    def arrivals(self, airport: str) -> MovementBoard:
        return self._board(adsb.fetch_recorded_arrivals(self.client, airport),
                           True)

    def departures(self, airport: str) -> MovementBoard:
        return self._board(
            adsb.fetch_recorded_departures(self.client, airport), False)

    def _board(self, recorded: "adsb.RecordedMovements",
               arriving: bool) -> MovementBoard:
        notes = (adsb.MOVEMENTS_WARNING,)
        if not recorded.ok:
            # "OpenSky answered with an empty list" and "OpenSky refused,
            # timed out or was rate limited" arrive here as the same shape,
            # and they are not the same news. `fetched_at` tells them apart
            # without reading the message: `net.NetClient.get` sets it only
            # when a body actually came back, from the network or the cache,
            # and leaves it None on every failure path.
            answered = recorded.fetch.fetched_at is not None
            return MovementBoard(airport=recorded.airport, arriving=arriving,
                                 level=self.level, provider=self.name,
                                 fetch=recorded.fetch, since=recorded.since,
                                 until=recorded.until, notes=notes,
                                 error=recorded.fetch.error,
                                 state=(BOARD_NO_COVERAGE if answered
                                        else BOARD_ERROR))
        return MovementBoard(
            airport=recorded.airport, arriving=arriving, level=self.level,
            provider=self.name, fetch=recorded.fetch, since=recorded.since,
            until=recorded.until, notes=notes,
            movements=tuple(from_recorded_flight(flight, recorded.airport,
                                                 arriving)
                            for flight in recorded.flights))


def from_recorded_flight(flight: "adsb.Flight", airport: str,
                         arriving: bool) -> Movement:
    """One batch row as a `Movement`, with every uncertainty carried over.

    `first_seen`/`last_seen` are when a *track* started and stopped, so they
    become `actual_at` and never `scheduled_at`: OpenSky has no schedule and
    inventing one from a track would be the exact error this module exists to
    prevent. The airport at each end is OpenSky's own estimate, and a row
    with competing candidates says so in `note`.
    """
    stamp = flight.last_seen if arriving else flight.first_seen
    uncertain = flight.uncertain(arriving)
    return Movement(
        airport=(airport or "").strip().upper(), arriving=arriving,
        callsign=flight.callsign, origin=flight.departure,
        destination=flight.arrival, actual_at=stamp,
        status=STATUS_LANDED if arriving else STATUS_DEPARTED,
        source=adsb.SOURCE, level=LEVEL_RECORDED, kind=KIND_RECORDED,
        confidence=CONFIDENCE_REPORTED, last_updated=stamp,
        note=("the airport at this end is inferred from where the track "
              "started or stopped, and OpenSky had several candidates"
              if uncertain else
              "the airport at this end is inferred from where the track "
              "started or stopped"))


# ── choosing between them ───────────────────────────────────────────────

@dataclass(frozen=True)
class Selection:
    """Which level answered, and what every other level said about itself.

    Both halves are rendered. A panel that shows only the winner leaves the
    reader unable to tell "the operator has no FIDS" from "the FIDS is down",
    and those call for different actions.
    """

    provider: Any = None
    availability: tuple[Availability, ...] = ()

    @property
    def level(self) -> str:
        return getattr(self.provider, "level", "")

    @property
    def ok(self) -> bool:
        return self.provider is not None

    def reasons(self) -> tuple[str, ...]:
        return tuple(row.line() for row in self.availability)

    def summary(self) -> str:
        if not self.ok:
            return ("No movement source is available at any level. "
                    + OPERATIONAL_ABSENT)
        above = "; ".join(
            row.line() for row in self.availability
            if row.level in LEVEL_ORDER
            and LEVEL_ORDER.index(row.level) < LEVEL_ORDER.index(self.level))
        return (f"Answered by the {LEVEL_LABEL.get(self.level, self.level)}. "
                f"Higher levels: {above or 'none above it'}")


def select(providers) -> Selection:
    """The best available level, and the report on all of them.

    Strictly in `LEVEL_ORDER`. Never "whichever answered first": a recorded
    batch will always answer faster than a system nobody has connected, and
    letting speed decide would quietly demote a real schedule.
    """
    rows = []
    for provider in sorted(providers or (),
                           key=lambda p: LEVEL_ORDER.index(p.level)
                           if p.level in LEVEL_ORDER else len(LEVEL_ORDER)):
        rows.append((provider, provider.availability()))
    chosen = next((p for p, a in rows if a.available), None)
    return Selection(provider=chosen,
                     availability=tuple(a for _p, a in rows))


def default_providers(client: NetClient,
                      observed: ObservedProvider | None = None,
                      commercial: CommercialConfig | None = None) -> tuple:
    """The four levels this installation actually has, in order."""
    return (operational_provider(),
            commercial_provider(client, commercial),
            observed or ObservedProvider(),
            RecordedProvider(client))


__all__ = [
    "AIRPORT_RADIUS_NM", "Availability", "BOARD_ERROR", "BOARD_NO_COVERAGE",
    "BOARD_OK", "BOARD_STALE", "BOARD_UNAVAILABLE", "BoardState",
    "COMMERCIAL_ABSENT", "board_state",
    "CONFIDENCE_CONFIRMED", "CONFIDENCE_INFERRED", "CONFIDENCE_REPORTED",
    "CommercialConfig", "CommercialProvider", "Credential", "KIND_CONFIRMED",
    "KIND_OBSERVED", "KIND_RECORDED", "LEVEL_COMMERCIAL", "LEVEL_LABEL",
    "LEVEL_OBSERVED", "LEVEL_OPERATIONAL", "LEVEL_ORDER", "LEVEL_RECORDED",
    "Movement", "MovementBoard", "MovementProvider", "OBSERVED_NOTE",
    "OPERATIONAL_ABSENT", "ObservedProvider", "RecordedProvider", "Selection",
    "STATUS_ACTIVE", "STATUS_CANCELLED", "STATUS_DEPARTED", "STATUS_DIVERTED",
    "STATUS_ESTIMATED", "STATUS_LANDED", "STATUS_OBSERVED_LANDING",
    "STATUS_OBSERVED_TAKEOFF", "STATUS_SCHEDULED", "STATUS_UNKNOWN",
    "UnavailableProvider", "commercial_provider", "default_providers",
    "distance_nm", "from_recorded_flight", "operational_provider", "select",
]
