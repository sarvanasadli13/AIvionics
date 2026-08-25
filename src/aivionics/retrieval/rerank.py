"""Rerankers (PLAN 2.4) — the cross-encoder and the LLM, measured against each other.

Both only ever **reorder** candidates. Neither may emit content: the LLM is given
task numbers, ATA hierarchy and titles, and is required to answer with a JSON
array of indices and nothing else. That keeps it inside standing rule 3 (the LLM
never touches procedural text) by construction rather than by promise.

The fail-safe is mandatory, not defensive politeness. Invalid JSON, prose, a
short list, a duplicated index or an out-of-range index all produce the same
outcome: **the dense order is returned unchanged**. A reranker that cannot be
trusted to permute is simply skipped.

Reranking reorders; it does not invent a score. ``SearchResult.score`` keeps the
hybrid retrieval score — a real computed number — and the reranker's own signal
is recorded in ``provenance`` (standing rule 4: no fabricated numerals).
"""
from __future__ import annotations

import copy
import json
import re
from typing import Callable, Protocol, Sequence

from ..llm.service import strip_reasoning
from .embedder import model_cache_dir
from .search import SearchResult

# PLAN 2.4a names ms-marco-MiniLM-L-6-v2. FlashRank 0.2.10 does not ship it —
# its model map holds TinyBERT-L-2-v2, MiniLM-L-12-v2, MultiBERT-L-12, rank-T5-flan
# and ce-esci-MiniLM-L12-v2, so the L-6 name 404s at download time. L-12-v2 is the
# same MiniLM cross-encoder family with more layers and is what we default to.
# ms-marco-TinyBERT-L-2-v2 is the model the Collins Aerospace offline-RAG prior
# art used (PLAN §11.2) and is a valid alternative here.
FLASHRANK_MODEL = "ms-marco-MiniLM-L-12-v2"

PROMPT_TEMPLATE = """You are ordering candidate aircraft maintenance tasks by how well each one \
matches a reported defect.

DEFECT REPORT:
{query}

CANDIDATES:
{candidates}

Reply with ONLY a JSON array of the candidate indices, most relevant first.
The array must contain every index from 0 to {last} exactly once.
No prose, no explanation, no code fences, no other keys.
For four candidates ranked third, first, third-from-last, second, the answer
would be the four indices 3, 0, 2 and 1 in square brackets.
"""
# The example above is deliberately spelled out rather than shown as a literal
# JSON array. A reasoning model quotes the prompt back to itself while it
# thinks, and when its answer is cut off by the token budget the only balanced
# array left in the response is the echoed example — which `strip_reasoning`
# then hands back as though it were the answer. Measured against Nemotron on
# 2026-08-22: a truncated reply yielded `[3, 0, 2, 1]`, and with exactly four
# candidates that is a valid permutation, so the fail-safe would have passed a
# fabricated ranking. Leaving no literal array in the prompt removes the
# collision at the source instead of trying to detect it afterwards.


SELECT_PROMPT_TEMPLATE = """You are choosing which candidate aircraft maintenance \
tasks best match a reported defect.

DEFECT REPORT:
{query}

CANDIDATES:
{candidates}

Reply with ONLY a JSON array of the {m} candidate indices you judge most relevant, \
best first.
Every value must be a different whole number between 0 and {last}.
No prose, no explanation, no code fences, no other keys.
"""
# Two measured facts about Nemotron 3.5 shape this second contract, and both
# are properties of the *answer length*, not of the model's judgement.
#
# A full permutation of a window of w costs w indices of output. The window is
# also the ceiling: a reranker can only surface a gold task that is already
# inside it, and `phase4_rerank.ceiling_curve` puts best-possible Hit@5 at
# 0.353 relaxed for w=10 against 0.465 for w=20 on the 637-pair pool. So the
# permutation contract is caught between a window too narrow to be worth much
# and an answer too long to survive the budget: measured on 2026-08-22, w=20
# ran the response out of tokens where w=10 did not.
#
# Selecting the best m from a window of w breaks that coupling. The answer is m
# indices however wide the window, so the window can be opened to where the
# headroom actually is without lengthening the reply. Since Hit@5 and NDCG@5
# only read the first five results, ranking the tail is work the metric never
# looks at.


class Reranker(Protocol):
    def rerank(self, query: str, candidates: Sequence[SearchResult]) -> list[SearchResult]:
        ...


def _annotate(result: SearchResult, **extra) -> SearchResult:
    """Copy so reordering never mutates the caller's stage-1 list."""
    out = copy.copy(result)
    out.provenance = dict(result.provenance)
    out.provenance.update(extra)
    return out


