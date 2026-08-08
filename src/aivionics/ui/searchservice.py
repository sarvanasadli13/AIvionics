"""Bridge from the Diagnose screen to the Phase 2 retrieval engine.

Three things this file exists to get right:

* **The engine loads lazily and off the UI thread.** Importing fastembed and
  reading 76k case vectors out of SQLite costs seconds; doing either inside a
  click handler freezes the window. The `Searcher` is built once on the worker
  thread and reused, so only the first search pays.
* **A missing index is a state, not a crash.** The database can exist with no
  vectors in it — that is the normal condition until `scripts/phase2_index.py`
  has run — and the screen has to say so precisely rather than return nothing.
* **`found:` is reported as absent, not as empty.** SDR carries what was
  reported and what was replaced; the shop teardown finding is not in this
  data at all (PLAN §1.3, §3.1). A blank cell would read as "nothing was
  found"; the honest rendering is "not recorded".
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from .. import config

# how much rectification text to carry into the cases table
_RECT_CHARS = 160


@dataclass(frozen=True)
class IndexStatus:
    """Whether the retrieval index is usable, and why not when it is not."""

    task_vectors: int = 0
    case_vectors: int = 0
    index_version: str = ""
    reason: str = ""

    @property
    def ready(self) -> bool:
        return self.task_vectors > 0


def index_status(con: sqlite3.Connection | None,
                 index_version: str | None = None) -> IndexStatus:
    version = index_version or config.INDEX_VERSION
    if con is None:
        return IndexStatus(index_version=version,
                           reason="no database — run the Phase 0/1 ingest first")
    try:
        rows = dict(con.execute(
            "SELECT kind, COUNT(*) FROM vec_index WHERE index_version=?"
            " GROUP BY kind", (version,)).fetchall())
    except sqlite3.Error as exc:
        return IndexStatus(index_version=version, reason=f"index unreadable — {exc}")
    tasks, cases = rows.get("task", 0), rows.get("case", 0)
    if not tasks:
        return IndexStatus(
            case_vectors=cases, index_version=version,
            reason="index not built — run scripts/phase2_index.py")
    return IndexStatus(task_vectors=tasks, case_vectors=cases,
                       index_version=version)


@dataclass
class CaseRow:
    """One prior case, already reduced to what the table renders."""

    tail: str
    reported_at: str
    replaced: str
    found: str
    found_recorded: bool
    sdr_year: int | None = None
    score: float = 0.0
    defect_id: int | None = None      # the anchor a note hangs off


def case_rows(con: sqlite3.Connection, ids: list[int]) -> dict[int, CaseRow]:
    """Load the `replaced:` / `found:` split for the given defect ids.

    `replaced:` comes from the rectification half of the split narrative.
    `found:` comes from `defect_finding`, which SDR never populates — so for a
    mined case it renders as "not recorded", which is the true state of the
    evidence rather than an implied negative.
    """
    if not ids:
        return {}
    out: dict[int, CaseRow] = {}
    placeholders = ",".join("?" * len(ids))
    for did, tail, at, year, rect, ftype, ftext in con.execute(
        f"SELECT d.id, d.aircraft_tail, d.reported_at, d.sdr_year,"
        f"       substr(COALESCE(d.rectification_text,''), 1, {_RECT_CHARS}),"
        f"       f.finding_type, f.finding_text"
        f"  FROM defect d"
        f"  LEFT JOIN defect_finding f ON f.defect_id = d.id"
        f" WHERE d.id IN ({placeholders})", ids
    ):
        recorded = bool(ftype or ftext)
        out[did] = CaseRow(
            defect_id=did,
            tail=(tail or "—").strip(),
            reported_at=(at or "—"),
            replaced=" ".join((rect or "").split()) or "—",
            found=" ".join((ftext or ftype or "").split()) if recorded
                  else "not recorded",
            found_recorded=recorded,
            sdr_year=year,
        )
    return out


class SearchService:
    """Owns the `Searcher` and runs queries on a worker thread.

    The connection is opened here rather than shared with the UI: SQLite
    objects are not safely reusable across threads, and the retrieval engine
    only ever reads.
    """

    def __init__(self, db_path: Path | str | None = None,
                 index_version: str | None = None) -> None:
        self.db_path = Path(db_path or config.DB_PATH)
        self.index_version = index_version or config.INDEX_VERSION
        self._searcher = None
        self._calibrator = None
        self._llm = None
        self._llm_tried = False
        self._con: sqlite3.Connection | None = None
        self._pool = QThreadPool.globalInstance()
        self.busy = False

    # ── engine ───────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        if self._con is None:
            self._con = sqlite3.connect(
                f"file:{self.db_path.as_posix()}?mode=ro", uri=True,
                timeout=30.0, check_same_thread=False)
            self._con.execute("PRAGMA query_only=ON")
        return self._con

    def searcher(self):
        """Build once, reuse. Imports the engine on first call only."""
        if self._searcher is None:
            from ..retrieval.embedder import FastEmbedder
            from ..retrieval.search import Searcher
            self._searcher = Searcher(
                self._connect(), FastEmbedder(), index_version=self.index_version)
        return self._searcher

    def calibrator(self):
        """Fitted abstention thresholds, or an empty one that never abstains.

        An uncalibrated install must answer rather than silently suppress
        everything — the failure mode of a missing calibration table should be
        visible behaviour, not an app that appears to find nothing.
        """
        if self._calibrator is None:
            from ..retrieval.calibration import Calibrator
            self._calibrator = Calibrator.load(self._connect(),
                                               self.index_version)
        return self._calibrator

    def llm(self):
        """The optional summariser, or None. Probed once.

        A missing or unreachable model is an ordinary state: the whole screen
        works without it, so failure here must never propagate to the search.
        """
        if not self._llm_tried:
            self._llm_tried = True
            try:
                from ..llm.client import OllamaClient
                client = OllamaClient()
                self._llm = client if client.health().ok else None
            except Exception:                                    # noqa: BLE001
                self._llm = None
        return self._llm

    def status(self) -> IndexStatus:
        try:
            return index_status(self._connect(), self.index_version)
        except sqlite3.Error as exc:
            return IndexStatus(index_version=self.index_version,
                               reason=f"database unavailable — {exc}")

    # ── one query ────────────────────────────────────────────────────────
    def run(self, query: str, *, top_k: int = 20, cases_k: int = 12,
            jasc: str | None = None, summarise_cases: bool = True) -> dict:
        """Blocking search. Called on the worker thread, never on the UI one."""
        searcher = self.searcher()
        tasks = searcher.search(query, kind="task", top_k=top_k, rerank=False,
                                jasc=jasc)
        cases = searcher.search(query, kind="case", top_k=cases_k, rerank=False)
        con = self._connect()
        ids = [r.id for r in cases.results if r.id is not None]
        rows = case_rows(con, ids)
        for r in cases.results:
            row = rows.get(r.id)
            if row is not None:
                row.score = r.score
        # The abstention decision, made against signals that survive score
        # normalisation. Chapter comes from the top result rather than the
        # query: the JASC hint is what a reporter typed and is miscoded at
        # chapter boundaries.
        cal = self.calibrator()
        chapter = tasks.results[0].ata_chapter if tasks.results else None
        abstain = bool(tasks.results) and cal.should_abstain(
            chapter, tasks.confidence)
        ordered_cases = [rows[i] for i in ids if i in rows]
        summary = None
        if summarise_cases:
            from ..llm.summarise import summarise_cases as _summarise
            summary = _summarise(
                self.llm(), query,
                [{"tail": c.tail, "reported_at": c.reported_at,
                  "replaced": c.replaced, "found": c.found}
                 for c in ordered_cases[:6]],
                allowed_tasks={r.task_number for r in tasks.results
                               if r.task_number})
        return {
            "query": query,
            "tasks": tasks,
            "cases": cases,
            "case_rows": ordered_cases,
            "summary": summary,
            "abstain": abstain,
            "calibrated": cal.fitted,
            "calibration_note": cal.explain(chapter),
            "chapter": chapter,
        }

    # ── async wrapper ────────────────────────────────────────────────────
    def submit(self, query: str, signals: "SearchSignals", **kw) -> bool:
        """Queue a search. Returns False when one is already running."""
        if self.busy:
            return False
        self.busy = True
        self._pool.start(_SearchJob(self, query, signals, kw))
        return True

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None
        self._searcher = None
        self._calibrator = None


class SearchSignals(QObject):
    """Signals cannot live on a QRunnable, so they get their own QObject."""

    done = Signal(object)        # the result dict
    failed = Signal(str, str)    # query, message


class _SearchJob(QRunnable):
    def __init__(self, service: SearchService, query: str,
                 signals: SearchSignals, kw: dict) -> None:
        super().__init__()
        self.service = service
        self.query = query
        self.signals = signals
        self.kw = kw

    def run(self) -> None:                                   # pragma: no cover
        try:
            result = self.service.run(self.query, **self.kw)
            self.signals.done.emit(result)
        except Exception as exc:                             # noqa: BLE001
            self.signals.failed.emit(self.query, f"{type(exc).__name__}: {exc}")
        finally:
            self.service.busy = False
