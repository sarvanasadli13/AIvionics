"""Fill the remaining gaps in the demo database so every screen has content.

`make_demo_db.py` builds the fleet and its real defect history. Three screens
are still empty afterwards, and this finishes them:

* **AI Validation** — the demo database has no `gold_queue`, so the page opens
  on "no queue in this database". The 400 real pairs and the defects they
  cite are copied across from production, read-only.
* **Manuals** — the A320 training documents live only in production, so the
  training-vs-maintenance distinction has nothing to demonstrate.
* **Compliance** — `compliance_item` is empty in *both* databases, because it
  is filled by a CAMO export the project does not have.

**What is real and what is not.** Everything except the compliance rows is the
real record, copied rather than invented. The compliance rows are synthetic —
there is no CAMO export to draw on — so they are imported through the ordinary
importer under the source system `DEMO-CAMO (synthetic)`. Standing rule 2 puts
the source system and import time on screen beside every row, so the demo
states plainly that this part is demonstration data. Nothing else in the
product does that, and nothing else here needs to.

Production is opened read-only and never written.

    python scripts/make_demo_complete.py
"""
from __future__ import annotations

import argparse
import csv
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT / "src"))

from aivionics import config, documents as DOCS, goldreview  # noqa: E402
from aivionics.ops import importer                            # noqa: E402

DEMO = ROOT / "data" / "demo" / "aivionics-demo.db"
SOURCE_SYSTEM = "DEMO-CAMO (synthetic)"

# Enough variety that every state the Compliance page can render appears:
# in date, near due, overdue, each MEL category, and an AD/SB.
ITEMS = [
    ("checkup", "A-CHECK", "A-Check", None, 40),
    ("checkup", "C-CHECK", "C-Check", None, 220),
    ("checkup", "DAILY", "Daily inspection", None, 1),
    ("mel", "21-31-01", "Recirculation fan inoperative", "B", 3),
    ("mel", "32-42-05", "Anti-skid channel inoperative", "C", 10),
    ("mel", "33-51-02", "Cabin sign dim", "D", 120),
    ("mel", "34-11-07", "Standby airspeed indicator", "A", -2),
    ("adsb", "AD 2024-14-05", "Pitot heat controller inspection", None, 18),
    ("adsb", "SB 737-31-1234", "Display unit software update", None, -6),
    ("adsb", "AD 2025-03-11", "Slat track wear check", None, 75),
]


def copy_gold_queue(demo: sqlite3.Connection, prod_path: Path) -> dict:
    """Bring the 400 real pairs across, with the defects they cite."""
    demo.execute("ATTACH DATABASE ? AS prod",
                 (f"file:{prod_path.as_posix()}?mode=ro",))
    try:
        have = demo.execute("SELECT COUNT(*) FROM gold_queue").fetchone()[0]
        if have:
            return {"gold_queue": have, "defects_added": 0, "skipped": True}

        # The cited defects mostly are not in the demo's 12-tail slice.
        added = demo.execute("""
            INSERT OR IGNORE INTO defect
            SELECT * FROM prod.defect WHERE id IN
                (SELECT defect_id FROM prod.gold_queue)
        """).rowcount
        demo.execute("""
            INSERT OR IGNORE INTO gold_queue(id, defect_id, task_number,
                                             stratum, seq, done)
            SELECT id, defect_id, task_number, stratum, seq, 0
            FROM prod.gold_queue
        """)
        # `sdr_raw` carries the aircraft model the review is allowed to show.
        demo.execute("""
            INSERT OR IGNORE INTO sdr_raw
            SELECT * FROM prod.sdr_raw WHERE id IN
                (SELECT sdr_id FROM prod.defect WHERE id IN
                    (SELECT defect_id FROM prod.gold_queue)
                 AND sdr_id IS NOT NULL)
        """)
        demo.commit()
        return {"gold_queue": demo.execute(
            "SELECT COUNT(*) FROM gold_queue").fetchone()[0],
            "defects_added": added, "skipped": False}
    finally:
        demo.execute("DETACH DATABASE prod")


