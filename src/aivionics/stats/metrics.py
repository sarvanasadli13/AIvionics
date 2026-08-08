"""Phase 3.3-3.7 — the rate layer.

**The metric has one name and it is not NFF.** It is *"removals on this part
number followed by a repeat of the same defect within 30 days"*. NFF is a shop
teardown verdict; this corpus contains no shop findings at all, so the word
may not appear on any figure this module produces (PLAN 3.3). ``METRIC_NAME``
is the string the UI renders, and it is defined here rather than in a page so
there is exactly one place it could be got wrong.

Four rules are enforced by the types rather than by review:

1. **No rate without its n.** ``Rate`` is the only object that carries one and
   it holds ``n`` in the same instance. There is no function anywhere in this
   package that returns a bare float rate.
2. **Small n is suppressed, not decorated.** Below ``min_support``,
   ``Rate.value`` is ``None`` and ``Rate.text`` reads *"n too small (n=3)"*.
   "43% from 7 cases" is statistically void and rhetorically powerful, which
   is the worst combination a safety tool can ship.
3. **Provenance never pools.** ``sdr_mined`` is a proxy computed from a
   reportable-occurrence sample; ``operator_confirmed`` would be a measurement
   from an operator's own tech log. ``combine`` raises rather than average
   them.
4. **Cross-standard pooling is a decision, not a default** (PLAN 3.7). Airframes
   built to different standards can pool to a directionally wrong answer —
   Simpson's paradox — so pooled figures come from ``standardise``, which
   reports whether the strata disagree.

**Deviation, stated rather than buried.** PLAN §4 requires statistics queries
to filter ``defect_closure.complete = 1``. SDR reports have no closure record
and never will — a service difficulty report is a notification, not a work
order — so applying that filter to the mined population returns zero rows
everywhere. The filter is therefore applied to ``operator_confirmed`` only,
where a closure genuinely exists, and the ``sdr_mined`` provenance label is
what carries the incompleteness for the mined population.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from ..db import stats_guard
from .casebase import REMOVAL_ACTIONS

METRIC_NAME = ("removals on this part number followed by a repeat of the same "
               "defect within {days} days")
METRIC_SHORT = "removal → repeat ≤{days} d"

SDR_MINED = "sdr_mined"
OPERATOR_CONFIRMED = "operator_confirmed"

PROVENANCE_TEXT = {
    SDR_MINED: ("SDR-mined proxy — a reportable-occurrence sample "
                "(14 CFR 121.703/145.221), not a measured rate"),
    OPERATOR_CONFIRMED: "operator-confirmed — from the operator's own tech log",
}

MIN_SUPPORT = 5
Z_95 = 1.959963984540054
DEFAULT_WINDOW_DAYS = 30

# Observation periods offered by the Reliability screen.
PERIODS: list[tuple[str, int]] = [
    ("1w", 7), ("1m", 30), ("3m", 91), ("6m", 182), ("1y", 365),
]

_SOURCES = {SDR_MINED: ("sdr",), OPERATOR_CONFIRMED: ("operator", "techlog")}


# ── the rate type ───────────────────────────────────────────────────────

def wilson_interval(k: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Wilson rather than the normal approximation because the interesting cases
    here sit near 0 and near 1 on small n, where the normal interval runs off
    the end of the scale and reports a negative lower bound.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class Rate:
    """A proportion that cannot be separated from its support.

    ``value`` is ``None`` below ``min_support``. That is the suppression rule
    expressed as a type: a caller cannot obtain a number to render, only the
    ``text`` that says why there isn't one.
    """

    numerator: int
    n: int
    provenance: str = SDR_MINED
    label: str = ""
    window_days: int = DEFAULT_WINDOW_DAYS
    min_support: int = MIN_SUPPORT

    @property
    def suppressed(self) -> bool:
        return self.n < self.min_support

    @property
    def value(self) -> float | None:
        if self.suppressed or self.n <= 0:
            return None
        return self.numerator / self.n

    @property
    def interval(self) -> tuple[float, float] | None:
        if self.suppressed or self.n <= 0:
            return None
        return wilson_interval(self.numerator, self.n)

    @property
    def text(self) -> str:
        """What the UI renders in a value cell. Always mentions n."""
        if self.suppressed:
            return f"n too small (n={self.n})"
        return f"{self.value * 100:.1f}%  n={self.n}"

    @property
    def interval_text(self) -> str:
        bounds = self.interval
        if bounds is None:
            return "—"
        return f"{bounds[0] * 100:.1f} – {bounds[1] * 100:.1f}%"

    @property
    def support_text(self) -> str:
        return f"{self.numerator} of {self.n}"

    @property
    def provenance_text(self) -> str:
        return PROVENANCE_TEXT.get(self.provenance, self.provenance)

    def metric_name(self) -> str:
        return METRIC_NAME.format(days=self.window_days)


def combine(rates: list[Rate], label: str = "") -> Rate:
    """Sum several rates into one. Refuses to cross a provenance boundary."""
    if not rates:
        return Rate(0, 0, SDR_MINED, label)
    provenances = {r.provenance for r in rates}
    if len(provenances) > 1:
        raise ValueError(
            "refusing to pool across provenance: "
            f"{sorted(provenances)} — an SDR-mined proxy and an "
            "operator-confirmed measurement answer different questions")
    windows = {r.window_days for r in rates}
    if len(windows) > 1:
        raise ValueError(f"refusing to pool across repeat windows: {sorted(windows)}")
    return Rate(sum(r.numerator for r in rates), sum(r.n for r in rates),
                rates[0].provenance, label, rates[0].window_days,
                rates[0].min_support)


# ── cross-standard stratification (PLAN 3.7) ────────────────────────────

# Boundaries follow the plan's own example — a 1999-standard airframe and a
# 2015-standard airframe are different machines wearing the same ATA chapters.
STANDARD_BUCKETS: list[tuple[str, int | None, int | None]] = [
    ("pre-2000", None, 1999),
    ("2000–2014", 2000, 2014),
    ("2015+", 2015, None),
]
UNKNOWN_STANDARD = "year unknown"


def airframe_standard(year_built: int | None) -> str:
    if not year_built:
        return UNKNOWN_STANDARD
    for name, lo, hi in STANDARD_BUCKETS:
        if (lo is None or year_built >= lo) and (hi is None or year_built <= hi):
            return name
    return UNKNOWN_STANDARD


@dataclass(frozen=True)
class Stratified:
    """Per-standard rates plus the verdict on whether pooling them is safe."""

    strata: list[Rate]
    crude: Rate

    @property
    def reportable(self) -> list[Rate]:
        return [r for r in self.strata if not r.suppressed]

    @property
    def direction_conflict(self) -> bool:
        """True when the pooled figure sits outside every stratum's own figure.

        That is the signature of a Simpson reversal: each subgroup says one
        thing and the total says another, because the subgroups are unequally
        sized. When it fires, the pooled number must not be shown alone.
        """
        values = [r.value for r in self.reportable if r.value is not None]
        crude = self.crude.value
        if crude is None or len(values) < 2:
            return False
        return not (min(values) - 1e-9 <= crude <= max(values) + 1e-9)

    def standardised(self, weights: dict[str, float] | None = None) -> Rate:
        """Directly standardised rate — the down-weighting hook of PLAN 3.7.

        Each stratum contributes its own rate at a reference weight instead of
        at its sample size, so a stratum that merely happens to be numerous
        cannot carry the fleet figure. Default weights are equal across the
        reportable strata; pass measured fleet proportions to do better.
        """
        usable = self.reportable
        if not usable:
            return Rate(0, self.crude.n, self.crude.provenance,
                        "standardised (no stratum above the support threshold)",
                        self.crude.window_days)
        chosen = weights or {r.label: 1.0 for r in usable}
        total_w = sum(chosen.get(r.label, 0.0) for r in usable)
        if total_w <= 0:
            return Rate(0, 0, self.crude.provenance, "standardised (no weight)",
                        self.crude.window_days)
        n = sum(r.n for r in usable)
        pooled = sum(chosen.get(r.label, 0.0) * (r.value or 0.0)
                     for r in usable) / total_w
        return Rate(round(pooled * n), n, self.crude.provenance,
                    "standardised across airframe standards",
                    self.crude.window_days)


# ── queries ─────────────────────────────────────────────────────────────

def _run(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list:
    """Every statistics query goes through here so the guard cannot be skipped."""
    stats_guard(sql)
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _filters(*, tail: str | None, ata_chapter: str | None,
             part_number: str | None, since: str | None,
             provenance: str) -> tuple[str, list]:
    clauses, args = [], []
    sources = _SOURCES.get(provenance, ("sdr",))
    clauses.append(f"d.source IN ({','.join('?' * len(sources))})")
    args.extend(sources)
    if provenance == OPERATOR_CONFIRMED:
        # See the module docstring: the closure invariant applies only where a
        # closure record can exist.
        clauses.append("EXISTS(SELECT 1 FROM defect_closure c"
                       " WHERE c.defect_id=d.id AND c.complete=1)")
    if tail:
        clauses.append("d.aircraft_tail = ?")
        args.append(tail)
    if ata_chapter:
        clauses.append("d.ata_ref = ?")
        args.append(ata_chapter)
    if part_number:
        clauses.append("a.part_number = ?")
        args.append(part_number)
    if since:
        clauses.append("d.reported_at >= ?")
        args.append(since)
    return " AND ".join(clauses), args


_REMOVALS = ",".join("?" * len(REMOVAL_ACTIONS))


def latest_report_date(con: sqlite3.Connection) -> str | None:
    """The newest report in the database.

    Observation periods are measured back from here, not from today: the
    corpus is a static download, and a "last 7 days" window anchored on the
    wall clock would be empty and would read as "no defects" rather than
    "no data".
    """
    rows = _run(con, "SELECT MAX(reported_at) FROM defect")
    return rows[0][0] if rows and rows[0][0] else None


def period_start(con: sqlite3.Connection, period_days: int) -> str | None:
    anchor = latest_report_date(con)
    if not anchor:
        return None
    try:
        end = date.fromisoformat(str(anchor)[:10])
    except ValueError:
        return None
    return (end - timedelta(days=period_days)).isoformat()


def removal_repeat_rate(con: sqlite3.Connection, *, tail: str | None = None,
                        ata_chapter: str | None = None,
                        part_number: str | None = None,
                        since: str | None = None,
                        window_days: int = DEFAULT_WINDOW_DAYS,
                        provenance: str = SDR_MINED,
                        min_support: int = MIN_SUPPORT,
                        label: str = "") -> Rate:
    """The Phase 3.3 metric, for whatever slice the filters describe.

    Denominator: defects whose recorded action was a removal. Numerator: those
    followed by a normalised repeat of the same defect inside the window.
    """
    where, args = _filters(tail=tail, ata_chapter=ata_chapter,
                           part_number=part_number, since=since,
                           provenance=provenance)
    sql = f"""
        SELECT COUNT(*),
               SUM(CASE WHEN EXISTS(
                     SELECT 1 FROM repeat_norm r
                      WHERE r.defect_id = d.id AND r.days_apart <= ?)
                   THEN 1 ELSE 0 END)
          FROM defect d
          JOIN defect_action a ON a.defect_id = d.id
         WHERE a.action_type IN ({_REMOVALS}) AND {where}
    """
    rows = _run(con, sql, tuple([window_days, *REMOVAL_ACTIONS, *args]))
    n, k = (rows[0] if rows else (0, 0))
    return Rate(int(k or 0), int(n or 0), provenance, label, window_days,
                min_support)


def top_repeat_parts(con: sqlite3.Connection, *, since: str | None = None,
                     tail: str | None = None,
                     window_days: int = DEFAULT_WINDOW_DAYS,
                     provenance: str = SDR_MINED,
                     min_support: int = MIN_SUPPORT,
                     limit: int = 25) -> list[dict]:
    """Part numbers ordered by repeat count, each carrying its own Rate.

    Ordered by the repeat *count* rather than the rate, deliberately: ordering
    by rate puts a 1-of-1 at the top of the table, which is the exact figure
    the suppression rule exists to keep off the screen.
    """
    where, args = _filters(tail=tail, ata_chapter=None, part_number=None,
                           since=since, provenance=provenance)
    sql = f"""
        SELECT a.part_number,
               MIN(a.part_name),
               COUNT(*),
               SUM(CASE WHEN EXISTS(
                     SELECT 1 FROM repeat_norm r
                      WHERE r.defect_id = d.id AND r.days_apart <= ?)
                   THEN 1 ELSE 0 END) AS repeats,
               MIN(d.ata_ref)
          FROM defect d
          JOIN defect_action a ON a.defect_id = d.id
         WHERE a.action_type IN ({_REMOVALS})
           AND a.part_number IS NOT NULL AND a.part_number <> ''
           AND {where}
         GROUP BY a.part_number
         ORDER BY repeats DESC, COUNT(*) DESC
         LIMIT ?
    """
    rows = _run(con, sql, tuple([window_days, *REMOVAL_ACTIONS, *args, limit]))
    return [{"part_number": pn, "part_name": name or "—", "ata_chapter": ata or "—",
             "rate": Rate(int(k or 0), int(n or 0), provenance, pn, window_days,
                          min_support)}
            for pn, name, n, k, ata in rows]


def chapter_rates(con: sqlite3.Connection, *, since: str | None = None,
                  tail: str | None = None,
                  window_days: int = DEFAULT_WINDOW_DAYS,
                  provenance: str = SDR_MINED,
                  min_support: int = MIN_SUPPORT,
                  limit: int = 30) -> list[dict]:
    """Per-ATA-chapter removals and repeats, each with its Rate."""
    where, args = _filters(tail=tail, ata_chapter=None, part_number=None,
                           since=since, provenance=provenance)
    sql = f"""
        SELECT d.ata_ref, COUNT(*),
               SUM(CASE WHEN EXISTS(
                     SELECT 1 FROM repeat_norm r
                      WHERE r.defect_id = d.id AND r.days_apart <= ?)
                   THEN 1 ELSE 0 END) AS repeats
          FROM defect d
          JOIN defect_action a ON a.defect_id = d.id
         WHERE a.action_type IN ({_REMOVALS}) AND d.ata_ref IS NOT NULL
           AND {where}
         GROUP BY d.ata_ref
         ORDER BY repeats DESC, COUNT(*) DESC
         LIMIT ?
    """
    rows = _run(con, sql, tuple([window_days, *REMOVAL_ACTIONS, *args, limit]))
    return [{"chapter": ch, "rate": Rate(int(k or 0), int(n or 0), provenance,
                                         ch, window_days, min_support)}
            for ch, n, k in rows]


def repeat_events(con: sqlite3.Connection, *, since: str | None = None,
                  tail: str | None = None,
                  window_days: int = DEFAULT_WINDOW_DAYS,
                  limit: int = 200) -> list[dict]:
    """The repeat pairs themselves — the evidence behind every rate above."""
    clauses = ["r.days_apart <= ?"]
    args: list = [window_days]
    if tail:
        clauses.append("d.aircraft_tail = ?")
        args.append(tail)
    if since:
        clauses.append("d.reported_at >= ?")
        args.append(since)
    sql = f"""
        SELECT d.aircraft_tail, d.reported_at, r.ata_chapter, d.defect_text,
               r.days_apart, r.similarity, a.action_type, a.part_name,
               d.id, r.repeat_defect_id
          FROM repeat_norm r
          JOIN defect d ON d.id = r.defect_id
          LEFT JOIN defect_action a ON a.defect_id = d.id
         WHERE {' AND '.join(clauses)}
         ORDER BY d.reported_at DESC, r.days_apart
         LIMIT ?
    """
    rows = _run(con, sql, tuple([*args, limit]))
    keys = ("tail", "reported_at", "chapter", "symptom", "days_apart",
            "similarity", "action_type", "part_name", "defect_id",
            "repeat_defect_id")
    return [dict(zip(keys, row)) for row in rows]


def monthly_counts(con: sqlite3.Connection, *, since: str | None = None,
                   tail: str | None = None,
                   window_days: int = DEFAULT_WINDOW_DAYS,
                   provenance: str = SDR_MINED) -> list[dict]:
    """Removals and repeats per calendar month — the chart series."""
    where, args = _filters(tail=tail, ata_chapter=None, part_number=None,
                           since=since, provenance=provenance)
    sql = f"""
        SELECT substr(d.reported_at, 1, 7) AS ym, COUNT(*),
               SUM(CASE WHEN EXISTS(
                     SELECT 1 FROM repeat_norm r
                      WHERE r.defect_id = d.id AND r.days_apart <= ?)
                   THEN 1 ELSE 0 END)
          FROM defect d
          JOIN defect_action a ON a.defect_id = d.id
         WHERE a.action_type IN ({_REMOVALS}) AND d.reported_at IS NOT NULL
           AND {where}
         GROUP BY ym ORDER BY ym
    """
    rows = _run(con, sql, tuple([window_days, *REMOVAL_ACTIONS, *args]))
    return [{"month": ym, "removals": int(n or 0), "repeats": int(k or 0)}
            for ym, n, k in rows if ym]


def stratify_by_standard(con: sqlite3.Connection, *, since: str | None = None,
                         ata_chapter: str | None = None,
                         window_days: int = DEFAULT_WINDOW_DAYS,
                         provenance: str = SDR_MINED,
                         min_support: int = MIN_SUPPORT) -> Stratified:
    """Split the fleet rate by airframe standard (PLAN 3.7).

    Tails with no year of manufacture on the register fall into
    ``year unknown`` rather than being dropped or assumed modern.
    """
    where, args = _filters(tail=None, ata_chapter=ata_chapter,
                           part_number=None, since=since, provenance=provenance)
    sql = f"""
        SELECT ac.year_built, COUNT(*),
               SUM(CASE WHEN EXISTS(
                     SELECT 1 FROM repeat_norm r
                      WHERE r.defect_id = d.id AND r.days_apart <= ?)
                   THEN 1 ELSE 0 END)
          FROM defect d
          JOIN defect_action a ON a.defect_id = d.id
          LEFT JOIN aircraft ac ON ac.tail = d.aircraft_tail
         WHERE a.action_type IN ({_REMOVALS}) AND {where}
         GROUP BY ac.year_built
    """
    rows = _run(con, sql, tuple([window_days, *REMOVAL_ACTIONS, *args]))
    buckets: dict[str, list[int]] = {}
    for year, n, k in rows:
        acc = buckets.setdefault(airframe_standard(year), [0, 0])
        acc[0] += int(n or 0)
        acc[1] += int(k or 0)
    order = [name for name, _, _ in STANDARD_BUCKETS] + [UNKNOWN_STANDARD]
    strata = [Rate(buckets[name][1], buckets[name][0], provenance, name,
                   window_days, min_support)
              for name in order if name in buckets]
    crude = Rate(sum(b[1] for b in buckets.values()),
                 sum(b[0] for b in buckets.values()),
                 provenance, "fleet (crude)", window_days, min_support)
    return Stratified(strata=strata, crude=crude)


def finding_mix(con: sqlite3.Connection, *, since: str | None = None,
                tail: str | None = None) -> dict[str, int]:
    """How many closed cases actually recorded what was found.

    Rendered next to every rate because it is the honest measure of how much
    the case base knows: a corpus of removals with no findings is a record of
    what was swapped, not of what was wrong.
    """
    clauses, args = ["1=1"], []
    if tail:
        clauses.append("d.aircraft_tail = ?")
        args.append(tail)
    if since:
        clauses.append("d.reported_at >= ?")
        args.append(since)
    sql = f"""
        SELECT COALESCE(f.finding_type, 'not_recorded'), COUNT(*)
          FROM defect d
          LEFT JOIN defect_finding f ON f.defect_id = d.id
         WHERE {' AND '.join(clauses)}
         GROUP BY 1
    """
    return {kind: int(n) for kind, n in _run(con, sql, tuple(args))}
