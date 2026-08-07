"""Phase 2 — the Gate 2 evaluation sweep.

Runs the four required baselines plus the reranked hybrid over the leak-free
test-split silver labels, prints the table and writes the JSON.

    python scripts/phase2_eval.py
    python scripts/phase2_eval.py --rerank flashrank --limit 500
    python scripts/phase2_eval.py --out docs/gate2_eval.json

GATE 2 reads off this table:
  * recall@50 >= 0.80
  * NDCG@5 beats the stratified frequency baseline by a meaningful margin
  * reranked beats un-reranked
  * confident-and-wrong measured and reported
Run scripts/phase2_index.py first.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from aivionics import config, db                              # noqa: E402
from aivionics.retrieval import evalharness                   # noqa: E402
from aivionics.retrieval.embedder import FakeEmbedder, FastEmbedder   # noqa: E402
from aivionics.retrieval.rerank import FlashRankReranker      # noqa: E402
from aivionics.retrieval.search import Searcher               # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--out", default=str(config.PROJECT_ROOT / "docs" / "gate2_eval.json"))
    ap.add_argument("--split", default="test")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the number of queries (smoke runs)")
    ap.add_argument("--threshold", type=float, default=evalharness.DEFAULT_THRESHOLD,
                    help="abstention threshold on the top-1 hybrid score")
    ap.add_argument("--rerank", choices=("none", "flashrank"), default="flashrank")
    ap.add_argument("--top-k", type=int, default=50)
    ap.add_argument("--fake", action="store_true",
                    help="hashed offline embedder — must match how the index was built")
    args = ap.parse_args()

    con = db.connect(Path(args.db))
    embedder = FakeEmbedder() if args.fake else FastEmbedder()
    reranker = None
    if args.rerank == "flashrank":
        reranker = FlashRankReranker()
        reranker.warmup()      # fail now, not silently mid-sweep
        print(f"reranker      : {reranker.model_name}")
    searcher = Searcher(con, embedder, reranker=reranker)

    ids, _ = searcher.vectors("task")
    if ids.size == 0:
        print(f"no task vectors for index_version {searcher.index_version} — "
              f"run scripts/phase2_index.py first")
        return 1

    queries = evalharness.load_eval_queries(con, split=args.split, limit=args.limit)
    if not queries:
        print(f"no leak-free labelled queries in split '{args.split}' — "
              f"Phase 0 must finish first")
        return 1
    print(f"queries: {len(queries):,}   task vectors: {ids.size:,}   "
          f"reranker: {args.rerank}")

    start = time.time()
    report = evalharness.run_all(
        con, searcher, queries=queries, threshold=args.threshold,
        split=args.split, top_k=args.top_k)
    report["elapsed_s"] = round(time.time() - start, 1)
    report["reranker"] = args.rerank

    print()
    print(evalharness.format_table(report))
    path = evalharness.write_json(report, args.out)
    print(f"\nwritten: {path}   ({report['elapsed_s']}s)")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
