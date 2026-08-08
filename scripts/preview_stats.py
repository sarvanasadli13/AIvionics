"""Render the Reliability and Fleet screens against a seeded fixture database.

A fixture rather than the real corpus because these screens must be checked in
both the populated *and* the suppressed state: the interesting rendering is a
part number with four cases, where the rule says show "n too small" instead of
a percentage. Seeding lets that case exist on demand.

    python scripts/preview_stats.py
"""
from __future__ import annotations

import random
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtCore import Qt                                   # noqa: E402
from PySide6.QtWidgets import QApplication                      # noqa: E402

from aivionics import db                                        # noqa: E402
from aivionics.stats import schema as stats_schema              # noqa: E402
from aivionics.ui import fonts                                  # noqa: E402
from aivionics.ui.pages.fleet import FleetPage                  # noqa: E402
from aivionics.ui.pages.reliability import ReliabilityPage      # noqa: E402

OUT = ROOT / "docs" / "status"

TAILS = [("N101AV", "B737-8", 2019), ("N202AV", "B737-8", 2021),
         ("N303AV", "A320-232", 1998), ("N404AV", "E175", 2016)]
PARTS = [("622-1234-001", "AIR DATA MODULE", "34"),
         ("822-0917-002", "PITOT PROBE", "34"),
         ("285-4410-100", "FUEL QTY PROCESSOR", "28"),
         ("101-7788-030", "IDG", "24"),
         ("990-5511-007", "BRAKE CONTROL UNIT", "32")]
SYMPTOMS = ["CAPT AIRSPEED UNRELIABLE ON TAKEOFF ROLL",
            "FUEL QTY INDICATION FLUCTUATING IN CRUISE",
            "IDG DISCONNECT LIGHT ILLUMINATED",
            "ANTISKID INOP ON LANDING ROLLOUT",
            "ALT DISAGREE MESSAGE ON PFD"]


def seed(con) -> None:
    """A small fleet with a deliberate mix: enough support on two part numbers
    to report a rate, deliberately too little on another."""
    con.executescript(stats_schema.SCHEMA)
    rnd = random.Random(20260808)
    for i, (tail, typ, year) in enumerate(TAILS, start=1):
        con.execute(
            "INSERT INTO aircraft(id,tail,type,msn,line_number,year_built,"
            "total_time_hrs,total_cycles) VALUES(?,?,?,?,?,?,?,?)",
            (i, tail, typ, f"6{i}230", f"{7000 + i * 13}", year,
             18000 + i * 2600, 9000 + i * 1400))
        con.execute(
            "INSERT INTO aircraft_config(aircraft_id,sb_embodied,stc,"
            "software_load,effective_from) VALUES(?,?,?,?,?)",
            (i, f"SB 737-34-{1200 + i}", "STC ST01234NY-D",
             f"ADIRU L{i}.{i}", f"202{i}-03-1{i}"))

    start = date(2025, 1, 6)
    did = 0
    for week in range(78):
        when = start + timedelta(days=7 * week)
        for tail, _typ, _yr in TAILS:
            for pn, name, ata in PARTS:
                # thin support on the last part number, on purpose
                if pn.startswith("990") and week % 26:
                    continue
                if rnd.random() > 0.45:
                    continue
                did += 1
                sym = SYMPTOMS[hash(pn) % len(SYMPTOMS)]
                con.execute(
                    "INSERT INTO defect(id,aircraft_tail,reported_at,ata_ref,"
                    "defect_text,rectification_text,source,sdr_year)"
                    " VALUES(?,?,?,?,?,?,'sdr',?)",
                    (did, tail, when.isoformat(), ata, sym,
                     f"REPLACED {name} PER AMM {ata}-11-01-400-801", when.year))
                con.execute(
                    "INSERT INTO defect_action(defect_id,action_type,part_name,"
                    "part_number,task_number) VALUES(?,?,?,?,?)",
                    (did, "replaced", name, pn, f"{ata}-11-01-400-801"))
                if rnd.random() < 0.32:
                    con.execute(
                        "INSERT INTO defect_finding(defect_id,finding_type,"
                        "finding_text) VALUES(?,?,?)",
                        (did, "confirmed_fault",
                         "FOUND CHAFED WIRE AT CONNECTOR"))
    # repeats: a share of removals followed by the same defect inside 30 days
    rows = con.execute("SELECT id, aircraft_tail, ata_ref, reported_at"
                       " FROM defect ORDER BY id").fetchall()
    by_key: dict[tuple, list] = {}
    for rid, tail, ata, when in rows:
        by_key.setdefault((tail, ata), []).append((rid, when))
    for group in by_key.values():
        for (a_id, a_when), (b_id, b_when) in zip(group, group[1:]):
            gap = (date.fromisoformat(b_when) - date.fromisoformat(a_when)).days
            if gap <= 30 and rnd.random() < 0.5:
                con.execute(
                    "INSERT INTO repeat_norm(defect_id,repeat_defect_id,"
                    "ata_chapter,days_apart,similarity) VALUES(?,?,?,?,?)",
                    (a_id, b_id, None, gap, round(rnd.uniform(0.72, 0.98), 2)))
    con.commit()


class _Ctx:
    def __init__(self, path: Path, theme: str = "light"):
        self.theme_name = theme
        self.db_path = path
        self.con = None
        self.corpus = None
        self.user = None
        self.window = None


def shoot(app, widget, name: str) -> Path:
    # Native platform with WA_DontShowOnScreen: laid out and grabbed but never
    # mapped. QT_QPA_PLATFORM=offscreen reports an empty font database here and
    # would render a typeface the real application never uses.
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    for _ in range(10):
        app.processEvents()
    path = OUT / f"ui-preview-{name}.png"
    widget.grab().save(str(path))
    print(f"  {path.name}")
    return path


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="aivionics-stats-"))
    app = QApplication.instance() or QApplication(sys.argv[:1])
    try:
        path = tmp / "stats.db"
        con = db.connect(path)
        seed(con)
        con.close()

        app.setStyleSheet(fonts.qss("light"))
        rel = ReliabilityPage(_Ctx(path))
        rel.resize(1400, 860)
        rel.on_shown()
        snap = rel.service.snapshot(period_days=365, period_label="Last year")
        rel._on_snapshot(snap)
        shoot(app, rel, "reliability")
        rel.service.close()

        fleet = FleetPage(_Ctx(path))
        fleet.resize(1400, 860)
        fleet.on_shown()
        if fleet.table.rowCount():
            fleet.table.setCurrentCell(0, 0)
        shoot(app, fleet, "fleet")
        fleet.service.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
