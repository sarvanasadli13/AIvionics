"""Phase 4 — the reranker comparison, on one shared stage-1.

Gate 2 left one question open: the cross-encoder made ranking worse, and the
LLM reranker was implemented but never run against the real endpoint. This
answers it, and it answers a second question Gate 2 could not — *how much
reordering could possibly help*, which is what decides whether the loss is in
the ranker or in the candidate generator.

Stage-1 is executed **once per query** and the candidate list is cached. Every
arm then reorders that same list. Running retrieval separately per arm would
make the arms differ by three independent stage-1 executions as well as by the
reranker, and the reranker's effect could not be read off the difference.

    python scripts/phase4_rerank.py --arms none,flashrank --sample 637
    python scripts/phase4_rerank.py --arms none,llm --sample 30 --rerank-window 10

The LLM arm talks to NVIDIA NIM. The key is read out of ~/.claude.json at run
time and is never written to a file, an argument or the console (security rule:
secrets are not logged). That endpoint is slow and rate-limits, so the sample
size is deliberately a parameter and whatever was actually run is printed with
the results — a small measured sample is worth more here than a large claimed
one.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from aivionics import config                                      # noqa: E402
from aivionics.retrieval import evalharness                       # noqa: E402
from aivionics.retrieval.embedder import FakeEmbedder, FastEmbedder  # noqa: E402
from aivionics.retrieval.rerank import (FlashRankReranker,        # noqa: E402
                                        LLMReranker, NullReranker)
from aivionics.retrieval.search import SearchRun, Searcher        # noqa: E402


def nim_key() -> str:
    """The NIM key from the MCP configuration, or "".

    Read here rather than passed in, so the key never appears in a command
    line, a log file or this repository.
    """
    path = Path(os.path.expanduser("~/.claude.json"))
    if not path.exists():
        return ""
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return ""
    servers = blob.get("mcpServers") or {}
    env = (servers.get("nvidia-nim") or {}).get("env") or {}
    return str(env.get("NVIDIA_NIM_API_KEY") or "")


class TimedLLM:
    """Wraps the model callable to record latency and retry a rate limit.

    A 429 is an availability fact about a hosted endpoint, not evidence about
    the model's ranking ability, so it is retried once with backoff and counted
    separately. Anything still failing after that is left to the reranker's own
    fail-safe, which keeps the dense order.
    """

    def __init__(self, inner, retries: int = 1, backoff: float = 5.0) -> None:
        self.inner = inner
        self.retries = retries
        self.backoff = backoff
        self.latencies: list[float] = []
        self.rate_limited = 0
        self.errors: list[str] = []
        self.truncated: list[bool] = []
        self.completion_tokens: list[int] = []

    def __call__(self, prompt: str) -> str:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            start = time.monotonic()
            try:
                out = self.inner(prompt)
                self.latencies.append(time.monotonic() - start)
                return out
            except Exception as exc:                # noqa: BLE001 — recorded, not hidden
                self.latencies.append(time.monotonic() - start)
                last = exc
                text = str(exc)
                if "429" in text or "rate" in text.lower():
                    self.rate_limited += 1
                    if attempt < self.retries:
                        time.sleep(self.backoff * (attempt + 1))
                        continue
                break
        self.errors.append(str(last)[:200])
        raise last if last else RuntimeError("llm call failed")


def build_llm_reranker(model: str, max_tokens: int | None,
                       timeout: float, top_m: int | None):
    import dataclasses

    from aivionics.llm.client import LLMConfig, build_service

    key = nim_key()
    if not key:
        raise SystemExit("no NVIDIA_NIM_API_KEY in ~/.claude.json — cannot run "
                         "the llm arm")
    config = LLMConfig.for_nim(key, enabled=True, model=model)
    if timeout:
        # `for_nim` fixes the timeout at 120s. Measured on 2026-08-22, two of
        # eight rerank calls hit exactly that and raised, which the fail-safe
        # then counts as an endpoint error — indistinguishable in the totals
        # from a model that answered badly. Making it settable lets the run
        # separate "too slow for the product" from "wrong answer".
        config = dataclasses.replace(config, timeout=timeout)
    service = build_service(config)

    # The raw completion is passed through untouched. An earlier revision ran
    # `strip_reasoning` here as well as inside the parser, and that is actively
    # harmful: the parser needs the *whole* response to tell an answer from a
    # draft the model abandoned mid-thought (`rerank._is_final_answer`), and
    # pre-stripping throws away the trailing text that distinguishes them.
    def call(prompt: str) -> str:
        gen = service.generate(prompt, max_tokens=max_tokens)
        # Truncation is recorded because it is the cause behind most fallbacks
        # and is invisible in the fallback count alone: a reply cut off inside
        # the chain of thought and a reply that argued itself into a bad answer
        # both arrive as "unparseable", and only the first is fixable with a
        # bigger budget.
        timed.truncated.append(bool(getattr(gen.usage, "truncated", False)))
        timed.completion_tokens.append(int(getattr(gen.usage, "completion_tokens", 0)))
        return gen.text

    timed = TimedLLM(call)
    return LLMReranker(timed, top_m=top_m), timed, service


def reorder(run: SearchRun, reranker, window: int, top_k: int) -> SearchRun:
    """Apply one reranker to a cached stage-1 list, exactly as `Searcher.search`
    would: the window is reordered and the tail is left where it was."""
    ranked = run.ranked
    if reranker is None or len(ranked) < 2:
        results = ranked[:top_k]
    else:
        head = reranker.rerank(run.query, ranked[:window])
        results = (list(head) + ranked[window:])[:top_k]
    return SearchRun(query=run.query, ranked=ranked, results=results,
                     weights=run.weights, exact_query=run.exact_query,
                     confidence=run.confidence, reranked=reranker is not None)


def mean(xs) -> float:
    return float(statistics.fmean(xs)) if xs else 0.0


def aggregate(scores: list[dict]) -> dict:
    out = {}
    for mode in ("strict", "relaxed"):
        vals = [s[mode] for s in scores]
        out[mode] = {
            "stage1_recall": mean([v["stage1_hit"] for v in vals]),
            "recall_at_50": mean([v["recall_at_k"] for v in vals]),
            "hit_at_5": mean([v["hit_at_k"] for v in vals]),
            "ndcg_at_5": mean([v["ndcg_at_k"] for v in vals]),
            "top1_accuracy": mean([v["top1_correct"] for v in vals]),
        }
    return out


def ceiling_curve(runs, queries, depths=(1, 3, 5, 10, 20, 50, 100, 200)) -> dict:
    """Best Hit@5 any reordering of the top-*d* window could reach, per depth.

    A reranker over a window of *d* can only surface a gold item already inside
    that window, so this is the ceiling for a perfect reranker and the number a
    real one has to be judged against.
    """
    out = {}
    for mode, key_fn in evalharness.KEYS.items():
        ranks = [evalharness.gold_rank(r, q, key_fn) for r, q in zip(runs, queries)]
        n = len(ranks)
        out[mode] = {
            "found_in_stage1": mean([r is not None for r in ranks]),
            "median_gold_rank": (
                statistics.median([r for r in ranks if r is not None])
                if any(r is not None for r in ranks) else None),
            "ceiling_hit_at_5_by_window": {
                str(d): (sum(1 for r in ranks if r is not None and r < d) / n)
                if n else 0.0 for d in depths},
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--split", default="test")
    ap.add_argument("--sample", type=int, default=30,
                    help="queries to evaluate; the LLM endpoint is slow, so a "
                         "small honest sample beats a large slow one")
    ap.add_argument("--seed", type=int, default=20260822,
                    help="every arm sees the same queries in the same order")
    ap.add_argument("--arms", default="none,flashrank",
                    help="comma-separated: none, flashrank, llm")
    ap.add_argument("--rerank-window", type=int, default=50,
                    help="candidates handed to the reranker (Searcher's "
                         "rerank_k). 50 is what ships")
    ap.add_argument("--top-k", type=int, default=50)
    ap.add_argument("--aircraft-like", default="%737%")
    ap.add_argument("--all-aircraft", action="store_true",
                    help="drop the airframe filter; counts cross-type task "
                         "number collisions as answerable")
    ap.add_argument("--model", default="nvidia/nemotron-3.5-lightning-30b-a3b")
    ap.add_argument("--max-tokens", type=int, default=None,
                    help="token budget for the llm arm; must leave room for "
                         "the chain of thought (llm.service.budget_for)")
    ap.add_argument("--top-m", type=int, default=None,
                    help="ask the llm for the best m indices instead of a full "
                         "permutation. The answer is m indices however wide "
                         "the window, so the window can be opened to where the "
                         "ceiling curve says the headroom is")
    ap.add_argument("--timeout", type=float, default=0.0,
                    help="override the 120s NIM client timeout for the llm arm")
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    con = evalharness.connect_read_only(args.db)
    searcher = Searcher(con, FakeEmbedder() if args.fake else FastEmbedder())
    ids, _ = searcher.vectors("task")
    if ids.size == 0:
        print("no task vectors — run scripts/phase2_index.py first")
        return 1

    queries = evalharness.load_eval_queries(
        con, split=args.split, answerable_only=True,
        aircraft_like=None if args.all_aircraft else args.aircraft_like)
    pool = len(queries)
    if args.sample and args.sample < pool:
        random.Random(args.seed).shuffle(queries)
        queries = queries[:args.sample]
    print(f"pool {pool:,} → sample {len(queries):,} · split {args.split} · "
          f"seed {args.seed} · window {args.rerank_window} · "
          f"vectors {ids.size:,}")

    # ── stage 1, once ────────────────────────────────────────────────────
    t0 = time.time()
    runs = [searcher.search(q.query, jasc=q.jasc, top_k=args.top_k, rerank=False)
            for q in queries]
    stage1_s = time.time() - t0
    print(f"stage-1: {stage1_s:.1f}s for {len(runs)} queries "
          f"({1000 * stage1_s / max(len(runs), 1):.0f} ms/query)")

    report = {
        "date": time.strftime("%Y-%m-%d"),
        "index_version": searcher.index_version,
        "model": searcher.embedder.model_name,
        "split": args.split,
        "pool": pool,
        "n_queries": len(queries),
        "seed": args.seed,
        "rerank_window": args.rerank_window,
        "top_k": args.top_k,
        "aircraft_like": None if args.all_aircraft else args.aircraft_like,
        "labels": "silver — agreement with the cited task, not correctness",
        "stage1_seconds": round(stage1_s, 1),
        "ceiling": ceiling_curve(runs, queries),
        "arms": [],
    }

    for arm in arms:
        reranker, timed, service = None, None, None
        if arm == "flashrank":
            reranker = FlashRankReranker()
            reranker.warmup()             # a config error must fail now, not mid-sweep
        elif arm == "llm":
            reranker, timed, service = build_llm_reranker(
                args.model, args.max_tokens, args.timeout, args.top_m)
            ident = service.identity()
            contract = (f"best-{args.top_m} selection" if args.top_m
                        else "full permutation")
            print(f"llm arm: {ident.describe()} · {contract} · window "
                  f"{args.rerank_window} · budget {args.max_tokens or 'default'}")
            report["llm_identity"] = ident.describe()
            report["llm_contract"] = contract
            report["llm_max_tokens"] = args.max_tokens
        elif arm != "none":
            print(f"unknown arm {arm!r}")
            return 2

        t0 = time.time()
        scores = []
        for i, (run, q) in enumerate(zip(runs, queries), start=1):
            out = reorder(run, reranker, args.rerank_window, args.top_k)
            scores.append(evalharness.score_run(out, q))
            if arm == "llm" and i % 5 == 0:
                print(f"  {i}/{len(queries)}", flush=True)
        elapsed = time.time() - t0

        entry = {"arm": arm, "seconds": round(elapsed, 1), **aggregate(scores)}
        stats = getattr(reranker, "stats", None)
        if stats:
            calls = stats.get("calls", 0)
            entry["reranker_stats"] = dict(stats)
            entry["fallback_rate"] = (stats.get("fallbacks", 0) / calls) if calls else 0.0
        if timed is not None:
            lat = timed.latencies
            entry["latency_s"] = {
                "n": len(lat),
                "mean": round(mean(lat), 2),
                "p50": round(statistics.median(lat), 2) if lat else 0.0,
                "max": round(max(lat), 2) if lat else 0.0,
            }
            entry["rate_limited"] = timed.rate_limited
            entry["errors"] = timed.errors[:5]
            entry["truncated_replies"] = sum(timed.truncated)
            entry["n_replies"] = len(timed.truncated)
            entry["completion_tokens_mean"] = round(mean(timed.completion_tokens), 0)
        report["arms"].append(entry)
        print(f"  {arm}: {elapsed:.1f}s"
              + (f" · fallback {entry['fallback_rate']:.1%}"
                 if "fallback_rate" in entry else ""))

    print()
    print(format_report(report))
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwritten: {path}")
    con.close()
    return 0


def format_report(report: dict) -> str:
    lines = [
        f"index {report['index_version']} · {report['model']} · "
        f"{report['split']} split · n={report['n_queries']} of {report['pool']:,} "
        f"· seed {report['seed']} · window {report['rerank_window']}",
        f"labels: {report['labels']}",
    ]
    header = (f"{'arm':<12}{'mode':<9}{'stage1':>8}{'R@50':>8}{'NDCG@5':>9}"
              f"{'Hit@5':>8}{'top1':>8}{'fallback':>10}")
    lines += ["", header, "-" * len(header)]
    for arm in report["arms"]:
        fb = (f"{arm['fallback_rate']:.1%}" if "fallback_rate" in arm else "—")
        for mode in ("strict", "relaxed"):
            m = arm[mode]
            lines.append(
                f"{arm['arm']:<12}{mode:<9}{m['stage1_recall']:>8.3f}"
                f"{m['recall_at_50']:>8.3f}{m['ndcg_at_5']:>9.3f}"
                f"{m['hit_at_5']:>8.3f}{m['top1_accuracy']:>8.3f}{fb:>10}")
    lines += ["", "── ceiling: best Hit@5 any reordering of a top-d window could reach ──"]
    for mode, block in report["ceiling"].items():
        curve = block["ceiling_hit_at_5_by_window"]
        lines.append(f"  {mode:<8} gold found in stage-1 "
                     f"{block['found_in_stage1']:.3f} · median gold rank "
                     f"{block['median_gold_rank']}")
        lines.append("    " + "  ".join(f"d={d}:{v:.3f}" for d, v in curve.items()))
    for arm in report["arms"]:
        if "latency_s" in arm:
            lat = arm["latency_s"]
            lines.append(f"\n  {arm['arm']} latency: mean {lat['mean']}s · "
                         f"p50 {lat['p50']}s · max {lat['max']}s over {lat['n']} "
                         f"calls · rate-limited {arm.get('rate_limited', 0)}")
            if arm.get("n_replies"):
                lines.append(
                    f"  {arm['arm']} replies: {arm['truncated_replies']}/"
                    f"{arm['n_replies']} ran out of tokens · mean "
                    f"{arm['completion_tokens_mean']:.0f} completion tokens")
            if arm.get("errors"):
                lines.append(f"  {arm['arm']} first errors: {arm['errors']}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
