"""PLAN 2.7 — fit per-ATA-chapter abstention thresholds.

Gate 2 measured a confident-and-wrong rate of 0.94. That was not a badly chosen
threshold: `search._max_norm` divides every score by the maximum, so the top hit
always scores exactly 1.0 and the merged top-1 can never fall below any absolute
threshold. The engine was structurally unable to decline to answer.

This fits thresholds against signals that survive normalisation — the raw cosine
of the best dense hit, and the margin to the runner-up — on the **train** split
only. Fitting on test would tune against the numbers Gate 2 reports.

    python scripts/phase2_calibrate.py --limit 3000
    python scripts/phase2_calibrate.py --feature margin --target 0.6
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from aivionics import config, db                                   # noqa: E402
from aivionics.retrieval import calibration, evalharness           # noqa: E402
from aivionics.retrieval.embedder import FakeEmbedder, FastEmbedder  # noqa: E402
from aivionics.retrieval.search import Searcher                    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--split", default="train",
                    help="fit on train; test is what Gate 2 reports")
    ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--feature", default="raw_dense_top",
                    choices=("raw_dense_top", "margin"))
    ap.add_argument("--target", type=float, default=calibration.TARGET_PRECISION)
    ap.add_argument("--match", default="relaxed", choices=("strict", "relaxed"),
                    help="what counts as a correct top-1 when fitting")
    ap.add_argument("--fake", action="store_true")
    args = ap.parse_args()

    if args.split == "test":
        print("refusing to fit on the test split — that tunes against the "
              "numbers Gate 2 reports", file=sys.stderr)
        return 2

    con = db.connect(Path(args.db))
    searcher = Searcher(con, FakeEmbedder() if args.fake else FastEmbedder())
    ids, _ = searcher.vectors("task")
    if ids.size == 0:
        print("no task vectors — run scripts/phase2_index.py first")
        return 1

    queries = evalharness.load_eval_queries(
        con, split=args.split, limit=args.limit, answerable_only=True)
    if not queries:
        print(f"no answerable leak-free queries in split '{args.split}'")
        return 1
    key_fn = evalharness.KEYS[args.match]
    print(f"fitting on {len(queries):,} answerable {args.split} queries · "
          f"feature {args.feature} · {args.match} match · target "
          f"precision {args.target:.0%}")

    by_chapter: dict[str, list[tuple[float, bool]]] = {}
    start = time.time()
    for i, q in enumerate(queries, start=1):
        run = searcher.search(q.query, jasc=q.jasc, top_k=5, rerank=False)
        if not run.results:
            continue
        gold = {key_fn(t) for t in q.gold} - {None}
        correct = key_fn(run.results[0].task_number) in gold
        score = float(run.confidence.get(args.feature, 0.0))
        chapter = (q.gold_chapters[0] if q.gold_chapters else "") or ""
        by_chapter.setdefault(chapter, []).append((score, correct))
        if i % 500 == 0:
            print(f"  {i:,}/{len(queries):,}", flush=True)

    thresholds = calibration.fit(by_chapter, feature=args.feature,
                                 target=args.target)
    calibration.save(con, searcher.index_version, thresholds)

    print(f"\n{'chapter':<9}{'n':>7}{'threshold':>11}{'precision':>11}"
          f"{'coverage':>10}  note")
    print("-" * 60)
    for t in sorted(thresholds, key=lambda t: (t.chapter == "", t.chapter)):
        note = "ALWAYS ABSTAIN" if t.always_abstain else ""
        label = t.chapter or "(global)"
        print(f"{label:<9}{t.n:>7}{t.threshold:>11.3f}{t.precision:>11.1%}"
              f"{t.coverage:>10.1%}  {note}")

    fitted = [t for t in thresholds if t.chapter]
    print(f"\n{len(fitted)} chapters fitted (support >= {calibration.MIN_SUPPORT}), "
          f"plus a global fallback · {time.time() - start:.0f}s")
    print("Coverage is the share of queries that would still be answered. "
          "An abstention rate of 1.0 scores perfectly on every other metric "
          "here and is worthless, so both numbers are reported together.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
