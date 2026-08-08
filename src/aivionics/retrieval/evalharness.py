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

**Coverage is reported separately from quality, and both are mandatory.** The
product covers the whole ATA range, but the corpus does not: most silver labels
point at a chapter with no readable AMM. A single pooled number would therefore
mostly measure the corpus hole, while a number computed only over covered
chapters would read far better than the product actually performs. So every run
reports three pools — ALL pairs (the headline), SERVABLE chapters only, and
UNSERVABLE chapters only — plus a per-chapter breakdown. The unservable pool
should sit at ~0 by construction; if it does not, something is matching a task
that is not in the corpus and that is a finding, not a win.

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

from ..parsers.ata import chapter_name
from .search import SearchResult, SearchRun, Searcher

DEFAULT_THRESHOLD = 0.35


@dataclass(frozen=True)
class EvalQuery:
    defect_id: int
    query: str
    jasc: str | None
    gold: tuple[str, ...]

    @property
    def gold_chapters(self) -> tuple[str, ...]:
        """Chapters of the labelled tasks — where the answer would have to live.
        Deliberately not the JASC hint: the hint is what the reporter typed and
        is miscoded at chapter boundaries, whereas coverage is a fact about the
        corpus and the label."""
        seen = []
        for task_number in self.gold:
            ch = chapter_of(task_number)
            if ch and ch not in seen:
                seen.append(ch)
        return tuple(seen)


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


# ── corpus coverage ──────────────────────────────────────────────────────
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def chapter_of(task_number: str | None) -> str | None:
    """ATA chapter from a task number, tolerating the engine-prefix form
    (G73-00-00-810-A73) and the six-group form (71-00-00-800-801-G00)."""
    if not task_number:
        return None
    head = task_number.strip().upper().split("-")[0].lstrip(_LETTERS)
    return head[:2] if head[:2].isdigit() else None


def servable_chapters(con: sqlite3.Connection) -> set[str]:
    """Chapters holding at least one task row — AMM body or FIM catalogue alike.

    A chapter outside this set cannot be answered at all: nothing is indexed to
    return, so its recall is zero *by construction*, not by retrieval failure.
    Pooling those pairs into a headline number measures the corpus hole rather
    than the engine, which is why the pools below are reported separately.
    """
    return {row[0] for row in con.execute(
        "SELECT DISTINCT ata_chapter FROM task WHERE ata_chapter IS NOT NULL")
        if row[0]}


# ── scoring one run ──────────────────────────────────────────────────────
@dataclass
class QueryOutcome:
    """One query's result kept per-query, so the aggregate can be re-sliced by
    ATA chapter and by coverage without re-running any search."""
    defect_id: int
    chapter: str | None          # primary gold chapter — where the answer lives
    chapters: tuple[str, ...]
    servable: bool               # any gold chapter has task rows in the corpus
    top1_score: float
    modes: dict


def _mode_metrics(run: SearchRun, q: EvalQuery, key_fn, k_hit: int, k_recall: int) -> dict:
    gold = {key_fn(t) for t in q.gold} - {None}
    stage1 = [key_fn(r.task_number) for r in run.ranked]
    final = [key_fn(r.task_number) for r in run.results]

    # Each gold item counts once, at its best rank. Under the relaxed key,
    # sibling task numbers (-801 and -802) collapse to the same key, so without
    # this a single gold item scores twice and NDCG exceeds 1.
    hits, credited = [], set()
    for key in final[:k_hit]:
        is_new_hit = key in gold and key not in credited
        if is_new_hit:
            credited.add(key)
        hits.append(is_new_hit)

    return {
        "stage1_hit": bool(gold & set(stage1)),
        "recall_at_k": bool(gold & set(stage1[:k_recall])),
        "hit_at_k": any(hits),
        "ndcg_at_k": ndcg_at_k(hits, len(gold), k_hit),
        "top1_correct": bool(final and final[0] in gold),
    }


