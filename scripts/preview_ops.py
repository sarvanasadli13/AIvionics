"""Render the Ops fleet map and airport page against seeded, synthetic data.

No socket is opened. The `OpsService` is given a fake transport that returns
canned OpenSky and aviationweather bodies, and the page's handlers are
invoked directly rather than through the thread pool, so the screenshot is
deterministic.

    python scripts/preview_ops.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtCore import Qt                                   # noqa: E402
from PySide6.QtWidgets import QApplication                      # noqa: E402

from aivionics import db                                        # noqa: E402
from aivionics.ops import compliance, net                       # noqa: E402
from aivionics.ui import fonts                                  # noqa: E402
from aivionics.ui.opsservice import OpsService                  # noqa: E402
from aivionics.ui.pages.ops import OpsPage                      # noqa: E402

OUT = ROOT / "docs" / "status"
AIRPORT = "EDDF"

# Tail, type, transponder address, and a plausible position for the map.
FLEET = [
    # tail, type, icao24, lat, lon, altitude m, speed m/s, track, v/s m/s, callsign
    ("N101AV", "B737-8", "3c6444", 50.03, 8.56, 11277.0, 235.0, 78.0, 2.5, "AVN101"),
    ("N202AV", "B737-8", "4008f3", 55.95, -3.37, 10058.0, 218.0, 265.0, -6.0, "AVN202"),
    ("N303AV", "A320-232", "a12b3c", 40.64, -73.78, 3200.0, 180.0, 310.0, 8.5, "AVN303"),
    ("N404AV", "E175", "484f21", 25.25, 55.36, 0.0, 12.0, 95.0, 0.0, "AVN404"),
    ("N505AV", "B737-8", "7c6b2d", -33.94, 151.18, 9144.0, 240.0, 20.0, 0.0, "AVN505"),
]
# N606AV is deliberately outside the state vectors: not seen is not not flying.
UNSEEN = ("N606AV", "A320-232", "3f9a11")
UNTRACKED = ("N707AV", "E175")

METAR = ("EDDF 081720Z 25012G24KT 210V280 3000 -RA BR BKN008 OVC020 "
         "14/12 Q1004 TEMPO 2000 RA")
TAF = ("TAF EDDF 081700Z 0818/0924 25012KT 4000 -RA BKN010 "
       "BECMG 0820/0822 27008KT 9999 SCT020 "
       "TEMPO 0900/0906 3000 RADZ BKN006")


def seed(con) -> None:
    compliance.ensure_schema(con)
    rows = [(i, tail, kind, address)
            for i, (tail, kind, address, *_) in enumerate(FLEET, start=1)]
    rows.append((len(rows) + 1, UNSEEN[0], UNSEEN[1], UNSEEN[2]))
    rows.append((len(rows) + 1, UNTRACKED[0], UNTRACKED[1], None))
    for i, tail, kind, address in rows:
        con.execute(
            "INSERT INTO aircraft(id,tail,type,msn,year_built,total_time_hrs,"
            "total_cycles,icao24) VALUES(?,?,?,?,?,?,?,?)",
            (i, tail, kind, f"6{i}230", 2016 + i, 18000 + i * 2600,
             9000 + i * 1400, address))

    defects = [
        ("2026-07-29", "34-11", "CAPT AIRSPEED UNRELIABLE ON TAKEOFF ROLL",
         "PITOT PROBE", "822-0917-002"),
        ("2026-06-14", "34-21", "ALT DISAGREE MESSAGE ON PFD DURING CLIMB",
         "AIR DATA MODULE", "622-1234-001"),
        ("2026-05-02", "32-42", "ANTISKID INOP ON LANDING ROLLOUT",
         "BRAKE CONTROL UNIT", "990-5511-007"),
    ]
    for i, (when, ata, text, part, pn) in enumerate(defects, start=1):
        con.execute(
            "INSERT INTO defect(id,aircraft_tail,reported_at,ata_ref,"
            "defect_text,rectification_text,source,sdr_year)"
            " VALUES(?,?,?,?,?,?,'sdr',?)",
            (i, FLEET[0][0], when, ata, text,
             f"REPLACED {part} PER AMM {ata}-01-400-801", 2026))
        con.execute(
            "INSERT INTO defect_action(defect_id,action_type,part_name,"
            "part_number,task_number) VALUES(?,?,?,?,?)",
            (i, "replaced", part, pn, f"{ata}-01-400-801"))

    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    batch = "preview-batch-1"
    con.execute(
        "INSERT INTO import_batch(batch_id,source_system,source_file,kind,"
        "rows_total,rows_imported,rows_rejected,imported_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (batch, "AMOS export", "amos-2026-08-08.csv", "mixed", 3, 3, 0, stamp))
    soon = (now + timedelta(days=9)).date().isoformat()
    items = [
        ("checkup", "C-CHECK 4", "C check — calendar, hours and cycles", None,
         soon, 21400.0, None),
        ("mel", "MEL 34-11-02A", "Capt pitot heat monitor inoperative", "B",
         None, None, None),
        ("adsb", "AD 2026-14-05", "Air data module software standard", None,
         (now + timedelta(days=53)).date().isoformat(), None, None),
    ]
    for i, (kind, ref, text, category, due, hours, cycles) in enumerate(items, 1):
        con.execute(
            "INSERT INTO compliance_item(id,aircraft_tail,kind,ref,description,"
            "mel_category,due_date,due_hours,due_cycles,source_system,"
            "imported_at,batch_id,raised_at,status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'open')",
            (i, FLEET[0][0], kind, ref, text, category, due, hours, cycles,
             "AMOS export", stamp, batch,
             (now - timedelta(days=2)).date().isoformat()))
    con.commit()


def canned(url: str, timeout: float) -> tuple[int, str]:
    """Stand-in transport. Recognises the three endpoints the page calls."""
    now = datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    if "/metar" in url:
        return 200, METAR
    if "/taf" in url:
        return 200, TAF
    if "/states/all" in url:
        states = []
        for _tail, _kind, address, lat, lon, alt, speed, track, rate, call in FLEET:
            states.append([address, f"{call}  ", "Germany", epoch, epoch,
                           lon, lat, alt, alt == 0.0, speed, track, rate,
                           None, alt + 90, "1000", False, 0])
        return 200, json.dumps({"time": epoch, "states": states})
    if "/flights/arrival" in url or "/flights/departure" in url:
        arriving = "/arrival" in url
        flights = []
        for i, (origin, call) in enumerate(
                [("KSEA", "DLH491"), ("LFPG", "AFR1218"), ("EGLL", "BAW906"),
                 ("OMDB", "UAE47"), ("LIRF", "ITY392")]):
            start = epoch - (i + 1) * 2400
            flights.append({
                "icao24": f"3c64{i}{i}", "callsign": call,
                "estDepartureAirport": origin if arriving else AIRPORT,
                "estArrivalAirport": AIRPORT if arriving else origin,
                "firstSeen": start, "lastSeen": start + 1800,
                "departureAirportCandidatesCount": 1,
                "arrivalAirportCandidatesCount": 3 if i == 1 else 1,
            })
        return 200, json.dumps(flights)
    return 404, ""


class _Ctx:
    def __init__(self, path: Path, theme: str = "light", online: bool = True):
        self.theme_name = theme
        self.db_path = path
        self.con = None
        self.corpus = None
        self.user = None
        self.window = None
        self.online_enabled = online


def shoot(app, widget, name: str) -> Path:
    # Native platform with WA_DontShowOnScreen, as in preview_stats.py:
    # QT_QPA_PLATFORM=offscreen reports an empty font database here and would
    # render a typeface the real application never uses.
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    for _ in range(12):
        app.processEvents()
    path = OUT / f"ui-preview-{name}.png"
    widget.grab().save(str(path))
    print(f"  {path.name}")
    return path


def build(path: Path, cache_dir: Path, online: bool = True) -> OpsPage:
    page = OpsPage(_Ctx(path, online=online))
    page.service = OpsService(
        path, online=lambda: online,
        client=net.NetClient(online=lambda: online, transport=canned,
                             cache=net.DiskCache(cache_dir),
                             limiter=net.RateLimiter(min_interval=0.0),
                             log=net.FetchLog()))
    return page


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="aivionics-ops-"))
    app = QApplication.instance() or QApplication(sys.argv[:1])
    try:
        path = tmp / "ops.db"
        con = db.connect(path)
        seed(con)
        con.close()

        app.setStyleSheet(fonts.qss("light"))

        page = build(path, tmp / "cache")
        page.resize(1400, 860)
        page._on_ready(page.service.warm())
        page._on_fleet(page.service.fleet())
        page.map.set_selected(FLEET[0][0])
        page._on_tail(page.service.tail_record(FLEET[0][0]))
        page.tabs.setCurrentIndex(0)
        shoot(app, page, "ops-map")

        page.tabs.setCurrentIndex(1)
        page.search.setText(AIRPORT)
        if page.results.count():
            page.results.setCurrentRow(0)
        page._on_airport(page.service.airport_detail(AIRPORT))
        # _on_airport also queues the same fetches on the thread pool, which
        # would leave the synchronous ones below reading their own cache and
        # the screenshot labelled "(cache)". Drop it so the image shows the
        # live path a user sees on first open.
        page.service.client.cache.clear()
        page._on_weather(page.service.weather(AIRPORT))
        page._on_movements(page.service.movements(AIRPORT))
        shoot(app, page, "ops-airport")
        page.service.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
