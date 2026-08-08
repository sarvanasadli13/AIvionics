"""Compliance register — clocks, triage state and provenance (PLAN 4B.1).

**Standing rule 2 is the whole reason this module exists in this shape.** The
CAMO is the legal record. What is held here is an *imported mirror*, and a
mirror that has drifted is more dangerous than no mirror at all, because it
looks authoritative. Three mechanisms, all structural rather than editorial:

1. `Provenance` refuses to be constructed without a source system and an
   import timestamp, and `ComplianceRow` refuses to be constructed without a
   `Provenance`. There is no code path that produces a renderable row with no
   provenance to render beside it.
2. `Freshness` compares each source's last import against its own configured
   window. Past the window the source is **stale**, and `ModuleState.degraded`
   goes true for the whole register — not just for that source's rows —
   because an engineer reading one amber badge cannot be expected to know
   which feed it came from.
3. Nothing here decides anything. A due state is arithmetic over limits that
   were imported; when the inputs are missing the answer is `unknown`, never
   `ok`. Standing rule 8's habit of failing closed applies to clocks too.

**Whichever falls first.** A maintenance interval is not a date. It is up to
three limits — calendar, flight hours, flight cycles — and the aircraft
reaches whichever comes first. A date-only scheduler silently under-reports
on a high-utilisation tail, which is exactly the tail that matters. So all
three are tracked per item, each against its own warning window, and the row
takes the worst state of the three with the driving limit named in the UI.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

# ── triage states ───────────────────────────────────────────────────────
# Ordered worst-first: this tuple is the sort order of the whole register.

BREACHED, WARNING, OK, UNKNOWN = "breached", "warning", "ok", "unknown"
STATE_RANK = {BREACHED: 0, WARNING: 1, UNKNOWN: 2, OK: 3}

# Status kinds from ui.theme.STATUS, so colour+icon+word stay defined once.
STATE_BADGE = {BREACHED: "alert", WARNING: "warn", OK: "ok", UNKNOWN: "unknown"}
STATE_WORD = {BREACHED: "BREACHED", WARNING: "DUE SOON", OK: "IN LIMITS",
              UNKNOWN: "NOT EVALUABLE"}

KINDS = ("checkup", "mel", "adsb")
KIND_LABEL = {"checkup": "Checkup", "mel": "MEL", "adsb": "AD/SB"}

# ── warning windows ─────────────────────────────────────────────────────
# Defaults, not policy. A department sets its own; these are the values the
# register uses until it is told otherwise.

WARN_DAYS = 14
WARN_HOURS = 50.0
WARN_CYCLES = 50

# MEL rectification intervals. Category A has no standard interval — it is
# whatever the MEL's own remarks column says — so an A item without an
# explicit due date cannot be clocked and says so.
MEL_INTERVAL_DAYS: dict[str, int] = {"B": 3, "C": 10, "D": 120}
MEL_WARN_DAYS: dict[str, int] = {"A": 1, "B": 1, "C": 2, "D": 10}
MEL_CATEGORIES = ("A", "B", "C", "D")

# How old an import may be before its source is stale and the module degrades.
DEFAULT_FRESHNESS_HOURS = 24.0

DEGRADED_BANNER = ("data imported {when} — verify against the CAMO. "
                   "This register is a mirror of an export that is now older "
                   "than its freshness window; alerts are shown without their "
                   "triage colour until it is re-imported.")


class MissingProvenance(ValueError):
    """A compliance row was built without source system or import time.

    Standing rule 2, enforced where it cannot be forgotten. Raised at
    construction, so an unprovenanced row never reaches a widget.
    """


# ── schema ──────────────────────────────────────────────────────────────
# Additive only. `compliance_item` is created by aivionics.db; this extends
# it rather than declaring a second, competing table.

_OPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batch(
    batch_id       TEXT PRIMARY KEY,
    source_system  TEXT NOT NULL,
    source_file    TEXT,
    kind           TEXT,
    rows_total     INTEGER NOT NULL DEFAULT 0,
    rows_imported  INTEGER NOT NULL DEFAULT 0,
    rows_rejected  INTEGER NOT NULL DEFAULT 0,
    imported_at    TEXT NOT NULL,
    imported_by    TEXT
);
CREATE TABLE IF NOT EXISTS source_freshness(
    source_system  TEXT PRIMARY KEY,
    window_hours   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_compliance_tail ON compliance_item(aircraft_tail);
CREATE INDEX IF NOT EXISTS ix_compliance_kind ON compliance_item(kind);
"""