def candidate_lines(candidates: Sequence[SearchResult]) -> str:
    """Locator text only — number, hierarchy, title. Never a task body."""
    lines = []
    for i, c in enumerate(candidates):
        label = c.hierarchy or c.title or ""
        number = c.task_number or f"{c.kind}:{c.id}"
        lines.append(f"[{i}] {number} | {label}".strip())
    return "\n".join(lines)


class NullReranker:
    """Explicit no-op, so 'hybrid without rerank' is a configuration rather than
    a separate code path."""

    name = "none"

    def rerank(self, query: str, candidates: Sequence[SearchResult]) -> list[SearchResult]:
        return list(candidates)


def available_flashrank_models() -> list[str]:
    from flashrank.Config import model_file_map

    return sorted(model_file_map)


class FlashRankReranker:
    """FlashRank cross-encoder over the top-k locator text.

    The model is built lazily but **outside** the inference fail-safe. A missing
    or misnamed model is a configuration error and must raise: swallowing it
    would silently report un-reranked results as reranked, which is exactly the
    confident-and-wrong failure Gate 2 exists to measure. Only inference itself
    falls back. Call ``warmup()`` to force that failure up front.
    """

    name = "flashrank"

    def __init__(self, model_name: str = FLASHRANK_MODEL, cache_dir=None,
                 max_length: int = 256) -> None:
        self.model_name = model_name
        self.cache_dir = str(cache_dir or model_cache_dir())
        self.max_length = max_length
        self.stats = {"calls": 0, "fallbacks": 0}
        self._ranker = None

    @property
    def ranker(self):
        if self._ranker is None:
            from flashrank import Ranker

            try:
                known = available_flashrank_models()
            except ImportError:                     # layout changed — let Ranker speak
                known = None
            if known is not None and self.model_name not in known:
                raise ValueError(
                    f"FlashRank has no model {self.model_name!r}. Available: {known}"
                )
            self._ranker = Ranker(model_name=self.model_name,
                                  cache_dir=self.cache_dir,
                                  max_length=self.max_length)
        return self._ranker

    def warmup(self):
        """Force model download/load now, so a broken configuration fails before
        an evaluation sweep starts rather than degrading silently inside it."""
        return self.ranker

    def rerank(self, query: str, candidates: Sequence[SearchResult]) -> list[SearchResult]:
        candidates = list(candidates)
        if len(candidates) < 2:
            return candidates
        ranker = self.ranker            # configuration errors propagate, by design
        from flashrank import RerankRequest

        passages = [
            {"id": i,
             "text": f"{c.task_number or ''} {c.hierarchy or c.title or ''}".strip()}
            for i, c in enumerate(candidates)
        ]
        self.stats["calls"] += 1
        try:
            scored = ranker.rerank(RerankRequest(query=query, passages=passages))
        except Exception:
            self.stats["fallbacks"] += 1            # same fail-safe rule
            return candidates
        order = [(int(r["id"]), float(r.get("score", 0.0))) for r in scored]
        seen = {i for i, _ in order}
        order += [(i, 0.0) for i in range(len(candidates)) if i not in seen]
        return [
            _annotate(candidates[i], reranker=self.name, rerank_score=round(s, 6))
            for i, s in order
        ]


# Whatever may legitimately follow the answer: whitespace, a closing code
# fence, a full stop, a closing quote. Anything else means the model kept
# talking, and an array it kept talking past is not its answer.
_TRAILING_OK = re.compile(r"^[\s`.\"'”]*$")


def _is_final_answer(raw: str, block: str) -> bool:
    """True when `block` is the last thing the model actually said.

    `strip_reasoning` extracts the longest balanced array anywhere in the
    response, which is right for a finished reply and wrong for an unfinished
    one. A reasoning model drafts orderings while it thinks and then abandons
    them; when the token budget cuts it off mid-thought, the only array left in
    the text is a draft it had already rejected. Measured against Nemotron on
    2026-08-22: a reply that ran out of budget contained
    `So order: [0, 1, ..., 9] - wait, that's just the natural order`, and that
    draft parsed as a valid permutation. The fail-safe accepted a ranking the
    model never gave, and — worse for the evaluation — counted it as a success
    rather than a fallback, so `fallback_rate` under-reported the failures it
    exists to expose. Requiring the array to sit at the end of the response
    distinguishes an answer from an abandoned draft without needing the token
    accounting, which this layer does not receive.
    """
    idx = raw.rfind(block)
    if idx < 0:
        return True                 # reconstructed, not sliced — nothing to check
    return bool(_TRAILING_OK.match(raw[idx + len(block):]))


