"""Hybrid retrieval (PLAN 2.2-2.6).

Stage 1 gathers candidates from two channels and unions them:

  FTS5 / BM25   exact tokens — task numbers, part numbers, SMYD.
  dense cosine  ATA hierarchy + task title vectors (Jo 2025 representation).

The two are then merged with **query-dependent weights** (PLAN 2.4b): Jo measured
BM25 collapsing 87.57% -> 63.38% Hit@5 on typo-injected queries while reranked
dense held above 88%, and SDR narratives are typo-dense by nature. So BM25 leads
only when the query actually contains something exact to match on; otherwise the
dense channel dominates and FTS contributes a small tie-break.

JASC/ATA agreement is a **soft boost, never a gate** (PLAN 2.5). Reporter-entered
codes are miscoded at the 22/24/27/31/34 boundaries, and a hard filter puts the
right task permanently out of reach with no recovery path.

Task results are LOCATORS — number, title, manual, hierarchy. Bodies are never
carried here; the UI looks them up separately (standing rule 1).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from .embedder import Embedder, blob_to_vec

# ── query shape detection ────────────────────────────────────────────────
# 34-11-01-400-801 · G73-00-00-810-A73 · short forms like 34-11-01
TASK_NUMBER_TOKEN = re.compile(
    r"\b[A-Z]?\d{2}-\d{2}-\d{2}(?:-\d{3})?(?:-(?:[A-Z]\d{2}|\d{3}[A-Z]?))?\b",
    re.I)
# hyphen/slash separated and contains a digit somewhere: 622-1234-001, P5-11
PART_NUMBER_TOKEN = re.compile(
    r"\b(?=[A-Za-z0-9/-]*\d)[A-Za-z0-9]{1,}(?:[-/][A-Za-z0-9]{1,})+\b")
# single token mixing letters and digits: A429, 622A, ARINC429
ALNUM_MIX_TOKEN = re.compile(
    r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,}\b")

_WORD = re.compile(r"[A-Za-z0-9]+")
_STOPWORDS = frozenset("""
a an and are as at be been but by for from had has have in into is it its of on
or that the this to was were will with not no do does did during after before
""".split())

# weights — see module docstring
W_EXACT = {"fts": 0.60, "dense": 0.40}
W_FREE = {"fts": 0.15, "dense": 0.85}
JASC_BOOST = 0.12

# bm25 column weights: an exact task-number hit must outrank a title word hit
TASK_BM25_WEIGHTS = (10.0, 3.0, 1.0)


@dataclass(frozen=True)
class Effectivity:
    """What we know about the airframe the query is being asked for."""
    msn: str | None = None
    tail: str | None = None
    line_number: str | None = None


@dataclass
class SearchResult:
    kind: str                      # 'task' | 'case'
    id: int | None
    score: float
    task_number: str | None = None
    title: str | None = None
    manual_type: str | None = None
    ata_chapter: str | None = None
    hierarchy: str | None = None
    provenance: dict = field(default_factory=dict)


@dataclass
class SearchRun:
    """One query's full trace. ``ranked`` is every stage-1 candidate after the
    merge and the JASC boost; ``results`` is what the caller asked for, after
    reranking. Recall@50 is read off ``ranked`` — reranking only reorders inside
    the top 50, so the set is identical either way."""
    query: str
    ranked: list[SearchResult]
    results: list[SearchResult]
    weights: dict
    exact_query: bool
    channels: dict = field(default_factory=dict)
    reranked: bool = False

    @property
    def stage1_size(self) -> int:
        return len(self.ranked)


def has_exact_token(text: str) -> bool:
    """Does the query carry something BM25 can match exactly?"""
    text = text or ""
    return bool(
        TASK_NUMBER_TOKEN.search(text)
        or PART_NUMBER_TOKEN.search(text)
        or ALNUM_MIX_TOKEN.search(text)
    )


def build_fts_match(text: str, max_terms: int = 32) -> str | None:
    """An OR of quoted terms. Exact-looking tokens become quoted phrases so
    '34-11-01-400-801' matches as five consecutive tokens rather than five
    independent ones."""
    parts: list[str] = []
    seen: set[str] = set()

    for pattern in (TASK_NUMBER_TOKEN, PART_NUMBER_TOKEN):
        for m in pattern.finditer(text or ""):
            phrase = " ".join(_WORD.findall(m.group(0))).lower()
            if phrase and phrase not in seen:
                seen.add(phrase)
                parts.append(f'"{phrase}"')

    for word in _WORD.findall((text or "").lower()):
        if len(parts) >= max_terms:
            break
        if len(word) < 3 or word in _STOPWORDS or word in seen:
            continue
        seen.add(word)
        parts.append(f'"{word}"')

    return " OR ".join(parts) if parts else None


def fts_search(con: sqlite3.Connection, table: str, match: str, limit: int,
               col_weights: Sequence[float] | None = None) -> dict[int, float]:
    """rowid -> relevance (higher is better). bm25() is negative-is-better, so
    it is negated here."""
    if not match:
        return {}
    if col_weights:
        rank = f"bm25({table}, {', '.join(str(w) for w in col_weights)})"
    else:
        rank = f"bm25({table})"
    sql = (f"SELECT rowid, {rank} AS b FROM {table} WHERE {table} MATCH ? "
           f"ORDER BY b LIMIT ?")
    try:
        rows = con.execute(sql, (match, limit)).fetchall()
    except sqlite3.OperationalError:
        return {}                      # malformed MATCH — treat as no hits
    return {int(rid): -float(b) for rid, b in rows}


def dense_search(qvec: np.ndarray, ids: np.ndarray, mat: np.ndarray,
                 limit: int) -> dict[int, float]:
    """Cosine over the stored vectors. numpy is more than fast enough at this
    corpus size (~4.5k tasks); no sqlite-vec dependency at query time."""
    if mat.size == 0 or ids.size == 0:
        return {}
    sims = mat @ qvec
    k = min(limit, sims.shape[0])
    top = np.argpartition(-sims, k - 1)[:k] if k < sims.shape[0] else np.arange(sims.shape[0])
    top = top[np.argsort(-sims[top])]
    return {int(ids[i]): float(sims[i]) for i in top}


def _max_norm(scores: dict[int, float]) -> dict[int, float]:
    if not scores:
        return {}
    hi = max(scores.values())
    if hi <= 0:
        return {k: 0.0 for k in scores}
    return {k: max(0.0, v) / hi for k, v in scores.items()}


def _in_chunks(seq: Sequence, size: int = 500) -> Iterable[Sequence]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def resolve_applicability(task_row: dict, eff: Effectivity | None,
                          airplanes: dict[str, dict]) -> str:
    """Fail closed (standing rule 8): anything we cannot positively resolve is
    reported as unresolved, never as a clean answer."""
    if eff is None or not (eff.msn or eff.tail or eff.line_number):
        return "not_checked"
    refs = [r for r in re.split(r"[^A-Za-z0-9_]+", task_row.get("applic_refs") or "") if r]
    if refs:
        known = [airplanes[r] for r in refs if r in airplanes]
        if not known:
            return "unresolved"
        for ap in known:
            if eff.msn and ap.get("msn") and str(ap["msn"]) == str(eff.msn):
                return "applicable"
            if eff.tail and ap.get("tail") and str(ap["tail"]).upper() == str(eff.tail).upper():
                return "applicable"
            if eff.line_number and ap.get("line_no") and str(ap["line_no"]) == str(eff.line_number):
                return "applicable"
        return "not_applicable"
    raw = (task_row.get("effectivity_raw") or "").upper()
    if "TBC ALL" in raw or re.search(r"\bAIRPLANES\s+ALL\b", raw):
        return "applicable"
    return "unresolved"


class Searcher:
    """Holds the connection, the embedder and the loaded vector matrices.
    Construct once and reuse — the vectors are loaded lazily and cached, which
    is what makes a full evaluation sweep affordable."""

    def __init__(
        self,
        con: sqlite3.Connection,
        embedder: Embedder,
        reranker=None,
        index_version: str | None = None,
        jasc_boost: float = JASC_BOOST,
    ) -> None:
        self.con = con
        self.embedder = embedder
        self.reranker = reranker
        self.index_version = index_version or embedder.index_version
        self.jasc_boost = jasc_boost
        self._vec_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._airplanes: dict[str, dict] | None = None

    # ── loading ──────────────────────────────────────────────────────────
    def vectors(self, kind: str) -> tuple[np.ndarray, np.ndarray]:
        if kind not in self._vec_cache:
            rows = self.con.execute(
                "SELECT ref_id, dim, vec FROM vec_index"
                " WHERE kind=? AND index_version=? ORDER BY ref_id",
                (kind, self.index_version)).fetchall()
            if not rows:
                self._vec_cache[kind] = (np.zeros(0, dtype=np.int64),
                                         np.zeros((0, self.embedder.dim), dtype=np.float32))
            else:
                ids = np.array([r[0] for r in rows], dtype=np.int64)
                mat = np.stack([blob_to_vec(r[2], r[1]) for r in rows])
                self._vec_cache[kind] = (ids, mat.astype(np.float32))
        return self._vec_cache[kind]

    def _airplane_map(self) -> dict[str, dict]:
        if self._airplanes is None:
            self._airplanes = {
                r[0]: {"model": r[1], "msn": r[3], "line_no": r[4], "tail": r[5]}
                for r in self.con.execute(
                    "SELECT eff_ref, model, owner, msn, line_no, tail, engine"
                    " FROM effectivity_airplane")
            }
        return self._airplanes

    def _task_meta(self, ids: Sequence[int]) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for chunk in _in_chunks(list(ids)):
            ph = ",".join("?" * len(chunk))
            for row in self.con.execute(
                f"SELECT t.id, t.task_number, t.title, t.ata_chapter, t.ata_section,"
                f" t.ata_subject, t.embed_text, t.effectivity_raw, t.applic_refs,"
                f" t.catalogue_only, m.manual_type, m.revision"
                f" FROM task t JOIN manual m ON m.id=t.manual_id"
                f" WHERE t.id IN ({ph})", chunk
            ):
                out[row[0]] = {
                    "task_number": row[1], "title": row[2], "ata_chapter": row[3],
                    "ata_section": row[4], "ata_subject": row[5],
                    "embed_text": row[6], "effectivity_raw": row[7],
                    "applic_refs": row[8], "catalogue_only": row[9],
                    "manual_type": row[10], "revision": row[11],
                }
        return out

    def _case_meta(self, ids: Sequence[int]) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for chunk in _in_chunks(list(ids)):
            ph = ",".join("?" * len(chunk))
            for row in self.con.execute(
                f"SELECT id, aircraft_tail, reported_at, ata_ref, sdr_year,"
                f" substr(COALESCE(defect_text,''), 1, 300)"
                f" FROM defect WHERE id IN ({ph})", chunk
            ):
                out[row[0]] = {
                    "tail": row[1], "reported_at": row[2], "ata_ref": row[3],
                    "sdr_year": row[4], "snippet": row[5],
                }
        return out

    # ── the pipeline ─────────────────────────────────────────────────────
    def search(
        self,
        query: str,
        *,
        kind: str = "task",
        jasc: str | None = None,
        top_k: int = 20,
        pool_k: int = 200,
        rerank_k: int = 50,
        rerank: bool = True,
        effectivity: Effectivity | None = None,
        channels: Sequence[str] = ("fts", "dense"),
    ) -> SearchRun:
        query = query or ""
        exact = has_exact_token(query)
        weights = dict(W_EXACT if exact else W_FREE)
        for ch in ("fts", "dense"):
            if ch not in channels:
                weights[ch] = 0.0

        fts_raw: dict[int, float] = {}
        if weights["fts"] > 0:
            match = build_fts_match(query)
            if match:
                table = "task_fts" if kind == "task" else "case_fts"
                cw = TASK_BM25_WEIGHTS if kind == "task" else None
                fts_raw = fts_search(self.con, table, match, pool_k, cw)

        dense_raw: dict[int, float] = {}
        if weights["dense"] > 0:
            ids, mat = self.vectors(kind)
            if ids.size:
                qvec = self.embedder.embed([query])[0]
                dense_raw = dense_search(qvec, ids, mat, pool_k)

        fts_n, dense_n = _max_norm(fts_raw), _max_norm(dense_raw)
        merged = {
            rid: weights["fts"] * fts_n.get(rid, 0.0)
                 + weights["dense"] * dense_n.get(rid, 0.0)
            for rid in set(fts_n) | set(dense_n)
        }

        ranked = (self._build_task_results(merged, fts_n, dense_n, jasc, effectivity)
                  if kind == "task"
                  else self._build_case_results(merged, fts_n, dense_n, jasc))
        ranked.sort(key=lambda r: r.score, reverse=True)

        results = ranked[:top_k]
        reranked = False
        if rerank and self.reranker is not None and len(ranked) > 1:
            head = self.reranker.rerank(query, ranked[:rerank_k])
            results = (list(head) + ranked[rerank_k:])[:top_k]
            reranked = True

        return SearchRun(
            query=query, ranked=ranked, results=results, weights=weights,
            exact_query=exact, reranked=reranked,
            channels={"fts": len(fts_raw), "dense": len(dense_raw)},
        )

    # ── result construction ──────────────────────────────────────────────
    def _build_task_results(self, merged, fts_n, dense_n, jasc, eff):
        meta = self._task_meta(list(merged))
        airplanes = self._airplane_map() if eff else {}
        jasc_ch = (jasc or "").strip()[:2] or None
        out = []
        for rid, base in merged.items():
            m = meta.get(rid)
            if m is None:
                continue
            boost = self.jasc_boost if (jasc_ch and m["ata_chapter"] == jasc_ch) else 0.0
            applic = resolve_applicability(m, eff, airplanes)
            out.append(SearchResult(
                kind="task", id=rid, score=base + boost,
                task_number=m["task_number"], title=m["title"],
                manual_type=m["manual_type"], ata_chapter=m["ata_chapter"],
                hierarchy=m["embed_text"],
                provenance={
                    "channels": _channels_of(rid, fts_n, dense_n),
                    "fts": round(fts_n.get(rid, 0.0), 4),
                    "dense": round(dense_n.get(rid, 0.0), 4),
                    "jasc_boost": round(boost, 4),
                    "jasc_hint": jasc_ch,
                    "applicability": applic,
                    "catalogue_only": bool(m["catalogue_only"]),
                    "manual_revision": m["revision"],
                },
            ))
        return out

    def _build_case_results(self, merged, fts_n, dense_n, jasc):
        """SDR carries no modification state — there is nothing to filter on, so
        cases get a provenance tag instead of an effectivity path (PLAN 2.6)."""
        meta = self._case_meta(list(merged))
        jasc_ch = (jasc or "").strip()[:2] or None
        out = []
        for rid, base in merged.items():
            m = meta.get(rid)
            if m is None:
                continue
            boost = self.jasc_boost if (jasc_ch and m["ata_ref"] == jasc_ch) else 0.0
            out.append(SearchResult(
                kind="case", id=rid, score=base + boost,
                title=m["snippet"], ata_chapter=m["ata_ref"],
                provenance={
                    "channels": _channels_of(rid, fts_n, dense_n),
                    "fts": round(fts_n.get(rid, 0.0), 4),
                    "dense": round(dense_n.get(rid, 0.0), 4),
                    "jasc_boost": round(boost, 4),
                    "source": "sdr",
                    "sdr_year": m["sdr_year"],
                    "tail": m["tail"],
                    "mod_state": "unknown — SDR records no modification state",
                },
            ))
        return out


def _channels_of(rid, fts_n, dense_n) -> str:
    in_f, in_d = rid in fts_n, rid in dense_n
    return "both" if in_f and in_d else ("fts" if in_f else "dense")