def copy_training_documents(demo: sqlite3.Connection,
                            prod_path: Path) -> dict:
    """Register the A320 training documents the Manuals demo needs."""
    DOCS.migrate(demo)
    demo.execute("ATTACH DATABASE ? AS prod",
                 (f"file:{prod_path.as_posix()}?mode=ro",))
    try:
        rows = demo.execute(
            "SELECT oem, aircraft_type, manual_type, doc_standard, "
            "       parser_plugin, revision, revision_date, is_current, "
            "       source_file, source_hash, ingested_at, doc_class, "
            "       display_title FROM prod.manual WHERE doc_class='training'"
        ).fetchall()
        added = 0
        for row in rows:
            existing = demo.execute(
                "SELECT 1 FROM manual WHERE source_hash=?", (row[9],)).fetchone()
            if existing:
                continue
            demo.execute(
                "INSERT INTO manual(oem, aircraft_type, manual_type, "
                "doc_standard, parser_plugin, revision, revision_date, "
                "is_current, source_file, source_hash, ingested_at, doc_class, "
                "display_title) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            added += 1
        demo.commit()
        return {"training_documents": added}
    finally:
        demo.execute("DETACH DATABASE prod")


def write_compliance_csv(demo: sqlite3.Connection, out: Path) -> int:
    """A CAMO-shaped export for the demo fleet. Synthetic, and labelled."""
    tails = [r[0] for r in demo.execute(
        "SELECT tail FROM aircraft ORDER BY tail")]
    if not tails:
        return 0
    rng = random.Random(20260825)          # deterministic between runs
    today = date.today()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["registration", "type", "reference", "description",
                         "category", "due_date"])
        rows = 0
        for tail in tails:
            for kind, ref, description, category, offset in ITEMS:
                if rng.random() < 0.25:    # not every aircraft carries every item
                    continue
                due = today + timedelta(days=offset + rng.randint(-4, 4))
                writer.writerow([tail, kind, ref, description, category or "",
                                 due.isoformat()])
                rows += 1
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Complete the demo database")
    ap.add_argument("--demo", type=Path, default=DEMO)
    ap.add_argument("--source", type=Path, default=config.DB_PATH)
    args = ap.parse_args()

    if not args.demo.exists():
        print(f"no demo database at {args.demo} — run scripts/make_demo_db.py")
        return 1
    if not args.source.exists():
        print(f"no source database at {args.source}")
        return 1

    # `uri=True` so ATTACH can open the source read-only by URI.
    demo = sqlite3.connect(str(args.demo), uri=True)
    demo.row_factory = sqlite3.Row
    print(f"demo   {args.demo}")
    print(f"source {args.source}  (read-only)\n")

    gold = copy_gold_queue(demo, args.source)
    print(f"  gold queue          {gold['gold_queue']} pairs"
          + ("  (already present)" if gold["skipped"]
             else f", {gold['defects_added']} defects copied"))

    docs = copy_training_documents(demo, args.source)
    print(f"  training documents  {docs['training_documents']} registered")

    goldreview.migrate(demo)
    print("  questionnaire schema applied")

    csv_path = args.demo.parent / "demo-camo-export.csv"
    written = write_compliance_csv(demo, csv_path)
    if written:
        report = importer.import_file(demo, csv_path,
                                      source_system=SOURCE_SYSTEM)
        print(f"  compliance          {written} rows written to "
              f"{csv_path.name}; imported: {report.ok}")
        if not report.ok:
            print("     " + report.summary())
    else:
        print("  compliance          no aircraft in the demo register")

    print("\nfinal counts")
    for table in ("aircraft", "defect", "task", "manual", "gold_queue",
                  "compliance_item"):
        try:
            n = demo.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            n = "—"
        print(f"  {table:<18} {n}")
    demo.close()
    print(f"\nrun it:  python -m aivionics.ui --db {args.demo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
