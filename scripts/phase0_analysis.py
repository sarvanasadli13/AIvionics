"""Phase 0.5 repeat linkage + Phase 0.8 frequency baselines.

Run after scripts/phase0.py.

0.5 is deliberately the NAIVE linkage the plan calls inflated: same tail,
same ATA chapter, inside the window, with no symptom normalisation. It is
built so the inflation is measured rather than assumed — Phase 3.2 is what
must bring it down. The number printed here is not a repeat-defect rate.

0.8 uses the TRAIN split only. The frequency baseline is what Gate 2 has to
beat, so it may never see a test-split row.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from aivionics import db

WINDOW_DAYS = 90          # widest window stored; 30d is a filter on days_apart


def build_repeat_links(con: sqlite3.Connection) -> None:
    print("== Phase 0.5: repeat linkage ==")
    con.execute("CREATE INDEX IF NOT EXISTS ix_defect_link "
                "ON defect(aircraft_tail, ata_ref, reported_at)")
    eligible = con.execute(
        "SELECT COUNT(*) FROM defect WHERE aircraft_tail IS NOT NULL "
        "AND aircraft_tail<>'' AND ata_ref IS NOT NULL AND reported_at IS NOT NULL"
    ).fetchone()[0]
    print(f"  defects eligible for linkage: {eligible:,}")

    con.execute("DELETE FROM repeat_link")
    t0 = time.time()
    con.execute(f"""
        INSERT OR IGNORE INTO repeat_link(
            defect_id, repeat_defect_id, days_apart, same_tail, same_ata)
        SELECT a.id, b.id,
               CAST(julianday(b.reported_at) - julianday(a.reported_at) AS INTEGER),
               1, 1
        FROM defect a
        JOIN defect b
          ON b.aircraft_tail = a.aircraft_tail
         AND b.ata_ref       = a.ata_ref
         AND (b.reported_at > a.reported_at
              OR (b.reported_at = a.reported_at AND b.id > a.id))
         AND julianday(b.reported_at) - julianday(a.reported_at)
             BETWEEN 0 AND {WINDOW_DAYS}
        WHERE a.aircraft_tail IS NOT NULL AND a.aircraft_tail <> ''
          AND a.ata_ref IS NOT NULL
          AND a.reported_at IS NOT NULL AND b.reported_at IS NOT NULL
    """)
    con.commit()
    n90 = con.execute("SELECT COUNT(*) FROM repeat_link").fetchone()[0]
    n30 = con.execute(
        "SELECT COUNT(*) FROM repeat_link WHERE days_apart<=30").fetchone()[0]
    d30 = con.execute(
        "SELECT COUNT(DISTINCT defect_id) FROM repeat_link WHERE days_apart<=30"
    ).fetchone()[0]
    print(f"  linked pairs  <=90d: {n90:,}")
    print(f"  linked pairs  <=30d: {n30:,}")
    print(f"  distinct defects with a <=30d repeat: {d30:,} "
          f"({100*d30/eligible:.1f}% of eligible)")
    print(f"  [{time.time()-t0:.1f}s] NOTE: naive tail x chapter linkage, "
          "no symptom normalisation — inflated by construction (PLAN 0.5/3.2)")


def build_baselines(con: sqlite3.Connection) -> None:
    print("== Phase 0.8: top-20 task frequency per ATA chapter (train only) ==")
    con.execute("DELETE FROM baseline_freq")
    con.execute("""
        INSERT INTO baseline_freq(ata_chapter, task_number, cnt, rank)
        SELECT ata_chapter, task_number, cnt, rnk FROM (
            SELECT d.ata_ref AS ata_chapter,
                   ls.task_number AS task_number,
                   COUNT(*) AS cnt,
                   ROW_NUMBER() OVER (PARTITION BY d.ata_ref
                                      ORDER BY COUNT(*) DESC, ls.task_number) AS rnk
            FROM label_silver ls
            JOIN defect d ON d.id = ls.defect_id
            WHERE ls.split = 'train' AND d.ata_ref IS NOT NULL
            GROUP BY d.ata_ref, ls.task_number)
        WHERE rnk <= 20
    """)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM baseline_freq").fetchone()[0]
    ch = con.execute(
        "SELECT COUNT(DISTINCT ata_chapter) FROM baseline_freq").fetchone()[0]
    print(f"  baseline rows: {n:,} across {ch} ATA chapters")
    for c in ("34", "31", "22"):
        rows = con.execute(
            "SELECT rank, task_number, cnt FROM baseline_freq "
            "WHERE ata_chapter=? ORDER BY rank LIMIT 5", (c,)).fetchall()
        print(f"  ATA {c} top-5: " + (
            " · ".join(f"#{r}:{t}({n})" for r, t, n in rows) or "(no rows)"))


if __name__ == "__main__":
    con = db.connect()
    build_repeat_links(con)
    build_baselines(con)
