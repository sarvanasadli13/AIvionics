"""Phase 2 — build the retrieval indexes (FTS5 + vectors) over the corpus.

Run AFTER Phase 0/1 have populated task, defect and label_silver:

    python scripts/phase2_index.py                 # real bge-small vectors
    python scripts/phase2_index.py --fake          # offline smoke run
    python scripts/phase2_index.py --no-cases      # tasks only, much faster

Idempotent — a rerun for the same index_version replaces that version's rows.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from aivionics import config, db                              # noqa: E402
from aivionics.retrieval import indexer                       # noqa: E402
from aivionics.retrieval.embedder import (                    # noqa: E402
    FakeEmbedder, FastEmbedder, model_cache_dir)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--no-cases", action="store_true",
                    help="skip case vectors (labelled defects only anyway)")
    ap.add_argument("--fake", action="store_true",
                    help="hashed offline embedder — for wiring checks, not for Gate 2")
    ap.add_argument("--threads", type=int, default=None)
    args = ap.parse_args()

    embedder = (FakeEmbedder() if args.fake
                else FastEmbedder(batch_size=args.batch, threads=args.threads))

    print(f"db            : {args.db}")
    print(f"model         : {embedder.model_name}  dim {embedder.dim}")
    print(f"index_version : {embedder.index_version}")
    if not args.fake:
        print(f"model cache   : {model_cache_dir()}")

    con = db.connect(Path(args.db))
    tasks = con.execute("SELECT COUNT(*) FROM task").fetchone()[0]
    if not tasks:
        print("no task rows — run scripts/phase1.py first")
        return 1

    start = time.time()
    out = indexer.build_all(con, embedder, batch=args.batch,
                            progress=lambda m: print(m, flush=True),
                            cases=not args.no_cases)
    print(f"\ntask_fts rows : {out['task_fts']:,}")
    print(f"case_fts rows : {out['case_fts']:,}")
    print(f"task vectors  : {out['task_vectors']:,}")
    print(f"case vectors  : {out['case_vectors']:,}")
    print(f"elapsed       : {time.time() - start:.1f}s")

    print("\n== index stats ==")
    stats = indexer.index_stats(con)
    for row in stats["vectors"]:
        print(f"  {row['kind']:<5} {row['index_version']:<28} "
              f"{row['rows']:>8,} rows  dim {row['dim']}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
