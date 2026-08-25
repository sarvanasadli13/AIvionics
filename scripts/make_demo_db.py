"""Build a small, real, shareable demo database.

    python scripts/make_demo_db.py

**Why a separate database rather than filling the production one.** The
`aircraft` register is empty, and everything keyed to a tail — the fleet map,
repeat defects, compliance — therefore has nothing to show. The tempting fix is
to insert a fleet into `data/aivionics.db`. That is forbidden, and rightly:
the production register is meant to be an *operator's* fleet, and inventing one
would put rows into the same table a real operator's aircraft would occupy,
where nothing afterwards distinguishes them.

So this writes `data/demo/aivionics-demo.db` and never opens the real database
for anything but reading.

**Nothing here is fabricated.** The tails are real N-numbers that already
appear in the FAA SDR data, chosen because they carry the most reported
defects, and every defect, action and finding attached to them is the real
record. The manuals and tasks are the real 737 MAX corpus. What the demo adds
is the one thing SDR does not contain — an `aircraft` row per tail, so the
register points at history that was already there.

The registration details a fleet register normally holds (MSN, line number,
build year, hours, cycles) are **left NULL**, because SDR does not publish them
and guessing them would be exactly the fabrication this file exists to avoid.

Run the app against it with:

    python -m aivionics.ui --db data/demo/aivionics-demo.db
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT / "src"))

from aivionics import config, db                      # noqa: E402
from aivionics.ui import auth                          # noqa: E402

DEMO_DIR = ROOT / "data" / "demo"
DEMO_DB = DEMO_DIR / "aivionics-demo.db"

# How many tails to register, and how many extra cases to carry so that
# retrieval has something to search that is not one of those tails.
FLEET_SIZE = 12
EXTRA_CASES = 40_000

# The fleet is picked on *recent* activity, not all-time. Ranking by total
# defects selects aircraft that were busy in the 1990s, and every screen that
# reports on a rolling window then has nothing in it — which is a correct
# answer to a badly chosen question.
FLEET_SINCE = "2022-01-01"

# Tables copied wholesale — small, and every screen depends on them.
WHOLE_TABLES = ("manual", "task", "task_link", "task_section",
                "effectivity_airplane", "aircraft_config", "mmsg", "mmsg_task",
                "baseline_freq", "calibration", "coverage")


def log(message: str) -> None:
    print(f"  {message}", flush=True)


def pick_fleet(con: sqlite3.Connection) -> list[tuple[str, int]]:
    """The tails with the most reported defects, read from the *source*.

    `src.defect`, not `defect`: at this point the demo table is empty, and
    querying it returned an empty fleet on the first run.
    """
    return [(str(tail).strip().upper(), int(n)) for tail, n in con.execute(
        "SELECT aircraft_tail, COUNT(*) n FROM src.defect "
        "WHERE aircraft_tail IS NOT NULL AND TRIM(aircraft_tail) <> '' "
        "AND reported_at >= ? "
        "GROUP BY aircraft_tail ORDER BY n DESC LIMIT ?",
        (FLEET_SINCE, FLEET_SIZE))]


def has_table(con: sqlite3.Connection, schema: str, table: str) -> bool:
    row = con.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    return row is not None


def mirror_missing_table(con: sqlite3.Connection, table: str) -> bool:
    """Create a table the base schema does not have, from the source's DDL.

    `repeat_norm` and `calibration` are made by the Phase 3 scripts rather than
    by `db.connect`, so a fresh database lacks them and the Reliability page
    reads an absent table as "no data". Copying the DDL keeps this general: the
    next phase script to add a table does not break the builder.
    """
    row = con.execute(
        "SELECT sql FROM src.sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    if not row or not row[0]:
        return False
    try:
        con.execute(row[0])
        for (index_sql,) in con.execute(
                "SELECT sql FROM src.sqlite_master WHERE type='index' "
                "AND tbl_name=? AND sql IS NOT NULL", (table,)):
            try:
                con.execute(index_sql)
            except sqlite3.Error:
                pass                    # an index is an optimisation, not data
        con.commit()
        log(f"{table}: created from the source schema")
        return True
    except sqlite3.Error as exc:
        log(f"{table}: could not be created — {exc}")
        return False


def copy_table(con: sqlite3.Connection, table: str, where: str = "") -> int:
    """Copy rows from the attached real database, committing as we go.

    Committing per table is not tidiness: the first build put everything in one
    transaction, and when it was interrupted the 55 MB write-ahead log was
    discarded and the database on disk was still empty.
    """
    if not has_table(con, "src", table):
        log(f"{table}: not in the source — skipped")
        return 0
    if not has_table(con, "main", table) and not mirror_missing_table(con, table):
        return 0
    try:
        columns = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        names = ", ".join(f'"{c}"' for c in columns)
        con.execute(f"INSERT INTO {table} ({names}) "
                    f"SELECT {names} FROM src.{table} {where}")
        con.commit()
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error as exc:
        con.rollback()
        log(f"{table}: skipped — {exc}")
        return 0


def main() -> int:
    if not config.DB_PATH.exists():
        print(f"the real database is not at {config.DB_PATH}")
        return 1

    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    if DEMO_DB.exists():
        DEMO_DB.unlink()
    for suffix in ("-wal", "-shm"):
        stale = DEMO_DB.with_name(DEMO_DB.name + suffix)
        stale.unlink(missing_ok=True)

    started = time.monotonic()
    print(f"building {DEMO_DB.relative_to(ROOT)}")

    # Create the schema with the real migrations, then reopen with uri=True so
    # the real database can be attached `mode=ro`. `PRAGMA query_only` is not
    # the tool for this: it is connection-wide, not per-schema, and setting it
    # after the ATTACH makes the demo database read-only too.
    db.connect(DEMO_DB).close()
    con = sqlite3.connect(str(DEMO_DB), uri=True)
    con.execute("PRAGMA journal_mode=WAL")
    auth.seed(con)
    con.execute("ATTACH DATABASE ? AS src",
                (f"file:{config.DB_PATH.as_posix()}?mode=ro",))

    fleet_rows = pick_fleet(con)
    fleet = [tail for tail, _n in fleet_rows]
    log("fleet: " + ", ".join(f"{tail} ({n:,} defects)" for tail, n in fleet_rows))

    for table in WHOLE_TABLES:
        count = copy_table(con, table)
        if count:
            log(f"{table}: {count:,}")

    quoted = ", ".join(f"'{t}'" for t in fleet)
    copied = copy_table(con, "defect", f"WHERE aircraft_tail IN ({quoted})")
    log(f"defect (fleet): {copied:,}")

    # A body of unrelated cases, so search is searching a corpus rather than
    # one aircraft's history.
    # Most recent, not oldest: `ORDER BY id` took the 1995 end of the corpus,
    # so every time-windowed view had nothing to show.
    con.execute(
        "INSERT INTO defect SELECT * FROM src.defect "
        "WHERE id NOT IN (SELECT id FROM defect) "
        "AND aircraft_tail IS NOT NULL ORDER BY reported_at DESC LIMIT ?",
        (EXTRA_CASES,))
    con.commit()
    total_defects = con.execute("SELECT COUNT(*) FROM defect").fetchone()[0]
    log(f"defect (total): {total_defects:,}")

    included = "SELECT id FROM defect"
    # Repeat chains are only ever displayed for a tail on the register, so the
    # 12 M-row table is filtered to the fleet rather than to every case carried.
    fleet_defects = (f"SELECT id FROM defect WHERE aircraft_tail IN ({quoted})")
    for table, where in (
            ("defect_action", f"WHERE defect_id IN ({included})"),
            ("defect_finding", f"WHERE defect_id IN ({included})"),
            ("repeat_link",
             f"WHERE defect_id IN ({fleet_defects}) "
             f"AND repeat_defect_id IN ({included})"),
            ("repeat_norm",
             f"WHERE defect_id IN ({fleet_defects}) "
             f"AND repeat_defect_id IN ({included})"),
            ("calibration", "")):
        count = copy_table(con, table, where)
        if count:
            log(f"{table}: {count:,}")

    # ── the one thing SDR has no row for ──────────────────────────────
    for tail in fleet:
        con.execute("INSERT OR IGNORE INTO aircraft(tail, type) VALUES(?, ?)",
                    (tail, "B737-8"))
    con.commit()
    log(f"aircraft: {len(fleet)} registered (type only — SDR publishes no MSN, "
        f"hours or cycles, and inventing them is not on)")

    # ── vectors, for the tasks and for the cases that came with them ──
    con.execute(
        "INSERT INTO vec_index(id, kind, ref_id, index_version, dim, vec) "
        "SELECT id, kind, ref_id, index_version, dim, vec FROM src.vec_index "
        "WHERE kind = 'task' OR ref_id IN (SELECT id FROM defect)")
    con.commit()
    log(f"vec_index: {con.execute('SELECT COUNT(*) FROM vec_index').fetchone()[0]:,}")

    # ── contentless FTS has to be written, not rebuilt ────────────────
    con.execute("INSERT INTO task_fts(rowid, task_number, title, embed_text) "
                "SELECT id, task_number, title, embed_text FROM task")
    con.execute("INSERT INTO case_fts(rowid, defect_text) "
                "SELECT id, defect_text FROM defect")
    con.commit()
    log("full-text indexes written")

    con.commit()
    con.execute("DETACH DATABASE src")
    con.isolation_level = None          # VACUUM cannot run inside a transaction
    con.execute("VACUUM")
    con.close()

    size_mb = DEMO_DB.stat().st_size / (1024 ** 2)
    print(f"\ndone in {time.monotonic() - started:.0f}s — "
          f"{DEMO_DB.relative_to(ROOT)}  {size_mb:,.0f} MB")
    print("open it with:  python -m aivionics.ui --db "
          f"{DEMO_DB.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
