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
Example for 4 candidates: [3, 0, 2, 1]
"""


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


def parse_index_order(raw: str, n: int) -> list[int] | None:
    """Strict permutation parse. Returns None on anything that is not exactly a
    JSON array holding each index 0..n-1 once — which is the fail-safe trigger."""
    if raw is None:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    if not (text.startswith("[") and text.endswith("]")):
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


class LLMReranker:
    """Takes any callable ``llm(prompt) -> str``. Injected, so nothing here
    depends on a particular client, and pytest never reaches a network."""

    name = "llm"

    def __init__(self, llm: Callable[[str], str],
                 prompt_template: str = PROMPT_TEMPLATE) -> None:
        self.llm = llm
        self.prompt_template = prompt_template
        self.stats = {"calls": 0, "fallbacks": 0}

    def build_prompt(self, query: str, candidates: Sequence[SearchResult]) -> str:
        return self.prompt_template.format(
            query=(query or "").strip(),
            candidates=candidate_lines(candidates),
            last=len(candidates) - 1,
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
            return candidates
        order = parse_index_order(raw, len(candidates))
        if order is None:
            self.stats["fallbacks"] += 1
            return candidates
        return [
            _annotate(candidates[idx], reranker=self.name, rerank_rank=pos)
            for pos, idx in enumerate(order)
        ]
