"""Phase 3 — build the case base and the normalised repeat links.

    python scripts/phase3.py                       # against data/aivionics.db
    python scripts/phase3.py --db data/scratch.db  # against anything else
    python scripts/phase3.py --rebuild             # discard mined rows first

Two passes, in this order and for a reason: the repeat linker reads
``defect_action`` to decide whether a repeat came after the same corrective
action, so the case base has to exist before the links are drawn.

Both passes are idempotent. ``--rebuild`` clears only rows this pipeline
mined — an engineer's promoted finding (``source`` other than ``sdr_mined``)
survives a full statistics rebuild.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from aivionics import config, db                                  # noqa: E402
from aivionics.stats import casebase, metrics, repeat             # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", type=Path, default=config.DB_PATH)
    p.add_argument("--window", type=int, default=repeat.DEFAULT_WINDOW_DAYS,
                   help="repeat window in days (default 30)")
    p.add_argument("--threshold", type=float, default=repeat.DEFAULT_THRESHOLD,
                   help="symptom token-overlap threshold, 0-1 (default 0.5)")
    p.add_argument("--max-forward", type=int, default=repeat.MAX_FORWARD_PAIRS,
                   help="cap on forward comparisons per defect (clique bound)")
    p.add_argument("--batch", type=int, default=50_000)
    p.add_argument("--limit", type=int, default=None,
                   help="stop the case-base pass after N defects (smoke test)")
    p.add_argument("--rebuild", action="store_true",
                   help="discard previously mined actions/findings/links")
    p.add_argument("--skip-casebase", action="store_true")
    p.add_argument("--skip-repeat", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not Path(args.db).exists():
        print(f"no database at {args.db} — run scripts/phase1.py and "
              f"scripts/phase0.py first", file=sys.stderr)
        return 1
    con = db.connect(args.db)
    started = time.time()

    if not args.skip_casebase:
        print("== Phase 3.1: case base (actions + findings) ==")
        counts = casebase.build(
            con, batch=args.batch, rebuild=args.rebuild, limit=args.limit,
            progress=lambda c: print(f"  scanned {c['scanned']:,} · "
                                     f"{c['actions']:,} actions · "
                                     f"{c['findings']:,} findings"))
        for key in ("scanned", "actions", "removals", "findings",
                    "confirmed_fault", "no_fault_found"):
            print(f"  {key}: {counts[key]:,}")
        if counts["scanned"]:
            print(f"  action extraction rate: "
                  f"{counts['actions'] / counts['scanned']:.1%}")

    if not args.skip_repeat:
        print(f"== Phase 3.2: normalised repeat linkage "
              f"(window {args.window} d, threshold {args.threshold}) ==")
        run = repeat.build(
            con, window_days=args.window, threshold=args.threshold,
            max_forward=args.max_forward, rebuild=True,
            progress=lambda r: print(f"  pairs {r.pairs:,} · "
                                     f"groups {r.groups:,}"))
        print(f"  eligible defects: {run.eligible:,}")
        print(f"  (tail x chapter) groups: {run.groups:,}")
        print(f"  normalised pairs: {run.pairs:,}")
        print(f"  defects with a repeat: {run.defects_with_repeat:,} "
              f"({run.rate:.1%} of eligible)")
        print(f"  defects that hit the forward cap: {run.capped_defects:,}")
        _report_chapters(con, args.window)

    print(f"== Phase 3 done in {time.time() - started:.1f}s ==")
    _report_headline(con, args.window)
    con.close()
    return 0


def _report_chapters(con, window: int) -> None:
    """Per-chapter repeat counts — the check that ATA 53 has stopped dominating."""
    rows = repeat.chapter_counts(con, window_days=window)
    total = sum(r["repeated"] for r in rows) or 1
    print("  top chapters by repeated defects:")
    for row in rows[:8]:
        print(f"    ATA {row['chapter']}: {row['repeated']:,} of "
              f"{row['eligible']:,} eligible ({row['repeated'] / total:.1%} "
              f"of all repeats)")


def _report_headline(con, window: int) -> None:
    """The Phase 3.3 metric, named correctly, with its n."""
    rate = metrics.removal_repeat_rate(con, window_days=window,
                                       label="fleet, all time")
    print(f"  metric: {rate.metric_name()}")
    print(f"  fleet: {rate.text}  CI95 {rate.interval_text}")
    print(f"  provenance: {rate.provenance_text}")
    mix = metrics.finding_mix(con)
    if mix:
        print("  finding mix: " + " · ".join(
            f"{k} {v:,}" for k, v in sorted(mix.items())))


if __name__ == "__main__":
    raise SystemExit(main())
