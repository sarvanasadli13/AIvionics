"""Read-only bridge from the Reliability and Fleet screens to `aivionics.stats`.

Mirrors `searchservice`: its own read-only connection, work off the UI thread,
and a missing database rendered as a state rather than an exception.

Two rules from PLAN §2 are enforced here rather than left to the pages:

* **Nothing is pooled across provenance.** `metrics.combine` refuses it, and
  this service never asks — SDR-mined proxies and operator-confirmed rates
  answer different questions and a merged figure would answer neither.
* **No query names a person.** Standing rule 6 and BetrVG §87(1)(6): a view
  that attributes outcomes to an engineer makes the tool suitable for
  performance monitoring, and engineers who believe they are measured write
  vaguer narratives — which poisons the only data source this rests on.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from .. import config
from ..stats import metrics


@dataclass
class ReliabilitySnapshot:
    """Everything the Reliability screen renders for one filter selection."""

    period_label: str = ""
    since: str | None = None
    fleet: metrics.Rate | None = None
    chapters: list[dict] = field(default_factory=list)
    parts: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    months: list[dict] = field(default_factory=list)
    stratified: metrics.Stratified | None = None
    findings: dict[str, int] = field(default_factory=dict)
    latest_report: str | None = None
    available: bool = True
    reason: str = ""

    @property
    def findings_recorded(self) -> int:
        return sum(n for k, n in self.findings.items() if k != "not_recorded")

    @property
    def findings_total(self) -> int:
        return sum(self.findings.values())

    @property
    def findings_text(self) -> str:
        """How much the case base actually knows — shown beside every rate."""
        total = self.findings_total
        if not total:
            return "no closed cases in this window"
        pct = 100.0 * self.findings_recorded / total
        return (f"{self.findings_recorded:,} of {total:,} cases record what was "
                f"found ({pct:.1f}%)")


def _connect(db_path: Path | str | None) -> sqlite3.Connection | None:
    p = Path(db_path or config.DB_PATH)
    if not p.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True,
                              timeout=30.0, check_same_thread=False)
        con.execute("PRAGMA query_only=ON")
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return con
    except sqlite3.Error:
        return None


class StatsService:
    """Owns the connection and runs snapshots on a worker thread."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path or config.DB_PATH)
        self._con: sqlite3.Connection | None = None
        self._pool = QThreadPool.globalInstance()
        self.busy = False

    def connection(self) -> sqlite3.Connection | None:
        if self._con is None:
            self._con = _connect(self.db_path)
        return self._con

    def ready(self) -> tuple[bool, str]:
        con = self.connection()
        if con is None:
            return False, "no database — run the Phase 0/1 ingest first"
        try:
            n = con.execute("SELECT COUNT(*) FROM defect_action").fetchone()[0]
        except sqlite3.Error:
            return False, "case base not built — run scripts/phase3.py"
        if not n:
            return False, "case base is empty — run scripts/phase3.py"
        return True, ""

    def snapshot(self, *, period_days: int, period_label: str,
                 tail: str | None = None,
                 window_days: int = metrics.DEFAULT_WINDOW_DAYS,
                 ) -> ReliabilitySnapshot:
        """Blocking. Called on the worker thread, never on the UI one."""
        ok, reason = self.ready()
        if not ok:
            return ReliabilitySnapshot(available=False, reason=reason,
                                       period_label=period_label)
        con = self.connection()
        since = metrics.period_start(con, period_days) if period_days else None
        return ReliabilitySnapshot(
            period_label=period_label,
            since=since,
            fleet=metrics.removal_repeat_rate(
                con, tail=tail, since=since, window_days=window_days,
                label=(tail or "fleet")),
            chapters=metrics.chapter_rates(
                con, since=since, tail=tail, window_days=window_days),
            parts=metrics.top_repeat_parts(
                con, since=since, tail=tail, window_days=window_days, limit=25),
            events=metrics.repeat_events(
                con, since=since, tail=tail, window_days=window_days, limit=200),
            months=metrics.monthly_counts(
                con, since=since, tail=tail, window_days=window_days),
            stratified=metrics.stratify_by_standard(
                con, since=since, window_days=window_days) if not tail else None,
            findings=metrics.finding_mix(con, since=since, tail=tail),
            latest_report=metrics.latest_report_date(con),
        )

    # ── fleet register (no rates, so no suppression involved) ────────────
    def aircraft(self) -> list[dict]:
        con = self.connection()
        if con is None:
            return []
        try:
            rows = con.execute(
                "SELECT a.tail, a.type, a.msn, a.line_number, a.year_built,"
                "       a.total_time_hrs, a.total_cycles,"
                "       (SELECT COUNT(*) FROM defect d WHERE d.aircraft_tail=a.tail)"
                "  FROM aircraft a ORDER BY a.tail").fetchall()
        except sqlite3.Error:
            return []
        keys = ("tail", "type", "msn", "line_number", "year_built",
                "total_time_hrs", "total_cycles", "defects")
        return [dict(zip(keys, r)) for r in rows]

    def config_records(self, tail: str) -> list[dict]:
        con = self.connection()
        if con is None:
            return []
        try:
            rows = con.execute(
                "SELECT c.sb_embodied, c.stc, c.software_load, c.effective_from"
                "  FROM aircraft_config c JOIN aircraft a ON a.id=c.aircraft_id"
                " WHERE a.tail=? ORDER BY c.effective_from DESC", (tail,)).fetchall()
        except sqlite3.Error:
            return []
        return [dict(zip(("sb_embodied", "stc", "software_load",
                          "effective_from"), r)) for r in rows]

    def tails(self) -> list[str]:
        con = self.connection()
        if con is None:
            return []
        try:
            rows = con.execute(
                "SELECT DISTINCT aircraft_tail FROM defect"
                " WHERE aircraft_tail IS NOT NULL AND aircraft_tail <> ''"
                " ORDER BY aircraft_tail LIMIT 5000").fetchall()
        except sqlite3.Error:
            return []
        return [r[0] for r in rows]

    # ── async ────────────────────────────────────────────────────────────
    def submit(self, signals: "StatsSignals", **kw) -> bool:
        if self.busy:
            return False
        self.busy = True
        self._pool.start(_SnapshotJob(self, signals, kw))
        return True

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None


class StatsSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _SnapshotJob(QRunnable):
    def __init__(self, service: StatsService, signals: StatsSignals, kw: dict):
        super().__init__()
        self.service = service
        self.signals = signals
        self.kw = kw

    def run(self) -> None:                                   # pragma: no cover
        try:
            self.signals.done.emit(self.service.snapshot(**self.kw))
        except Exception as exc:                             # noqa: BLE001
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.service.busy = False