# (table, column, DDL type) — added when absent, never redefined.
_ADDED_COLUMNS = [
    ("compliance_item", "raised_at", "TEXT"),
    ("compliance_item", "status", "TEXT NOT NULL DEFAULT 'open'"),
    ("compliance_item", "source_ref", "TEXT"),
    ("aircraft", "icao24", "TEXT"),
    ("aircraft", "base_airport", "TEXT"),
]


def ensure_schema(con: sqlite3.Connection) -> None:
    """Add what Phase 4B needs on top of the core schema. Idempotent."""
    con.executescript(_OPS_SCHEMA)
    for table, column, ddl in _ADDED_COLUMNS:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if cols and column not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    con.commit()


# ── provenance ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Provenance:
    """Source system · import timestamp · staleness. Mandatory, always shown.

    The constructor is the enforcement point: a blank source system or a
    blank import time raises rather than rendering an alert that looks like
    it came from somewhere.
    """

    source_system: str
    imported_at: str
    stale: bool = False
    age_hours: float | None = None
    window_hours: float | None = None

    def __post_init__(self) -> None:
        if not str(self.source_system or "").strip():
            raise MissingProvenance(
                "source_system is required on every compliance row "
                "(standing rule 2)")
        if not str(self.imported_at or "").strip():
            raise MissingProvenance(
                "imported_at is required on every compliance row "
                "(standing rule 2)")

    def line(self) -> str:
        stamp = _short_stamp(self.imported_at)
        if self.stale:
            window = f"{self.window_hours:.0f} h" if self.window_hours else "its window"
            return (f"Source: {self.source_system} · imported {stamp} · "
                    f"STALE — older than {window}; verify against the CAMO")
        return (f"Source: {self.source_system} · imported {stamp} · "
                f"mirror of the CAMO export, not the legal record")


