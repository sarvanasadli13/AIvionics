"""Worker-thread bridge from the Ops screen to the fleet-operations package.

Mirrors `searchservice` and `statsservice`: its own read-only connection, all
work off the UI thread, a missing database rendered as a state rather than an
exception. Two things are specific to this one:

* **Nothing blocks the window.** The airport index costs ~1.4 s to build and
  `timezonefinder` another ~0.8 s to construct, and every online call has a
  network timeout of up to 8 s behind it. All of it goes through
  `net.submit`, which is the `QThreadPool` pattern standing rule 12 requires.
* **Offline is not an error path, it is the normal one.** `warm()` and the
  airport lookups never consult `online_enabled` at all; only `weather`,
  `movements` and `fleet` do, and each returns a result object carrying the
  reason instead of raising or returning None.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import config
from ..ops import adsb, airports, compliance, net, weather


@dataclass(frozen=True)
class AirportDetail:
    """Everything the airport page can render with the cable out."""

    airport: airports.Airport
    runways: tuple[airports.Runway, ...] = ()
    frequencies: tuple[airports.Frequency, ...] = ()
    timezone_name: str | None = None
    local_time: datetime | None = None

    @property
    def icao(self) -> str:
        return self.airport.icao or self.airport.ident

    def local_time_text(self) -> str:
        if not self.timezone_name:
            return "local time unavailable — no IANA zone resolves at this position"
        if self.local_time is None:
            return f"zone {self.timezone_name} is not in this tz database"
        return (f"{self.local_time.strftime('%H:%M')} · {self.timezone_name} · "
                f"{self.local_time.strftime('%a %d %b, UTC%z')}")


@dataclass(frozen=True)
class TailRecord:
    """A tail's defect history and compliance rows, for the map click-through."""

    tail: str
    defects: tuple[dict, ...] = ()
    compliance_rows: tuple[compliance.ComplianceRow, ...] = ()
    total_defects: int = 0
    reason: str = ""


@dataclass(frozen=True)
class Ready:
    """Whether the offline half is usable, and why not when it is not."""

    airports: int = 0
    timezone_ok: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.airports > 0


