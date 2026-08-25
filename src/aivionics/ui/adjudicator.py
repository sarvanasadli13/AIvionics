"""Gold-set adjudication logic (PLAN 0.7) — no Qt in this module.

400 hand-adjudicated pairs decide Gate 0, and Gate 0 decides whether the
case base exists at all. Two protocol rules from PLAN §5 are enforced here
rather than left to the operator:

  * **"unsure" is a first-class verdict.** Forcing a guess into yes/no/partial
    is what makes a gold set worthless; the unsure rate is reported and those
    pairs are excluded from scoring.
  * **adjudicate with the manual open.** Where the cited task has a body in
    the corpus it is shown, warnings and cautions first and non-collapsible
    (standing rule 3) — reading the actual task is what converts a clinical
    judgement into reading comprehension.

The queue table is created by the corpus pipeline
(`scripts/build_gold_queue.py`); this module only reads it and marks rows
done, so the two processes cannot fight over its shape.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

VERDICTS = ("yes", "no", "partial", "unsure")

# Keyboard verdicts, as specified. One key per verdict, no modifiers.
VERDICT_KEYS = {"y": "yes", "n": "no", "p": "partial", "u": "unsure"}

VERDICT_LABELS = {
    "yes": "Yes — the cited task is right for this defect",
    "no": "No — wrong task",
    "partial": "Partial — related but not the right entry point",
    "unsure": "Unsure — excluded from scoring, and that is fine",
}


class QueueMissing(RuntimeError):
    """Raised when `gold_queue` has not been built yet."""


@dataclass
class Pair:
    """One defect/task pair on screen."""

    queue_id: int
    seq: int
    stratum: str
    done: bool
    defect_id: int
    task_number: str
    defect_text: str = ""
    tail: str = ""
    reported_at: str = ""
    ata_ref: str = ""
    fault_code: str = ""
    task_title: str = ""
    task_body: str | None = None
    catalogue_only: bool = False
    manual_type: str = ""
    revision: str = ""
    warnings: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    verdict: str | None = None
    correct_task_number: str | None = None

    @property
    def has_body(self) -> bool:
        return bool(self.task_body and self.task_body.strip())


@dataclass
class Progress:
    done: int
    total: int
    per_stratum: list[tuple[str, int, int]]
    unsure: int = 0

    @property
    def pct(self) -> float:
        return (self.done / self.total * 100.0) if self.total else 0.0


class AdjudicationQueue:
    """Read the queue, write verdicts, resume where the last session stopped."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    # ── availability ──────────────────────────────────────────────────
    @property
    def exists(self) -> bool:
        row = self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gold_queue'"
        ).fetchone()
        return row is not None

    def require(self) -> None:
        if not self.exists:
            raise QueueMissing(
                "gold_queue table not found — run scripts/build_gold_queue.py first")

    # ── progress ──────────────────────────────────────────────────────
    def progress(self) -> Progress:
        self.require()
        total, done = self.con.execute(
            "SELECT COUNT(*), COALESCE(SUM(done),0) FROM gold_queue").fetchone()
        rows = self.con.execute(
            "SELECT stratum, COUNT(*), COALESCE(SUM(done),0) FROM gold_queue "
            "GROUP BY stratum ORDER BY stratum").fetchall()
        unsure = self.con.execute(
            "SELECT COUNT(*) FROM label_gold WHERE verdict='unsure'").fetchone()[0]
        return Progress(done=int(done or 0), total=int(total or 0),
                        per_stratum=[(r[0] or "—", int(r[2] or 0), int(r[1]))
                                     for r in rows],
                        unsure=int(unsure or 0))

    # ── navigation ────────────────────────────────────────────────────
    def sequences(self) -> list[int]:
        self.require()
        return [r[0] for r in
                self.con.execute("SELECT seq FROM gold_queue ORDER BY seq")]

    def resume_seq(self) -> int | None:
        """First pending pair, or the last pair when everything is done."""
        self.require()
        row = self.con.execute(
            "SELECT seq FROM gold_queue WHERE done=0 ORDER BY seq LIMIT 1").fetchone()
        if row:
            return row[0]
        row = self.con.execute(
            "SELECT seq FROM gold_queue ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def next_seq(self, seq: int) -> int | None:
        row = self.con.execute(
            "SELECT seq FROM gold_queue WHERE seq>? ORDER BY seq LIMIT 1",
            (seq,)).fetchone()
        return row[0] if row else None

    def previous_seq(self, seq: int) -> int | None:
        row = self.con.execute(
            "SELECT seq FROM gold_queue WHERE seq<? ORDER BY seq DESC LIMIT 1",
            (seq,)).fetchone()
        return row[0] if row else None

    # ── one pair ──────────────────────────────────────────────────────
    def pair(self, seq: int) -> Pair | None:
        """Assemble the pair at `seq`: queue row, defect, cited task, sections."""
        self.require()
        row = self.con.execute("""
            SELECT q.id, q.seq, q.stratum, q.done, q.defect_id, q.task_number,
                   d.defect_text, d.aircraft_tail, d.reported_at, d.ata_ref,
                   d.fault_code
            FROM gold_queue q
            LEFT JOIN defect d ON d.id = q.defect_id
            WHERE q.seq = ?
        """, (seq,)).fetchone()
        if row is None:
            return None

        pair = Pair(queue_id=row[0], seq=row[1], stratum=row[2] or "—",
                    done=bool(row[3]), defect_id=row[4], task_number=row[5],
                    defect_text=row[6] or "", tail=row[7] or "",
                    reported_at=row[8] or "", ata_ref=row[9] or "",
                    fault_code=row[10] or "")

        task = self.con.execute("""
            SELECT t.id, t.title, t.body, t.catalogue_only,
                   m.manual_type, m.revision
            FROM task t JOIN manual m ON m.id = t.manual_id
            WHERE t.task_number = ?
            ORDER BY m.is_current DESC LIMIT 1
        """, (pair.task_number,)).fetchone()
        if task:
            pair.task_title = task[1] or ""
            pair.task_body = task[2]
            pair.catalogue_only = bool(task[3])
            pair.manual_type = task[4] or ""
            pair.revision = task[5] or ""
            for kind, text in self.con.execute(
                    "SELECT kind, text FROM task_section WHERE task_id=? "
                    "AND kind IN ('warning','caution') ORDER BY seq", (task[0],)):
                (pair.warnings if kind == "warning" else pair.cautions).append(text)

        existing = self.con.execute(
            "SELECT verdict, correct_task_number FROM label_gold "
            "WHERE defect_id=? AND task_number=? ORDER BY id DESC LIMIT 1",
            (pair.defect_id, pair.task_number)).fetchone()
        if existing:
            pair.verdict, pair.correct_task_number = existing[0], existing[1]
        return pair

    # ── writing ───────────────────────────────────────────────────────
    def commit(self, seq: int, verdict: str, correct_task_number: str | None = None,
               note: str | None = None) -> None:
        """Refused. `goldreview.GoldReviewService` is the only write path.

        This method used to DELETE the `label_gold` row and INSERT a
        replacement, with no reviewer identity, no permission check, no
        revision history and no audit entry. Two independent writers of the
        gold set is not a duplication problem, it is a provenance problem:
        nothing downstream could tell which of them produced a given answer.

        The class survives as a read-only adapter because the queue readers
        and the preview tooling use it. Writing goes through the service.
        """
        raise GoldReviewWriteRemoved(
            "AdjudicationQueue.commit no longer writes the gold set. Use "
            "aivionics.goldreview.GoldReviewService, which records the "
            "reviewer, keeps revision history and writes the audit entry in "
            "the same transaction.")


class GoldReviewWriteRemoved(RuntimeError):
    """Raised by the retired legacy write path."""