def _short_stamp(value: str) -> str:
    """`2026-08-08T09:12:04+00:00` -> `2026-08-08 09:12`. Never invents a value."""
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        return _parse_ts(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ── limits ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Limit:
    """One dimension of one item: how much is left, and against what window."""

    dimension: str            # calendar | hours | cycles
    unit: str                 # d | FH | FC
    limit: float              # the absolute limit as imported
    remaining: float | None   # None when the current value is not known
    window: float             # the warning window in the same unit

    @property
    def state(self) -> str:
        if self.remaining is None:
            return UNKNOWN
        if self.remaining <= 0:
            return BREACHED
        return WARNING if self.remaining <= self.window else OK

    @property
    def margin(self) -> float:
        """Remaining as a fraction of the warning window.

        Dimensionless on purpose. Comparing 3 days against 40 flight hours
        needs a common scale, and converting hours into days would require a
        utilisation rate this application does not have and must not invent
        (standing rule 4).
        """
        if self.remaining is None:
            return float("inf")
        return self.remaining / self.window if self.window else float(self.remaining)

    def remaining_text(self) -> str:
        if self.remaining is None:
            return "not evaluable"
        value = self.remaining
        if self.dimension == "calendar":
            days = int(value)
            return f"{days} d" if days >= 0 else f"{abs(days)} d overdue"
        shown = f"{abs(value):,.0f} {self.unit}"
        return shown if value >= 0 else f"{shown} overdue"

    def limit_text(self) -> str:
        if self.dimension == "calendar":
            return date.fromordinal(int(self.limit)).isoformat()
        return f"{self.limit:,.0f} {self.unit}"


@dataclass(frozen=True)
class DueState:
    """The row's triage state, the limit that drives it, and why."""

    state: str
    driver: Limit | None
    limits: tuple[Limit, ...]
    reason: str = ""

    @property
    def rank(self) -> int:
        return STATE_RANK[self.state]

    @property
    def sort_key(self) -> tuple[int, float]:
        """Worst first, then by time to limit. The triage order, once."""
        return (self.rank, self.driver.margin if self.driver else float("inf"))

    @property
    def badge(self) -> str:
        return STATE_BADGE[self.state]

    @property
    def word(self) -> str:
        return STATE_WORD[self.state]

    def due_text(self) -> str:
        return self.driver.limit_text() if self.driver else "no limit imported"

    def remaining_text(self) -> str:
        return self.driver.remaining_text() if self.driver else "—"


@dataclass(frozen=True)
class Item:
    """The imported facts about one compliance item, before any evaluation."""

    tail: str
    kind: str
    ref: str = ""
    description: str = ""
    mel_category: str | None = None
    due_date: date | None = None
    due_hours: float | None = None
    due_cycles: int | None = None
    raised_at: date | None = None
    id: int | None = None
    status: str = "open"


def mel_due_date(raised_at: date | None, category: str | None) -> date | None:
    """The calendar limit a MEL category implies, or None when it implies none.

    Categories B/C/D carry fixed rectification intervals (3, 10 and 120
    consecutive days) counted from the day after the deferral. Category A has
    no standard interval — it is whatever that MEL entry's own remarks say —
    so an A item is clockable only if the export carried an explicit date.
    """
    if raised_at is None:
        return None
    days = MEL_INTERVAL_DAYS.get(str(category or "").strip().upper())
    return raised_at + timedelta(days=days) if days else None


def due_state(item: Item, *, today: date | None = None,
              hours: float | None = None, cycles: int | None = None,
              warn_days: int = WARN_DAYS, warn_hours: float = WARN_HOURS,
              warn_cycles: int = WARN_CYCLES) -> DueState:
    """Evaluate one item against the tail's current state.

    `hours` and `cycles` are the aircraft's *current* totals. Passing None
    for either means the fleet register does not hold it — in which case a
    limit expressed in that dimension is reported as not evaluable, and the
    row can never come out `ok`. Reading "in limits" from an item whose
    hours limit could not be checked is precisely the failure standing rule 2
    exists to prevent.
    """
    today = today or datetime.now(timezone.utc).date()
    category = str(item.mel_category or "").strip().upper() or None

    calendar_due = item.due_date
    if calendar_due is None and item.kind == "mel":
        calendar_due = mel_due_date(item.raised_at, category)

    limits: list[Limit] = []
    if calendar_due is not None:
        window = MEL_WARN_DAYS.get(category, warn_days) if item.kind == "mel" \
            else warn_days
        limits.append(Limit("calendar", "d", float(calendar_due.toordinal()),
                            float((calendar_due - today).days), float(window)))
    if item.due_hours is not None:
        remaining = None if hours is None else float(item.due_hours) - float(hours)
        limits.append(Limit("hours", "FH", float(item.due_hours), remaining,
                            float(warn_hours)))
    if item.due_cycles is not None:
        remaining = None if cycles is None else float(item.due_cycles) - float(cycles)
        limits.append(Limit("cycles", "FC", float(item.due_cycles), remaining,
                            float(warn_cycles)))

    if not limits:
        reason = ("category A MEL with no date in the export — the interval is "
                  "in the MEL remarks, not in this data"
                  if item.kind == "mel" and category == "A"
                  else "no calendar, hours or cycles limit was imported for this item")
        return DueState(UNKNOWN, None, (), reason)

    # Worst state wins, and within a state the tightest margin drives it.
    ranked = sorted(limits, key=lambda l: (STATE_RANK[l.state], l.margin))
    driver = ranked[0]
    state = driver.state

    # A tracked dimension that could not be evaluated blocks "in limits" but
    # never suppresses an alert that another dimension has already raised.
    unresolved = [l for l in limits if l.state == UNKNOWN]
    if state == OK and unresolved:
        missing = " and ".join(sorted({l.dimension for l in unresolved}))
        return DueState(UNKNOWN, unresolved[0], tuple(limits),
                        f"the tail's current {missing} are not on the fleet "
                        f"register, so this limit cannot be checked")
    reason = ""
    if unresolved and state in (BREACHED, WARNING):
        reason = (f"the {unresolved[0].dimension} limit could not be checked — "
                  f"current value not on the fleet register")
    return DueState(state, driver, tuple(limits), reason)


# ── a renderable row ────────────────────────────────────────────────────

@dataclass(frozen=True)
class ComplianceRow:
    """One register line: the item, its evaluated state, and its provenance.

    `provenance` is not optional and is not defaulted. Constructing this
    object is the only way to get a row onto a screen, so "no alert renders
    without its provenance line visible" is a property of the type rather
    than a habit of whoever wrote the widget.
    """

    item: Item
    due: DueState
    provenance: Provenance

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, Provenance):
            raise MissingProvenance(
                "a compliance row cannot be built without a Provenance "
                "(standing rule 2): source system + import timestamp")

    @property
    def tail(self) -> str:
        return self.item.tail

    @property
    def kind_label(self) -> str:
        return KIND_LABEL.get(self.item.kind, self.item.kind)

    @property
    def badge_kind(self) -> str:
        """Greyed out while the source is stale — an alert nobody can trust
        must not wear the colour of one that can."""
        return "unknown" if self.provenance.stale else self.due.badge

    @property
    def badge_word(self) -> str:
        return f"{self.due.word} · STALE" if self.provenance.stale else self.due.word

    def title(self) -> str:
        ref = (self.item.ref or "").strip()
        description = (self.item.description or "").strip()
        if ref and description:
            return f"{ref} — {description}"
        return ref or description or "(no reference or description imported)"