def parse_index_order(raw: str, n: int) -> list[int] | None:
    """Strict permutation parse. Returns None on anything that is not exactly a
    JSON array holding each index 0..n-1 once — which is the fail-safe trigger.

    The strictness that matters is the permutation check, not the position of
    the array in the response. A reasoning model puts its chain of thought
    first — Nemotron 3.5 does it on every call and no system prompt suppresses
    it (`llm.service`) — so requiring the answer to *begin* with `[` rejected
    every reply the real endpoint ever sent. Measured on 2026-08-22: with the
    preamble left in place the NIM reranker fell back on 100% of queries, which
    reads in the metrics as "the reranker made no difference" rather than as
    "the reranker never ran". The preamble is stripped first and the
    permutation is then checked exactly as before — plus `_is_final_answer`,
    which rejects a draft the model wrote and then talked past.
    """
    if raw is None:
        return None
    text = strip_reasoning(raw).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    if not (text.startswith("[") and text.endswith("]")):
        return None
    if not _is_final_answer(raw, text):
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list) or len(parsed) != n:
        return None
    order: list[int] = []
    for item in parsed:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        if item < 0 or item >= n:
            return None
        order.append(item)
    if len(set(order)) != n:
        return None
    return order


def parse_index_selection(raw: str, n: int, m: int) -> list[int] | None:
    """Strict top-*m* parse: exactly ``m`` distinct indices inside 0..n-1.

    Deliberately as unforgiving as `parse_index_order`. A selection that is
    short, over-long, duplicated or out of range is not a partial success to be
    patched up — a reranker whose output cannot be trusted is skipped, and the
    dense order stands.
    """
    if raw is None:
        return None
    text = strip_reasoning(raw).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    if not (text.startswith("[") and text.endswith("]")):
        return None
    if not _is_final_answer(raw, text):
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list) or len(parsed) != m or m > n:
        return None
    picked: list[int] = []
    for item in parsed:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        if item < 0 or item >= n:
            return None
        picked.append(item)
    if len(set(picked)) != m:
        return None
    return picked


class LLMReranker:
    """Takes any callable ``llm(prompt) -> str``. Injected, so nothing here
    depends on a particular client, and pytest never reaches a network.

    ``top_m`` switches the output contract from "order every candidate" to
    "name the best m". Both only ever permute the candidate list — the
    unselected candidates keep their dense order behind the selection, so no
    item is added, dropped or rescored either way.
    """

    name = "llm"

    def __init__(self, llm: Callable[[str], str],
                 prompt_template: str | None = None,
                 top_m: int | None = None) -> None:
        self.llm = llm
        self.top_m = top_m
        self.prompt_template = prompt_template or (
            SELECT_PROMPT_TEMPLATE if top_m else PROMPT_TEMPLATE)
        # `fallbacks` is the number the evaluation quotes; the two causes are
        # counted apart because they call for opposite responses. An endpoint
        # error is an availability problem and says nothing about the model's
        # ranking ability, whereas an unparseable answer means the model
        # answered and could not hold the output contract.
        self.stats = {"calls": 0, "fallbacks": 0,
                      "errors": 0, "unparseable": 0}

    def build_prompt(self, query: str, candidates: Sequence[SearchResult]) -> str:
        return self.prompt_template.format(
            query=(query or "").strip(),
            candidates=candidate_lines(candidates),
            last=len(candidates) - 1,
            m=min(self.top_m or len(candidates), len(candidates)),
        )

    def rerank(self, query: str, candidates: Sequence[SearchResult]) -> list[SearchResult]:
        candidates = list(candidates)
        if len(candidates) < 2:
            return candidates
        self.stats["calls"] += 1
        try:
            raw = self.llm(self.build_prompt(query, candidates))
        except Exception:
            self.stats["fallbacks"] += 1
            self.stats["errors"] += 1
            return candidates
        order = self._order_from(raw, len(candidates))
        if order is None:
            self.stats["fallbacks"] += 1
            self.stats["unparseable"] += 1
            return candidates
        return [
            _annotate(candidates[idx], reranker=self.name, rerank_rank=pos)
            for pos, idx in enumerate(order)
        ]

    def _order_from(self, raw: str, n: int) -> list[int] | None:
        """The full permutation implied by the model's answer, or None.

        Under the selection contract the unselected candidates are appended in
        their existing dense order, so the result is still a permutation of the
        same list and every metric downstream sees the shape it expects.
        """
        if not self.top_m:
            return parse_index_order(raw, n)
        m = min(self.top_m, n)
        picked = parse_index_selection(raw, n, m)
        if picked is None:
            return None
        chosen = set(picked)
        return picked + [i for i in range(n) if i not in chosen]
