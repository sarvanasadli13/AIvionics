"""Take and verify a consistent backup of the application database.

Uses SQLite's online backup API, not a file copy. A copy taken while the
application holds the database open can capture a torn page or miss a WAL
that has not been checkpointed; the backup API takes a transactionally
consistent snapshot of a live database, which is the whole reason it exists.

An existing backup is never overwritten — a second run writes a timestamped
file beside it. A backup you can silently replace is not a backup.

    python scripts/backup_db.py
    python scripts/backup_db.py --db data/aivionics.db --name PRE-SOMETHING
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT / "src"))

from aivionics import audit, config, goldreview as G          # noqa: E402

QUESTIONNAIRE_TABLES = ("gold_review_response", "gold_review_draft",
                        "gold_review_history", "gold_set_release")


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def census(con: sqlite3.Connection) -> dict:
    def one(sql, default="—"):
        try:
            return con.execute(sql).fetchone()[0]
        except sqlite3.Error:
            return default

    out = {
        "gold_queue": one("SELECT COUNT(*) FROM gold_queue"),
        "gold_queue_done": one("SELECT COALESCE(SUM(done),0) FROM gold_queue"),
        "label_gold": one("SELECT COUNT(*) FROM label_gold"),
        "label_gold_pro": one("SELECT COUNT(*) FROM label_gold_pro"),
        "audit_log": one("SELECT COUNT(*) FROM audit_log"),
    }
    for table in QUESTIONNAIRE_TABLES:
        out[table] = one(f"SELECT COUNT(*) FROM {table}", "MISSING")
    try:
        out["queue_fingerprint"] = G.queue_fingerprint(con)
    except sqlite3.Error:
        out["queue_fingerprint"] = "—"
    return out


def verify(path: Path) -> tuple[dict, list[str]]:
    """Open the backup read-only and prove it is a usable database."""
    problems: list[str] = []
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        quick = con.execute("PRAGMA quick_check").fetchone()[0]
        if quick != "ok":
            problems.append(f"quick_check returned {quick!r}")
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            problems.append(f"{len(fk)} foreign-key violations")
        chain_ok, chain_rows = audit.verify_chain(con)
        if not chain_ok:
            problems.append("audit chain does not verify")
        data = census(con)
        data["quick_check"] = quick
        data["foreign_key_violations"] = len(fk)
        data["audit_chain"] = f"{'valid' if chain_ok else 'BROKEN'} " \
                              f"over {chain_rows} rows"
    finally:
        con.close()
    return data, problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Consistent SQLite backup")
    ap.add_argument("--db", type=Path, default=config.DB_PATH)
    ap.add_argument("--name", default="PRE-GOLD-REVIEW")
    ap.add_argument("--out", type=Path, default=ROOT / "db-backup")
    args = ap.parse_args()

    source = Path(args.db)
    if not source.exists():
        print(f"no database at {source}")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)

    target = args.out / f"aivionics.{args.name}.db"
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = args.out / f"aivionics.{args.name}.{stamp}.db"
        print(f"a backup already exists — writing {target.name} instead")

    free = shutil.disk_usage(args.out).free
    need = source.stat().st_size
    print(f"source {source}  {need / 1e9:.2f} GB   ·   free {free / 1e9:.1f} GB")
    if free < need * 1.05:
        print("not enough free space for the backup — refusing")
        return 1

    # ── the snapshot ──────────────────────────────────────────────────
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    before = census(src)
    dst = sqlite3.connect(str(target))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"\nbackup written: {target}")

    # ── verification ──────────────────────────────────────────────────
    data, problems = verify(target)
    size = target.stat().st_size
    digest = sha256(target)

    print(f"  absolute path   {target.resolve()}")
    print(f"  bytes           {size:,}")
    print(f"  sha256          {digest}")
    print()
    for key in ("quick_check", "foreign_key_violations", "audit_chain"):
        print(f"  {key:<24} {data[key]}")
    print()
    for key in ("gold_queue", "gold_queue_done", "label_gold", "label_gold_pro",
                *QUESTIONNAIRE_TABLES, "audit_log"):
        match = "" if data[key] == before.get(key) else \
            f"   <-- production has {before.get(key)}"
        print(f"  {key:<24} {data[key]}{match}")
    print(f"  {'queue_fingerprint':<24} {data['queue_fingerprint']}")

    # ── does it match production? ─────────────────────────────────────
    critical = ("gold_queue", "gold_queue_done", "label_gold", "label_gold_pro",
                "queue_fingerprint", *QUESTIONNAIRE_TABLES)
    for key in critical:
        if data.get(key) != before.get(key):
            problems.append(f"{key}: backup {data.get(key)!r} != "
                            f"production {before.get(key)!r}")

    print()
    if problems:
        print("VERIFICATION FAILED")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("VERIFICATION PASSED — the backup matches production and is intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
