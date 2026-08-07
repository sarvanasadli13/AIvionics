"""Evaluation harness (PLAN 2.8 / §5) — reports every metric on every run.

Metrics, and why each one is here:

  stage-1 recall     caps everything downstream; if the candidate pool misses
                     the task, no reranker can recover it
  recall@50          the hard ceiling — below this the reranker is decorating
                     a failure
  NDCG@5, Hit@5      final ranking quality. Hit@5 is reported alongside NDCG so
                     the numbers are directly comparable to Jo (2025)
  abstention rate    is the low-confidence gate usable at this threshold?
  confident-and-wrong  the score distribution of *incorrect* top-1 answers —
                     the failure mode nobody tests for

Two correctness definitions are reported side by side and never merged:

  strict   the returned task_number equals the labelled task_number
  relaxed  chapter-section-subject **and** function code agree, sequence number
           ignored — i.e. the right procedure at the right place, possibly a
           sibling revision of it

Baselines that must be beaten (PLAN §5): stratified top-20 frequency per ATA
chapter, FTS5 alone, vector alone, and hybrid without a reranker.

**These are silver labels.** They record what an engineer cited, not what was
correct. Everything here measures agreement with that, and the gold set (§5) is
what turns agreement into correctness.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from .search import SearchResult, SearchRun, Searcher

DEFAULT_THRESHOLD = 0.35


@dataclass(frozen=True)
class EvalQuery:
    defect_id: int
    query: str
    jasc: str | None
    gold: tuple[str, ...]


SearchFn = Callable[[EvalQuery], SearchRun]


# ── query set ────────────────────────────────────────────────────────────
def load_eval_queries(
    con: sqlite3.Connection,
    split: str = "test",
    leak_free: bool = True,
    limit: int | None = None,
) -> list[EvalQuery]:
    sql = [
        "SELECT d.id, d.defect_text, d.ata_ref, ls.task_number",
        "FROM label_silver ls JOIN defect d ON d.id = ls.defect_id",
        "WHERE TRIM(COALESCE(d.defect_text, '')) <> ''",
    ]
    params: list = []
    if leak_free:
        sql.append("AND ls.leak_free = 1")
    if split:
        sql.append("AND ls.split = ?")
        params.append(split)
    sql.append("ORDER BY d.id")

    grouped: dict[int, dict] = {}
    for did, text, jasc, task_number in con.execute(" ".join(sql), params):
        entry = grouped.setdefault(did, {"text": text, "jasc": jasc, "gold": []})
        if task_number not in entry["gold"]:
            entry["gold"].append(task_number)

    queries = [
        EvalQuery(defect_id=did, query=v["text"], jasc=v["jasc"],
                  gold=tuple(v["gold"]))
        for did, v in grouped.items() if v["gold"]
    ]
    return queries[:limit] if limit else queries


# ── correctness keys ─────────────────────────────────────────────────────
def strict_key(task_number: str | None) -> str | None:
    return task_number.strip().upper() if task_number else None


def relaxed_key(task_number: str | None) -> str | None:
    """chapter-section-subject + function code; sequence number dropped."""
    if not task_number:
        return None
    parts = task_number.strip().upper().split("-")
    return "-".join(parts[:4]) if len(parts) >= 4 else "-".join(parts)


KEYS = {"strict": strict_key, "relaxed": relaxed_key}


# ── metric primitives ────────────────────────────────────────────────────
def ndcg_at_k(hits: Sequence[bool], n_gold: int, k: int) -> float:
    """Binary relevance. IDCG assumes the best possible placement of however
    many gold items could fit in k."""
    dcg = sum(1.0 / math.log2(i + 2) for i, hit in enumerate(hits[:k]) if hit)
    ideal_n = min(max(n_gold, 1), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_n))
    return dcg / idcg if idcg else 0.0


def _pct(values: Sequence[float], q: float) -> float:
    return float(np.percentile(values, q)) if len(values) else 0.0


# ── scoring one run ──────────────────────────────────────────────────────
def _mode_metrics(run: SearchRun, q: EvalQuery, key_fn, k_hit: int, k_recall: int) -> dict:
    gold = {key_fn(t) for t in q.gold} - {None}
    stage1 = [key_fn(r.task_number) for r in run.ranked]
    final = [key_fn(r.task_number) for r in run.results]
    hits = [t in gold for t in final[:k_hit]]
    return {
        "stage1_hit": bool(gold & set(stage1)),
        "recall_at_k": bool(gold & set(stage1[:k_recall])),
        "hit_at_k": any(hits),
        "ndcg_at_k": ndcg_at_k(hits, len(gold), k_hit),
        "top1_correct": bool(final and final[0] in gold),
    }


def evaluate(
    con_or_none,
    search_fn: SearchFn,
    name: str,
    queries: Sequence[EvalQuery],
    threshold: float = DEFAULT_THRESHOLD,
    k_hit: int = 5,
    k_recall: int = 50,
) -> dict:
    """Run ``search_fn`` over every query and aggregate. ``con_or_none`` is
    accepted so the signature matches the 'connection + search function' contract
    even when the search function already closes over its own connection."""
    per_mode = {m: {"stage1": [], "recall": [], "hit": [], "ndcg": [], "top1": []}
                for m in KEYS}
    top1_scores: list[float] = []
    wrong_scores = {m: [] for m in KEYS}

    for q in queries:
        run = search_fn(q)
        score = float(run.results[0].score) if run.results else 0.0
        top1_scores.append(score)
        for mode, key_fn in KEYS.items():
            m = _mode_metrics(run, q, key_fn, k_hit, k_recall)
            per_mode[mode]["stage1"].append(m["stage1_hit"])
            per_mode[mode]["recall"].append(m["recall_at_k"])
            per_mode[mode]["hit"].append(m["hit_at_k"])
            per_mode[mode]["ndcg"].append(m["ndcg_at_k"])
            per_mode[mode]["top1"].append(m["top1_correct"])
            if not m["top1_correct"]:
                wrong_scores[mode].append(score)

    n = len(queries)
    mean = lambda xs: float(np.mean(xs)) if xs else 0.0  # noqa: E731

    report = {
        "name": name,
        "n_queries": n,
        "threshold": threshold,
        "k_hit": k_hit,
        "k_recall": k_recall,
        "abstention_rate": mean([s < threshold for s in top1_scores]),
        "top1_score": {
            "mean": mean(top1_scores),
            "p50": _pct(top1_scores, 50),
            "p90": _pct(top1_scores, 90),
            "min": float(min(top1_scores)) if top1_scores else 0.0,
            "max": float(max(top1_scores)) if top1_scores else 0.0,
        },
    }
    for mode in KEYS:
        d = per_mode[mode]
        ws = wrong_scores[mode]
        report[mode] = {
            "stage1_recall": mean(d["stage1"]),
            f"recall_at_{k_recall}": mean(d["recall"]),
            f"hit_at_{k_hit}": mean(d["hit"]),
            f"ndcg_at_{k_hit}": mean(d["ndcg"]),
            "top1_accuracy": mean(d["top1"]),
            "confident_wrong": {
                "n_wrong": len(ws),
                "rate_of_all": (len(ws) / n) if n else 0.0,
                "score_mean": mean(ws),
                "score_p50": _pct(ws, 50),
                "score_p75": _pct(ws, 75),
                "score_p90": _pct(ws, 90),
                "score_max": float(max(ws)) if ws else 0.0,
                "n_above_threshold": int(sum(1 for s in ws if s >= threshold)),
                "rate_above_threshold": (
                    sum(1 for s in ws if s >= threshold) / n) if n else 0.0,
            },
        }
    return report


# ── search functions under test ──────────────────────────────────────────
def hybrid_fn(searcher: Searcher, rerank: bool = True, top_k: int = 50,
              use_jasc: bool = True) -> SearchFn:
    def fn(q: EvalQuery) -> SearchRun:
        return searcher.search(q.query, jasc=q.jasc if use_jasc else None,
                               top_k=top_k, rerank=rerank)
    return fn


def channel_fn(searcher: Searcher, channel: str, top_k: int = 50) -> SearchFn:
    """FTS-alone or vector-alone, with no reranker and no JASC boost — the
    baselines are meant to be bare."""
    def fn(q: EvalQuery) -> SearchRun:
        return searcher.search(q.query, jasc=None, top_k=top_k, rerank=False,
                               channels=(channel,))
    return fn


def load_frequency_baseline(con: sqlite3.Connection, top_n: int = 20,
                            split: str = "train") -> dict[str, list[str]]:
    """Stratified top-N task frequency per ATA chapter. Prefers a precomputed
    ``baseline_freq`` table (Phase 0.8) and falls back to computing it from the
    train split. Key '' holds the ungrouped fallback list."""
    table = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='baseline_freq'"
    ).fetchone()

    rows: list[tuple[str | None, str]] = []
    if table:
        cols = [c[1].lower() for c in con.execute("PRAGMA table_info(baseline_freq)")]
        chapter_col = next((c for c in ("ata_chapter", "chapter", "jasc", "ata_ref")
                            if c in cols), None)
        task_col = next((c for c in ("task_number", "task", "tasknum") if c in cols), None)
        order_col = next((c for c in ("n", "cnt", "count", "freq", "frequency")
                          if c in cols), None)
        rank_col = next((c for c in ("rank", "rnk", "position") if c in cols), None)
        if chapter_col and task_col:
            order = (f"ORDER BY {chapter_col}, {order_col} DESC" if order_col
                     else f"ORDER BY {chapter_col}, {rank_col}" if rank_col
                     else f"ORDER BY {chapter_col}")
            rows = list(con.execute(
                f"SELECT {chapter_col}, {task_col} FROM baseline_freq {order}"))

    if not rows:
        rows = list(con.execute(
            "SELECT d.ata_ref, ls.task_number FROM label_silver ls"
            " JOIN defect d ON d.id = ls.defect_id"
            " WHERE ls.split = ? GROUP BY d.ata_ref, ls.task_number"
            " ORDER BY d.ata_ref, COUNT(*) DESC", (split,)))

    by_chapter: dict[str, list[str]] = {}
    for chapter, task_number in rows:
        if not task_number:
            continue
        key = (chapter or "").strip()[:2]
        bucket = by_chapter.setdefault(key, [])
        if task_number not in bucket and len(bucket) < top_n:
            bucket.append(task_number)

    overall: list[str] = []
    for bucket in by_chapter.values():
        for task_number in bucket:
            if task_number not in overall and len(overall) < top_n:
                overall.append(task_number)
    by_chapter.setdefault("", overall)
    return by_chapter


def frequency_fn(con: sqlite3.Connection, top_n: int = 20,
                 split: str = "train") -> SearchFn:
    table = load_frequency_baseline(con, top_n=top_n, split=split)
    ids = {tn: tid for tn, tid in con.execute(
        "SELECT task_number, id FROM task")}

    def fn(q: EvalQuery) -> SearchRun:
        chapter = (q.jasc or "").strip()[:2]
        picks = table.get(chapter) or table.get("", [])
        results = [
            SearchResult(
                kind="task", id=ids.get(tn), score=1.0 - i / max(len(picks), 1),
                task_number=tn, ata_chapter=chapter or None,
                provenance={"channels": "frequency_baseline",
                            "ata_chapter": chapter or None, "rank": i},
            )
            for i, tn in enumerate(picks)
        ]
        return SearchRun(query=q.query, ranked=results, results=results,
                         weights={"frequency": 1.0}, exact_query=False)
    return fn


# ── the whole sweep ──────────────────────────────────────────────────────
def run_all(
    con: sqlite3.Connection,
    searcher: Searcher,
    queries: Sequence[EvalQuery] | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    split: str = "test",
    limit: int | None = None,
    include_rerank: bool = True,
    top_k: int = 50,
) -> dict:
    if queries is None:
        queries = load_eval_queries(con, split=split, limit=limit)

    runs = [
        evaluate(con, frequency_fn(con), "baseline: top-20 frequency/ATA",
                 queries, threshold),
        evaluate(con, channel_fn(searcher, "fts", top_k), "baseline: FTS5 alone",
                 queries, threshold),
        evaluate(con, channel_fn(searcher, "dense", top_k), "baseline: vector alone",
                 queries, threshold),
        evaluate(con, hybrid_fn(searcher, rerank=False, top_k=top_k),
                 "hybrid, no rerank", queries, threshold),
    ]
    rerank_stats = None
    if include_rerank and searcher.reranker is not None:
        label = getattr(searcher.reranker, "name", "reranker")
        runs.append(evaluate(con, hybrid_fn(searcher, rerank=True, top_k=top_k),
                             f"hybrid + {label} rerank", queries, threshold))
        # a reranker that fell back on every query produces numbers identical to
        # the un-reranked run — report the count so that is never mistaken for
        # "the reranker made no difference"
        stats = getattr(searcher.reranker, "stats", None)
        if stats is not None:
            rerank_stats = dict(stats)

    return {
        "index_version": searcher.index_version,
        "model": searcher.embedder.model_name,
        "split": split,
        "n_queries": len(queries),
        "threshold": threshold,
        "labels": "silver — measures agreement with cited tasks, not correctness",
        "reranker_stats": rerank_stats,
        "runs": runs,
    }


# ── output ───────────────────────────────────────────────────────────────
def format_table(report: dict) -> str:
    k_hit = report["runs"][0]["k_hit"] if report["runs"] else 5
    k_rec = report["runs"][0]["k_recall"] if report["runs"] else 50
    lines = [
        f"index_version : {report['index_version']}   model: {report['model']}",
        f"split         : {report['split']}   queries: {report['n_queries']}   "
        f"abstain threshold: {report['threshold']}",
        f"labels        : {report['labels']}",
    ]
    header = (f"{'run':<34}{'stage1':>8}{'R@'+str(k_rec):>8}"
              f"{'NDCG@'+str(k_hit):>9}{'Hit@'+str(k_hit):>8}{'top1':>8}"
              f"{'abstain':>9}{'conf-wrong':>12}")
    for mode in ("strict", "relaxed"):
        lines += ["", f"── {mode} match "
                      f"{'(exact task number)' if mode == 'strict' else '(chapter-section-subject + function code)'} "
                      + "─" * 12, header, "-" * len(header)]
        for run in report["runs"]:
            m = run[mode]
            cw = m["confident_wrong"]
            lines.append(
                f"{run['name']:<34}"
                f"{m['stage1_recall']:>8.3f}"
                f"{m[f'recall_at_{k_rec}']:>8.3f}"
                f"{m[f'ndcg_at_{k_hit}']:>9.3f}"
                f"{m[f'hit_at_{k_hit}']:>8.3f}"
                f"{m['top1_accuracy']:>8.3f}"
                f"{run['abstention_rate']:>9.3f}"
                f"{cw['rate_above_threshold']:>12.3f}")
    lines += ["", "conf-wrong = share of ALL queries whose top-1 is wrong yet "
                  "scores at or above the abstention threshold."]
    stats = report.get("reranker_stats")
    if stats:
        calls, fell_back = stats.get("calls", 0), stats.get("fallbacks", 0)
        lines.append(f"reranker   : {calls} calls, {fell_back} fell back to dense order")
        if calls and fell_back == calls:
            lines.append("WARNING    : the reranker fell back on EVERY query — its "
                         "row above is the un-reranked run, not a rerank result.")
    return "\n".join(lines)


def write_json(report: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path