class OpsService:
    """One connection, one network client, one airport index. Reused."""

    def __init__(self, db_path: Path | str | None = None,
                 online: Callable[[], bool] | None = None,
                 client: net.NetClient | None = None) -> None:
        self.db_path = Path(db_path or config.DB_PATH)
        self.online = online or (lambda: False)
        self.client = client or net.NetClient(online=lambda: bool(self.online()))
        self._con: sqlite3.Connection | None = None
        self._tried = False

    # ── database ──────────────────────────────────────────────────────
    def connection(self) -> sqlite3.Connection | None:
        """Read-only, opened once, tolerant of a missing or locked file.

        `mode=ro` is not a precaution here, it is the contract: the Ops
        screen reads the fleet register and the compliance rows and writes
        nothing at all.
        """
        if self._tried:
            return self._con
        self._tried = True
        if not self.db_path.exists():
            return None
        try:
            self._con = sqlite3.connect(
                f"file:{self.db_path.as_posix()}?mode=ro", uri=True,
                timeout=30.0, check_same_thread=False)
            self._con.execute("PRAGMA query_only=ON")
        except sqlite3.Error:
            self._con = None
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    # ── offline ───────────────────────────────────────────────────────
    def warm(self) -> Ready:
        """Build the airport index and the timezone tables. Worker thread only."""
        index = airports.warm()
        if index.load_error:
            return Ready(reason=index.load_error)
        timezone_ok = airports.timezone_at(51.4775, -0.4614) is not None
        return Ready(
            airports=index.count, timezone_ok=timezone_ok,
            reason="" if timezone_ok else
            "timezonefinder is unavailable — local time cannot be resolved")

    def search_airports(self, query: str, limit: int = 12) -> list[airports.Airport]:
        return airports.search(query, limit)

    def airport_detail(self, code: str, now: datetime | None = None
                       ) -> AirportDetail | None:
        airport = airports.find(code)
        if airport is None:
            return None
        index = airports.index()
        zone = airports.timezone_for(airport)
        return AirportDetail(
            airport=airport,
            runways=index.runways(airport.ident),
            frequencies=index.frequencies(airport.ident),
            timezone_name=zone,
            local_time=airports.local_time(airport, now))

    def offline_provenance(self) -> str:
        return airports.index().provenance()

    # ── online ────────────────────────────────────────────────────────
    def weather(self, icao: str) -> tuple[weather.Report, weather.Report]:
        """METAR and TAF. Both carry their own reason when they fail."""
        return (weather.fetch_metar(self.client, icao),
                weather.fetch_taf(self.client, icao))

    def movements(self, icao: str) -> tuple[adsb.Movements, adsb.Movements]:
        return (adsb.fetch_arrivals(self.client, icao),
                adsb.fetch_departures(self.client, icao))

    def fleet(self) -> adsb.FleetSnapshot:
        return adsb.fleet_positions(self.client, self.connection())

    def activity(self) -> list[net.SourceActivity]:
        return self.client.log.rows()

    # ── click-through ─────────────────────────────────────────────────
    def tail_record(self, tail: str, *, limit: int = 6) -> TailRecord:
        """The defect and compliance history behind one map marker.

        Deliberately the *locator* view: dates, ATA references and what was
        replaced. Standing rule 6 forbids naming a person, and no query here
        selects one.
        """
        key = (tail or "").strip().upper()
        con = self.connection()
        if con is None:
            return TailRecord(tail=key, reason="no database — nothing to show")
        try:
            rows = con.execute(
                "SELECT d.id, d.reported_at, d.ata_ref, d.defect_text,"
                "       a.action_type, a.part_name, a.part_number, f.finding_type"
                "  FROM defect d"
                "  LEFT JOIN defect_action a ON a.defect_id = d.id"
                "  LEFT JOIN defect_finding f ON f.defect_id = d.id"
                " WHERE UPPER(TRIM(d.aircraft_tail)) = ?"
                " ORDER BY d.reported_at DESC, d.id DESC LIMIT ?",
                (key, limit)).fetchall()
            total = con.execute(
                "SELECT COUNT(*) FROM defect"
                " WHERE UPPER(TRIM(aircraft_tail)) = ?", (key,)).fetchone()[0]
        except sqlite3.Error as exc:
            return TailRecord(tail=key, reason=f"defect history unreadable — {exc}")

        defects = tuple({
            "id": row[0], "reported_at": row[1] or "", "ata_ref": row[2] or "",
            "defect_text": row[3] or "", "action_type": row[4] or "",
            "part_name": row[5] or "", "part_number": row[6] or "",
            "finding_type": row[7] or "not_recorded",
        } for row in rows)

        try:
            rows = compliance.load_rows(con, tail=key, limit=limit)
        except sqlite3.Error:
            rows = []
        return TailRecord(tail=key, defects=defects,
                          compliance_rows=tuple(rows), total_defects=int(total))

    # ── threading ─────────────────────────────────────────────────────
    def submit(self, fn: Callable[[], object]) -> "OpsSignals":
        """Run `fn` on the global pool and return the signals it will emit."""
        signals = OpsSignals()
        net.submit(fn, signals)
        return signals


def _signals_type():
    from PySide6.QtCore import QObject, Signal

    class OpsSignals(QObject):
        done = Signal(object)
        failed = Signal(str)

    return OpsSignals


def __getattr__(name: str):
    """Build the Qt type on first use, so this module imports without Qt."""
    if name == "OpsSignals":
        globals()["OpsSignals"] = _signals_type()
        return globals()["OpsSignals"]
    raise AttributeError(name)


__all__ = ["AirportDetail", "OpsService", "Ready", "TailRecord",
           "utc_now"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
