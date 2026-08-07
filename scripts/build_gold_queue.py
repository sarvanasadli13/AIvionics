"""Phase 0.7 — build the 400-pair adjudication queue.

Feeds the adjudication tool. Selects leak-free TEST-split pairs stratified by
ATA chapter x function-code class (diagnostic vs action) x narrative-length
tercile, so the 7.5:1 action skew cannot swamp the diagnostic cases that the
gold set exists to measure.

Held out permanently: these pairs are never used for tuning, thresholds or
model selection (PLAN §5).

Run:  python scripts/build_gold_queue.py
Deterministic — a fixed seed, so re-running reproduces the same queue.
"""
from __future__ import annotations

import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from aivionics import db
from aivionics.parsers.boeing import classify_function

TARGET = 400
SEED = 20260808


def fetch_pool(con: sqlite3.Connection) -> list[tuple]:
    """Leak-free test-split pairs, one row per (defect, task_number)."""
    return con.execute("""
        SELECT ls.defect_id, ls.task_number, ls.function_code,
               COALESCE(d.ata_ref,'??'), LENGTH(d.defect_text)
        FROM label_silver ls
        JOIN defect d ON d.id = ls.defect_id
        WHERE ls.leak_free = 1 AND ls.split = 'test'
          AND d.defect_text IS NOT NULL AND LENGTH(d.defect_text) > 0
        GROUP BY ls.defect_id, ls.task_number
    """).fetchall()


def terciles(lengths: list[int]) -> tuple[int, int]:
    s = sorted(lengths)
    return s[len(s) // 3], s[2 * len(s) // 3]


def allocate(strata: dict[str, list], target: int) -> dict[str, int]:
    """Proportional allocation with largest-remainder, min 1 per stratum.

    Every stratum that exists gets at least one pair (up to the target), so
    thin-but-important cells — diagnostic tasks in small chapters — are not
    rounded out of the sample.
    """
    total = sum(len(v) for v in strata.values())
    if total <= target:
        return {k: len(v) for k, v in strata.items()}
    quota = {k: min(len(v), max(1, len(v) * target // total))
             for k, v in strata.items()}
    # trim or top up to hit the target exactly, largest strata first
    order = sorted(strata, key=lambda k: -len(strata[k]))
    while sum(quota.values()) > target:
        for k in reversed(order):
            if sum(quota.values()) <= target:
                break
            if quota[k] > 1:
                quota[k] -= 1
    while sum(quota.values()) < target:
        for k in order:
            if sum(quota.values()) >= target:
                break
            if quota[k] < len(strata[k]):
                quota[k] += 1
    return quota


def main() -> None:
    con = db.connect()
    pool = fetch_pool(con)
    print(f"  leak-free test-split pairs available: {len(pool):,}")
    if not pool:
        raise SystemExit("pool is empty — run scripts/phase0.py first")

    lo, hi = terciles([r[4] for r in pool])
    print(f"  narrative-length terciles: <={lo} / <={hi} / >{hi} chars")

    strata: dict[str, list] = defaultdict(list)
    for defect_id, task_number, func, ata, ln in pool:
        cls = classify_function(func or "")
        t = "T1" if ln <= lo else ("T2" if ln <= hi else "T3")
        strata[f"{ata}|{cls}|{t}"].append((defect_id, task_number))
    print(f"  strata: {len(strata)}")

    quota = allocate(strata, TARGET)
    rng = random.Random(SEED)
    chosen: list[tuple[str, int, str]] = []
    for stratum in sorted(strata):
        picks = rng.sample(strata[stratum], quota[stratum])
        chosen.extend((stratum, d, t) for d, t in picks)
    rng.shuffle(chosen)          # adjudicate in mixed order, avoid anchoring

    con.execute("DELETE FROM gold_queue")
    con.executemany(
        "INSERT OR IGNORE INTO gold_queue(defect_id,task_number,stratum,seq,done)"
        " VALUES(?,?,?,?,0)",
        [(d, t, s, i) for i, (s, d, t) in enumerate(chosen)])
    con.commit()

    n = con.execute("SELECT COUNT(*) FROM gold_queue").fetchone()[0]
    print(f"  queued: {n}")
    print("  distribution by function class:")
    for cls, c in con.execute(
            "SELECT substr(stratum, instr(stratum,'|')+1, "
            "instr(substr(stratum, instr(stratum,'|')+1),'|')-1) AS cls, "
            "COUNT(*) FROM gold_queue GROUP BY cls ORDER BY 2 DESC"):
        print(f"    {cls:11} {c}")
    print("  distribution by ATA chapter (top 12):")
    for ata, c in con.execute(
            "SELECT substr(stratum,1,instr(stratum,'|')-1) AS a, COUNT(*) "
            "FROM gold_queue GROUP BY a ORDER BY 2 DESC LIMIT 12"):
        print(f"    ATA {ata:4} {c}")
    print("  distribution by length tercile:")
    for t, c in con.execute(
            "SELECT substr(stratum,-2) AS t, COUNT(*) FROM gold_queue "
            "GROUP BY t ORDER BY t"):
        print(f"    {t} {c}")


if __name__ == "__main__":
    main()