def _aggregate(outcomes: Sequence[QueryOutcome], threshold: float,
               k_hit: int, k_recall: int) -> dict:
    """The metric block, over whatever subset of outcomes it is handed.
    Identical arithmetic for the headline, a chapter and a pool alike."""
    n = len(outcomes)
    top1_scores = [o.top1_score for o in outcomes]
    mean = lambda xs: float(np.mean(xs)) if xs else 0.0  # noqa: E731

    block = {
        "n_queries": n,
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
        vals = [o.modes[mode] for o in outcomes]
        ws = [o.top1_score for o in outcomes if not o.modes[mode]["top1_correct"]]
        block[mode] = {
            "stage1_recall": mean([v["stage1_hit"] for v in vals]),
            f"recall_at_{k_recall}": mean([v["recall_at_k"] for v in vals]),
            f"hit_at_{k_hit}": mean([v["hit_at_k"] for v in vals]),
            f"ndcg_at_{k_hit}": mean([v["ndcg_at_k"] for v in vals]),
            "top1_accuracy": mean([v["top1_correct"] for v in vals]),
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
    return block


POOL_LABELS = {
    "all": "ALL pairs — THE HEADLINE. Quote this number.",
    "servable": "SERVABLE chapters only — NOT the headline. Excludes every pair "
                "whose chapter has no corpus, so it answers 'how well does "
                "retrieval work where a corpus exists', nothing broader.",
    "unservable": "UNSERVABLE chapters only — expected ~0 by construction "
                  "(nothing indexed to retrieve). Anything above 0 is a finding.",
}


def _pools(outcomes: Sequence[QueryOutcome], threshold: float,
           k_hit: int, k_recall: int) -> list[dict]:
    subsets = {
        "all": list(outcomes),
        "servable": [o for o in outcomes if o.servable],
        "unservable": [o for o in outcomes if not o.servable],
    }
    return [
        {"pool": key, "label": POOL_LABELS[key],
         **_aggregate(subset, threshold, k_hit, k_recall)}
        for key, subset in subsets.items()
    ]


def _by_chapter(outcomes: Sequence[QueryOutcome], servable: set[str],
                threshold: float, k_hit: int, k_recall: int) -> list[dict]:
    groups: dict[str, list[QueryOutcome]] = {}
    for o in outcomes:
        groups.setdefault(o.chapter or "??", []).append(o)
    rows = [
        {"ata_chapter": chapter,
         "chapter_name": chapter_name(chapter) if chapter.isdigit() else "unknown",
         "servable": chapter in servable,
         **_aggregate(items, threshold, k_hit, k_recall)}
        for chapter, items in groups.items()
    ]
    rows.sort(key=lambda r: (-r["n_queries"], r["ata_chapter"]))
    return rows


def evaluate(
    con_or_none,
    search_fn: SearchFn,
    name: str,
    queries: Sequence[EvalQuery],
    threshold: float = DEFAULT_THRESHOLD,
    k_hit: int = 5,
    k_recall: int = 50,
    servable: set[str] | None = None,
) -> dict:
    """Run ``search_fn`` over every query and aggregate. ``con_or_none`` is
    accepted so the signature matches the 'connection + search function' contract
    even when the search function already closes over its own connection; it is
    also where the servable-chapter set is read from when not supplied."""
    if servable is None and con_or_none is not None:
        servable = servable_chapters(con_or_none)
    servable = servable or set()

    outcomes: list[QueryOutcome] = []
    for q in queries:
        run = search_fn(q)
        chapters = q.gold_chapters
        outcomes.append(QueryOutcome(
            defect_id=q.defect_id,
            chapter=chapters[0] if chapters else None,
            chapters=chapters,
            servable=any(c in servable for c in chapters),
            top1_score=float(run.results[0].score) if run.results else 0.0,
            modes={mode: _mode_metrics(run, q, key_fn, k_hit, k_recall)
                   for mode, key_fn in KEYS.items()},
        ))

    return {
        "name": name,
        "threshold": threshold,
        "k_hit": k_hit,
        "k_recall": k_recall,
        **_aggregate(outcomes, threshold, k_hit, k_recall),
        "pools": _pools(outcomes, threshold, k_hit, k_recall),
        "by_chapter": _by_chapter(outcomes, servable, threshold, k_hit, k_recall),
    }


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

    servable = servable_chapters(con)
    ev = lambda fn, name: evaluate(  # noqa: E731
        con, fn, name, queries, threshold, servable=servable)

    runs = [
        ev(frequency_fn(con), "baseline: top-20 frequency/ATA"),
        ev(channel_fn(searcher, "fts", top_k), "baseline: FTS5 alone"),
        ev(channel_fn(searcher, "dense", top_k), "baseline: vector alone"),
        ev(hybrid_fn(searcher, rerank=False, top_k=top_k), "hybrid, no rerank"),
    ]
    rerank_stats = None
    if include_rerank and searcher.reranker is not None:
        label = getattr(searcher.reranker, "name", "reranker")
        runs.append(ev(hybrid_fn(searcher, rerank=True, top_k=top_k),
                       f"hybrid + {label} rerank"))
        # a reranker that fell back on every query produces numbers identical to
        # the un-reranked run — report the count so that is never mistaken for
        # "the reranker made no difference"
        stats = getattr(searcher.reranker, "stats", None)
        if stats is not None:
            rerank_stats = dict(stats)

    pool_n = {p["pool"]: p["n_queries"] for p in runs[0]["pools"]} if runs else {}
    n = len(queries)
    return {
        "index_version": searcher.index_version,
        "model": searcher.embedder.model_name,
        "split": split,
        "n_queries": n,
        "threshold": threshold,
        "labels": "silver — measures agreement with cited tasks, not correctness",
        "coverage": {
            "servable_chapters": sorted(servable),
            "n_servable_chapters": len(servable),
            "n_pairs_servable": pool_n.get("servable", 0),
            "n_pairs_unservable": pool_n.get("unservable", 0),
            "pct_pairs_servable": (100.0 * pool_n.get("servable", 0) / n) if n else 0.0,
            "note": "a pair is servable when the chapter of its labelled task "
                    "holds at least one task row; unservable pairs are "
                    "unanswerable by construction, not by retrieval failure",
        },
        "reranker_stats": rerank_stats,
        "runs": runs,
    }


# ── output ───────────────────────────────────────────────────────────────
def _pools_section(report: dict, k_hit: int, k_rec: int) -> list[str]:
    """Every run against all three coverage pools. The headline is ALL pairs;
    the servable row exists so the coverage hole can be separated from retrieval
    quality, never so it can be quoted in the hole's place."""
    if not report["runs"] or "pools" not in report["runs"][0]:
        return []
    header = (f"{'run':<34}{'pool':<12}{'n':>6}"
              f"{'R@'+str(k_rec):>8}{'Hit@'+str(k_hit):>8}{'NDCG@'+str(k_hit):>9}"
              f"{'R@'+str(k_rec):>8}{'Hit@'+str(k_hit):>8}{'NDCG@'+str(k_hit):>9}")
    lines = ["", "── coverage pools " + "─" * 60,
             f"{'':<52}{'——— strict ———':>25}{'——— relaxed ———':>25}",
             header, "-" * len(header)]
    for run in report["runs"]:
        for pool in run["pools"]:
            s, r = pool["strict"], pool["relaxed"]
            lines.append(
                f"{run['name']:<34}{pool['pool']:<12}{pool['n_queries']:>6}"
                f"{s[f'recall_at_{k_rec}']:>8.3f}{s[f'hit_at_{k_hit}']:>8.3f}"
                f"{s[f'ndcg_at_{k_hit}']:>9.3f}"
                f"{r[f'recall_at_{k_rec}']:>8.3f}{r[f'hit_at_{k_hit}']:>8.3f}"
                f"{r[f'ndcg_at_{k_hit}']:>9.3f}")
        lines.append("")
    for pool in report["runs"][0]["pools"]:
        lines.append(f"  {pool['pool']:<12} {pool['label']}")
    return lines


def _chapter_section(report: dict, k_hit: int, k_rec: int) -> list[str]:
    """Per-chapter breakdown for the final (best-configured) run. Every run's
    breakdown is in the JSON; printing all of them would bury the table."""
    runs = [r for r in report["runs"] if r.get("by_chapter")]
    if not runs:
        return []
    run = runs[-1]
    header = (f"{'ch':<4}{'chapter':<34}{'n':>6}{'srv':>5}"
              f"{'stage1':>8}{'R@'+str(k_rec):>8}{'Hit@'+str(k_hit):>8}"
              f"{'NDCG@'+str(k_hit):>9}"
              f"{'stage1':>8}{'R@'+str(k_rec):>8}{'Hit@'+str(k_hit):>8}"
              f"{'NDCG@'+str(k_hit):>9}")
    lines = ["", f"── per-ATA-chapter · {run['name']} " + "─" * 30,
             f"{'':<57}{'———————— strict ————————':>33}"
             f"{'——————— relaxed ————————':>33}",
             header, "-" * len(header)]
    for row in run["by_chapter"]:
        s, r = row["strict"], row["relaxed"]
        lines.append(
            f"{row['ata_chapter']:<4}{row['chapter_name'][:33]:<34}"
            f"{row['n_queries']:>6}{('yes' if row['servable'] else 'NO'):>5}"
            f"{s['stage1_recall']:>8.3f}{s[f'recall_at_{k_rec}']:>8.3f}"
            f"{s[f'hit_at_{k_hit}']:>8.3f}{s[f'ndcg_at_{k_hit}']:>9.3f}"
            f"{r['stage1_recall']:>8.3f}{r[f'recall_at_{k_rec}']:>8.3f}"
            f"{r[f'hit_at_{k_hit}']:>8.3f}{r[f'ndcg_at_{k_hit}']:>9.3f}")
    lines.append("")
    lines.append("srv = the chapter holds at least one task row. srv=NO chapters "
                 "score 0 because nothing is indexed,")
    lines.append("      not because retrieval failed — do not read them as a "
                 "quality signal.")
    return lines


def format_table(report: dict) -> str:
    k_hit = report["runs"][0]["k_hit"] if report["runs"] else 5
    k_rec = report["runs"][0]["k_recall"] if report["runs"] else 50
    lines = [
        f"index_version : {report['index_version']}   model: {report['model']}",
        f"split         : {report['split']}   queries: {report['n_queries']}   "
        f"abstain threshold: {report['threshold']}",
        f"labels        : {report['labels']}",
    ]
    cov = report.get("coverage")
    if cov:
        n_un = cov["n_pairs_unservable"]
        lines.append(
            f"corpus        : {cov['n_servable_chapters']} chapters have task rows; "
            f"{cov['n_pairs_servable']}/{report['n_queries']} pairs "
            f"({cov['pct_pairs_servable']:.1f}%) are servable, "
            f"{n_un} {'pair has' if n_un == 1 else 'pairs have'} no corpus at all")
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
    lines += _pools_section(report, k_hit, k_rec)
    lines += _chapter_section(report, k_hit, k_rec)
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
