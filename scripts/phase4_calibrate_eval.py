"""Phase 4 — does per-chapter abstention hold up on data it was not fitted on?

Gate 2 §5b reports *fitted* precision: the precision the threshold achieved on
the very queries that chose it. That number cannot be quoted as the precision an
engineer would see. `calibration._best_threshold` walks down the score order and
keeps the **deepest** cut-off still meeting the target, which is precisely the
point where noise last pushed precision above the bar — the estimator is
optimistically biased by construction, and the size of that bias is unknown
until the threshold is applied to held-out queries.

So this script does what `phase2_calibrate.py` does not: it fits on **train**
and scores on **test**, and prints the two side by side. The gap between them is
the finding.

It also differs from `phase2_calibrate.py` in never writing to the database.
That script opens `db.connect()` (read-write, and `executescript(SCHEMA)` is a
write transaction) and calls `calibration.save()`. Measurement must not be able
to alter the corpus it is scoring, so everything here is `mode=ro` and the
fitted thresholds live in memory only.

    python scripts/phase4_calibrate_eval.py --targets 0.5,0.6,0.7,0.8
    python scripts/phase4_calibrate_eval.py --match strict --feature margin

Both splits are answerable-only and restricted to the airframe whose manuals are
held, for the reason `evalharness.load_eval_queries` documents: ATA task numbers
are not unique across manufacturers, so an unrestricted pool counts cross-type
number collisions as successes.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from aivionics import config                                       # noqa: E402
from aivionics.retrieval import calibration, evalharness           # noqa: E402
from aivionics.retrieval.embedder import FakeEmbedder, FastEmbedder  # noqa: E402
from aivionics.retrieval.search import Searcher                    # noqa: E402


def collect(searcher: Searcher, queries, key_fn, feature: str,
            label: str) -> dict[str, list[tuple[float, bool]]]:
    """One (confidence, was-top-1-correct) sample per query, grouped by the
    chapter the answer lives in. Reranking is off: the confidence signals are
    stage-1 properties and must not move with the reranker under test."""
    by_chapter: dict[str, list[tuple[float, bool]]] = {}
    start = time.time()
    for i, q in enumerate(queries, start=1):
        run = searcher.search(q.query, jasc=q.jasc, top_k=5, rerank=False)
        if not run.results:
            continue
        gold = {key_fn(t) for t in q.gold} - {None}
        correct = key_fn(run.results[0].task_number) in gold
        score = float(run.confidence.get(feature, 0.0))
        chapter = (q.gold_chapters[0] if q.gold_chapters else "") or ""
        by_chapter.setdefault(chapter, []).append((score, correct))
        if i % 500 == 0:
            print(f"  {label} {i:,}/{len(queries):,}", flush=True)
    print(f"  {label}: {sum(len(v) for v in by_chapter.values()):,} samples "
          f"in {time.time() - start:.0f}s", flush=True)
    return by_chapter


def apply(thresholds: list[calibration.Threshold],
          test_by_chapter: dict[str, list[tuple[float, bool]]]) -> dict:
    """Score fitted thresholds against held-out samples.

    Coverage is counted over *every* test query, including the chapters marked
    always-abstain and the ones with no fitted row. Reporting precision over the
    answered subset alone, without that denominator, is how an abstain-almost-
    always policy comes to look excellent.
    """
    cal = calibration.Calibrator({t.chapter: t for t in thresholds},
                                 feature=thresholds[0].feature if thresholds else "")
    rows, tot_n = [], 0
    tot_answered = tot_correct = tot_wrong_answered = 0
    for chapter, samples in sorted(test_by_chapter.items()):
        t = cal.for_chapter(chapter)
        answered = [(s, ok) for s, ok in samples
                    if t is not None and not t.always_abstain and s >= t.threshold]
        correct = sum(1 for _, ok in answered if ok)
        n = len(samples)
        tot_n += n
        tot_answered += len(answered)
        tot_correct += correct
        tot_wrong_answered += len(answered) - correct
        own = next((x for x in thresholds if x.chapter == chapter), None)
        rows.append({
            "chapter": chapter,
            "n_test": n,
            "threshold": (t.threshold if t else None),
            "always_abstain": bool(t.always_abstain) if t else False,
            "inherited_global": own is None,
            "fitted_precision": (own.precision if own else None),
            "fitted_coverage": (own.coverage if own else None),
            "fitted_n": (own.n if own else None),
            "test_precision": (correct / len(answered)) if answered else None,
            "test_coverage": (len(answered) / n) if n else 0.0,
            "test_answered": len(answered),
            "test_confident_wrong": ((len(answered) - correct) / n) if n else 0.0,
        })
    return {
        "rows": rows,
        "overall": {
            "n_test": tot_n,
            "answered": tot_answered,
            "coverage": (tot_answered / tot_n) if tot_n else 0.0,
            "precision": (tot_correct / tot_answered) if tot_answered else None,
            # The headline safety number: share of ALL queries that get an
            # answer which is wrong. Abstaining drives it down; so does
            # answering nothing, which is why coverage sits next to it.
            "confident_wrong_rate": (tot_wrong_answered / tot_n) if tot_n else 0.0,
        },
    }


def uncalibrated(test_by_chapter: dict[str, list[tuple[float, bool]]]) -> dict:
    """The shipped behaviour: answer every query. The baseline the thresholds
    have to beat."""
    n = sum(len(v) for v in test_by_chapter.values())
    correct = sum(1 for v in test_by_chapter.values() for _, ok in v if ok)
    return {"n_test": n, "answered": n, "coverage": 1.0,
            "precision": (correct / n) if n else None,
            "confident_wrong_rate": ((n - correct) / n) if n else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--fit-split", default="train")
    ap.add_argument("--eval-split", default="test")
    ap.add_argument("--fit-limit", type=int, default=None)
    ap.add_argument("--eval-limit", type=int, default=None)
    ap.add_argument("--feature", default="raw_dense_top",
                    choices=("raw_dense_top", "margin"))
    ap.add_argument("--match", default="relaxed", choices=("strict", "relaxed"))
    ap.add_argument("--targets", default="0.5,0.6,0.7,0.8",
                    help="target precisions to sweep; each gives a different "
                         "point on the precision/coverage curve")
    ap.add_argument("--aircraft-like", default="%737%")
    ap.add_argument("--all-aircraft", action="store_true")
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if args.fit_split == args.eval_split:
        print("fit and eval splits are the same — that measures the fit "
              "against itself and tells you nothing", file=sys.stderr)
        return 2

    con = evalharness.connect_read_only(args.db)
    searcher = Searcher(con, FakeEmbedder() if args.fake else FastEmbedder())
    ids, _ = searcher.vectors("task")
    if ids.size == 0:
        print("no task vectors — run scripts/phase2_index.py first")
        return 1

    airframe = None if args.all_aircraft else args.aircraft_like
    key_fn = evalharness.KEYS[args.match]
    fit_qs = evalharness.load_eval_queries(
        con, split=args.fit_split, answerable_only=True,
        aircraft_like=airframe, limit=args.fit_limit)
    eval_qs = evalharness.load_eval_queries(
        con, split=args.eval_split, answerable_only=True,
        aircraft_like=airframe, limit=args.eval_limit)

    # The splits are supposed to be disjoint by defect. Assert it rather than
    # trust it: a leaked defect would make the held-out numbers meaningless in
    # exactly the direction that flatters the result.
    overlap = {q.defect_id for q in fit_qs} & {q.defect_id for q in eval_qs}
    if overlap:
        print(f"FIT/EVAL LEAK: {len(overlap)} defect ids in both splits",
              file=sys.stderr)
        return 3

    print(f"fit {len(fit_qs):,} {args.fit_split} · eval {len(eval_qs):,} "
          f"{args.eval_split} · feature {args.feature} · {args.match} match · "
          f"airframe {airframe or 'any'} · disjoint defects confirmed")

    fit_samples = collect(searcher, fit_qs, key_fn, args.feature, "fit")
    eval_samples = collect(searcher, eval_qs, key_fn, args.feature, "eval")

    base = uncalibrated(eval_samples)
    report = {
        "date": time.strftime("%Y-%m-%d"),
        "index_version": searcher.index_version,
        "model": searcher.embedder.model_name,
        "feature": args.feature,
        "match": args.match,
        "aircraft_like": airframe,
        "fit_split": args.fit_split, "eval_split": args.eval_split,
        "n_fit": len(fit_qs), "n_eval": len(eval_qs),
        "labels": "silver — agreement with the cited task, not correctness",
        "uncalibrated": base,
        "sweeps": [],
    }

    print(f"\nuncalibrated (answer everything): precision "
          f"{base['precision']:.3f} · coverage 1.000 · confident-wrong "
          f"{base['confident_wrong_rate']:.3f} on n={base['n_test']}")

    header = (f"\n{'target':>7}{'thr(glob)':>11}{'coverage':>10}{'precision':>11}"
              f"{'conf-wrong':>12}{'answered':>10}")
    print(header)
    print("-" * len(header.strip("\n")))
    for target in [float(t) for t in args.targets.split(",") if t.strip()]:
        thresholds = calibration.fit(fit_samples, feature=args.feature,
                                     target=target)
        scored = apply(thresholds, eval_samples)
        o = scored["overall"]
        g = next((t for t in thresholds if t.chapter == ""), None)
        print(f"{target:>7.2f}{(g.threshold if g else 0):>11.3f}"
              f"{o['coverage']:>10.3f}"
              f"{(o['precision'] if o['precision'] is not None else float('nan')):>11.3f}"
              f"{o['confident_wrong_rate']:>12.3f}{o['answered']:>10}")
        report["sweeps"].append({
            "target": target,
            "thresholds": [t.__dict__ for t in thresholds],
            **scored,
        })

    # Per-chapter detail for the mid target, which is where the trade is
    # clearest; every target's rows are in the JSON.
    mid = report["sweeps"][len(report["sweeps"]) // 2]
    print(f"\n── per chapter · target {mid['target']:.0%} · fitted on "
          f"{args.fit_split}, scored on {args.eval_split} " + "─" * 20)
    head = (f"{'ch':<4}{'n':>5}{'thr':>9}{'fit-prec':>10}{'fit-cov':>9}"
            f"{'TEST-prec':>11}{'TEST-cov':>10}{'answered':>10}  note")
    print(head)
    print("-" * len(head))
    for r in sorted(mid["rows"], key=lambda r: -r["n_test"]):
        note = ("ALWAYS ABSTAIN" if r["always_abstain"] else
                "inherits global" if r["inherited_global"] else "")
        fp = f"{r['fitted_precision']:.1%}" if r["fitted_precision"] is not None else "—"
        fc = f"{r['fitted_coverage']:.1%}" if r["fitted_coverage"] is not None else "—"
        tp = f"{r['test_precision']:.1%}" if r["test_precision"] is not None else "—"
        print(f"{r['chapter'] or '??':<4}{r['n_test']:>5}"
              f"{(r['threshold'] or 0):>9.3f}{fp:>10}{fc:>9}{tp:>11}"
              f"{r['test_coverage']:>10.1%}{r['test_answered']:>10}  {note}")

    print("\nfit-prec is the precision the threshold reached on the queries "
          "that chose it; TEST-prec is what it reached on held-out queries. "
          "Only the second is a claim about the product.")

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nwritten: {path}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