# ── freshness ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Freshness:
    """When a source last delivered, and whether that is still acceptable."""

    source_system: str
    last_import: datetime | None
    window_hours: float
    rows: int = 0

    @property
    def age_hours(self) -> float | None:
        if self.last_import is None:
            return None
        return (datetime.now(timezone.utc) - self.last_import).total_seconds() / 3600.0

    def age_at(self, now: datetime) -> float | None:
        if self.last_import is None:
            return None
        return (now - self.last_import).total_seconds() / 3600.0

    def is_stale(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        age = self.age_at(now)
        return True if age is None else age > self.window_hours

    def line(self, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        if self.last_import is None:
            return f"{self.source_system}: never imported"
        age = self.age_at(now) or 0.0
        verdict = "STALE" if self.is_stale(now) else "current"
        return (f"{self.source_system}: {self.rows:,} rows · imported "
                f"{self.last_import.strftime('%Y-%m-%d %H:%M')} "
                f"({age:.0f} h ago, window {self.window_hours:.0f} h) · {verdict}")


@dataclass(frozen=True)
class ModuleState:
    """Whether the whole register may be trusted at a glance right now."""

    sources: tuple[Freshness, ...]
    now: datetime

    @property
    def has_data(self) -> bool:
        return bool(self.sources)

    @property
    def degraded(self) -> bool:
        """One stale source degrades the register, not just its own rows.

        An engineer looking at an amber badge has no way to know which feed
        it came from, so partial trust is not a state this screen offers.
        """
        return any(f.is_stale(self.now) for f in self.sources)

    @property
    def stale_sources(self) -> tuple[Freshness, ...]:
        return tuple(f for f in self.sources if f.is_stale(self.now))

    def banner(self) -> str:
        if not self.degraded:
            return ""
        newest = max((f.last_import for f in self.stale_sources
                      if f.last_import is not None), default=None)
        when = newest.strftime("%Y-%m-%d %H:%M") if newest else "never"
        return DEGRADED_BANNER.format(when=when)

    def provenance_line(self) -> str:
        if not self.sources:
            return ("Source system: none · import timestamp: none · freshness: "
                    "unknown. No CAMO export has been imported, so no clock on "
                    "this screen comes from anywhere.")
        return " | ".join(f.line(self.now) for f in self.sources)


def freshness_windows(con: sqlite3.Connection) -> dict[str, float]:
    try:
        return {row[0]: float(row[1])
                for row in con.execute(
                    "SELECT source_system, window_hours FROM source_freshness")}
    except sqlite3.Error:
        return {}


def set_freshness_window(con: sqlite3.Connection, source_system: str,
                         window_hours: float) -> None:
    con.execute(
        "INSERT INTO source_freshness(source_system, window_hours) VALUES(?,?) "
        "ON CONFLICT(source_system) DO UPDATE SET window_hours=excluded.window_hours",
        (source_system, float(window_hours)))
    con.commit()


def module_state(con: sqlite3.Connection | None,
                 now: datetime | None = None) -> ModuleState:
    """Last import and staleness per source system, from the rows themselves."""
    now = now or datetime.now(timezone.utc)
    if con is None:
        return ModuleState((), now)
    windows = freshness_windows(con)
    try:
        rows = con.execute(
            "SELECT source_system, MAX(imported_at), COUNT(*) "
            "FROM compliance_item GROUP BY source_system "
            "ORDER BY source_system").fetchall()
    except sqlite3.Error:
        return ModuleState((), now)
    sources = []
    for source, last, count in rows:
        try:
            stamp = _parse_ts(last) if last else None
        except ValueError:
            stamp = None
        sources.append(Freshness(
            source_system=source or "(unnamed source)", last_import=stamp,
            window_hours=windows.get(source, DEFAULT_FRESHNESS_HOURS),
            rows=int(count)))
    return ModuleState(tuple(sources), now)


# ── loading ─────────────────────────────────────────────────────────────

def aircraft_state(con: sqlite3.Connection | None) -> dict[str, tuple]:
    """Current total time and cycles per tail, for the hours/cycles limits."""
    if con is None:
        return {}
    try:
        rows = con.execute(
            "SELECT tail, total_time_hrs, total_cycles FROM aircraft").fetchall()
    except sqlite3.Error:
        return {}
    return {str(tail).strip().upper(): (hours, cycles)
            for tail, hours, cycles in rows if tail}


_SELECT = """
    SELECT id, aircraft_tail, kind, ref, description, mel_category,
           due_date, due_hours, due_cycles, source_system, imported_at,
           raised_at, status
      FROM compliance_item
"""


def load_rows(con: sqlite3.Connection | None, *, kind: str | None = None,
              tail: str | None = None, now: datetime | None = None,
              state: ModuleState | None = None,
              limit: int | None = None) -> list[ComplianceRow]:
    """Read the register and evaluate it. Triage order, worst first.

    Every row that comes back carries a `Provenance`; a row whose stored
    source system or import timestamp is blank is **dropped**, not rendered
    without one. That is a data defect worth losing a row over.
    """
    if con is None:
        return []
    now = now or datetime.now(timezone.utc)
    state = state or module_state(con, now)
    stale_sources = {f.source_system: f for f in state.stale_sources}
    windows = {f.source_system: f.window_hours for f in state.sources}
    ages = {f.source_system: f.age_at(now) for f in state.sources}
    fleet = aircraft_state(con)

    sql, args = _SELECT, []
    where = ["COALESCE(status,'open') <> 'closed'"]
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if tail:
        where.append("UPPER(aircraft_tail) = ?")
        args.append(tail.strip().upper())
    sql += " WHERE " + " AND ".join(where)

    try:
        raw = con.execute(sql, tuple(args)).fetchall()
    except sqlite3.Error:
        return []

    today = now.date()
    rows: list[ComplianceRow] = []
    for (row_id, tail_value, item_kind, ref, description, category,
         due_date_text, due_hours, due_cycles, source, imported_at,
         raised_at_text, status) in raw:
        tail_key = str(tail_value or "").strip().upper()
        hours, cycles = fleet.get(tail_key, (None, None))
        item = Item(
            tail=tail_key or "(no tail)", kind=str(item_kind or "").strip().lower(),
            ref=ref or "", description=description or "",
            mel_category=category, due_date=_as_date(due_date_text),
            due_hours=_as_float(due_hours), due_cycles=_as_int(due_cycles),
            raised_at=_as_date(raised_at_text), id=row_id,
            status=status or "open")
        try:
            provenance = Provenance(
                source_system=source, imported_at=imported_at,
                stale=source in stale_sources,
                age_hours=ages.get(source), window_hours=windows.get(source))
        except MissingProvenance:
            continue
        rows.append(ComplianceRow(
            item=item,
            due=due_state(item, today=today, hours=hours, cycles=cycles),
            provenance=provenance))

    rows.sort(key=lambda r: (r.due.sort_key, r.item.tail, r.item.ref))
    return rows[:limit] if limit else rows


def triage_feed(con: sqlite3.Connection | None, *, limit: int = 8,
                now: datetime | None = None,
                state: ModuleState | None = None) -> list[ComplianceRow]:
    """The Home feed: breached and warning rows only, ordered by time to limit.

    Items already inside limits are deliberately absent — the homepage is a
    triage surface, and a clean item on it costs a line that a breached one
    needs.
    """
    rows = load_rows(con, now=now, state=state)
    urgent = [r for r in rows if r.due.state in (BREACHED, WARNING, UNKNOWN)]
    return urgent[:limit]


def counts_by_state(rows: Iterable[ComplianceRow]) -> dict[str, int]:
    counts = {BREACHED: 0, WARNING: 0, OK: 0, UNKNOWN: 0}
    for row in rows:
        counts[row.due.state] += 1
    return counts


def _as_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_float(value) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    try:
        return None if value in (None, "") else int(float(value))
    except (TypeError, ValueError):
        return None
