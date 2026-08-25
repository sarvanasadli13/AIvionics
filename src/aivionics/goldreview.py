"""Gold-set review: schema, validation and persistence. No Qt in this module.

The 400 pairs in `gold_queue` are the held-out human reference set. Every
retrieval number this project has reported is agreement with a regex-derived
*silver* label; this is the machinery that replaces that with agreement with
a person.

The lifecycle is split into three tables on purpose, because the first
version of this module kept drafts and answers in one row and that made four
distinct corruptions reachable:

* **`gold_review_response` holds only the current finalized answer**, keyed
  `UNIQUE(queue_id, review_kind)`. The reviewer is an *attribute* of the
  revision, never part of its identity — so a second authorised reviewer can
  revise a case instead of colliding with the first one's row.
* **`gold_review_draft` is a separate table.** A draft cannot demote, replace
  or delete an answer, because it is not stored in the same place as one.
  Saving or discarding a draft is structurally incapable of touching
  `label_gold` or `gold_queue.done`.
* **`gold_review_history` is append-only**, and records both the reviewer
  who wrote the revision being replaced and the one who replaced it.

`label_gold` is kept as a compatibility projection of the current finalized
primary answers, so the existing evaluation scripts keep working. It is a
view *of* the truth, never the truth.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from . import audit

VERDICTS = ("yes", "no", "partial", "unsure")
REVIEW_KINDS = ("primary", "professional")
CONFIDENCES = ("low", "medium", "high")

# The set is 400 cases. A "frozen release" that does not prove that is a
# label on an unknown quantity, so the number is a constant and every freeze
# is checked against it.
EXPECTED_GOLD_CASES = 400

GOLD_REVIEW_PERMISSION = "gold_review"
GOLD_MANAGE_PERMISSION = "gold_review_manage"

VERDICT_KEYS = {"y": "yes", "n": "no", "p": "partial", "u": "unsure"}

VERDICT_LABELS = {
    "yes": "The cited task is an appropriate entry point for this defect.",
    "no": "The cited task is wrong, unrelated or technically inappropriate.",
    "partial": "The task is related, but it is not the correct or safest entry point.",
    "unsure": "The available evidence or my qualification is not sufficient to decide.",
}

WRONG_REASONS = [
    ("wrong_ata", "Wrong ATA subject"),
    ("wrong_system", "Wrong system/component"),
    ("action_before_isolation", "Action task shown before fault isolation"),
    ("too_broad", "Task is too broad"),
    ("too_narrow", "Task is too narrow"),
    ("wrong_applicability", "Wrong aircraft/manual applicability"),
    ("wrong_entry_point", "Related task, but wrong entry point"),
    ("evidence_contradicts", "Manual evidence contradicts the pairing"),
    ("not_in_corpus", "Correct task unavailable in corpus"),
    ("other", "Other"),
]

UNSURE_REASONS = [
    ("insufficient_defect", "Insufficient defect information"),
    ("no_procedure", "Full manual procedure unavailable"),
    ("effectivity_unclear", "Effectivity unclear"),
    ("ambiguous", "Ambiguous between multiple tasks"),
    ("out_of_competence", "Outside reviewer's competence"),
    ("source_inconsistent", "Source material appears inconsistent"),
    ("other", "Other"),
]

WRONG_REASON_CODES = frozenset(c for c, _ in WRONG_REASONS)
UNSURE_REASON_CODES = frozenset(c for c, _ in UNSURE_REASONS)

# What the retrieval evaluation is actually allowed to condition on, read off
# `retrieval/evalharness.load_eval_queries`: it builds every query from
# `defect.defect_text` and `defect.ata_ref`, and restricts the pool by
# `sdr_raw.aircraft_model`. Showing the reviewer more than the engine saw
# measures the reviewer's extra context, not the engine.
EVAL_CONTEXT_FIELDS = ("defect_text", "ata_ref", "aircraft_model")

# Never carried on a pair under review. `stratum` is on this list because it
# is literally `chapter|diagnostic-or-action|length-tercile`, and the middle
# field answers one of the reason codes the reviewer is about to choose from.
LEAKING_FIELDS = ("stratum", "score", "rank", "confidence_threshold",
                  "calibrated", "model_output", "silver_label",
                  "previous_verdict", "retrieval_rank")

AUDIT_ACTIONS = ("gold_review_finalized", "gold_review_revised",
                 "gold_set_finalized", "gold_set_reopened",
                 "gold_set_exported")

DRAFT_FIELDS = ("verdict", "correct_task_number", "correct_task_unknown",
                "reason_code", "note", "confidence", "manual_checked")


class GoldReviewError(RuntimeError):
    """Domain refusal — the caller asked for something the rules forbid."""


class QueueMissing(GoldReviewError):
    """`gold_queue` has not been built."""


class NotAuthorised(GoldReviewError):
    """The reviewer does not hold the permission the operation needs."""


class SetFrozen(GoldReviewError):
    """The gold set is released; ordinary editing is closed."""


class MigrationBlocked(GoldReviewError):
    """A pre-existing development schema needs a deliberate decision."""


class ValidationFailed(GoldReviewError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── schema ───────────────────────────────────────────────────────────────
_SCHEMA = """
-- The single current finalized answer per case per review kind.
-- The reviewer is an attribute of the revision, never part of its identity:
-- a case has one answer, whoever wrote the latest revision of it.
CREATE TABLE IF NOT EXISTS gold_review_response(
    id                   INTEGER PRIMARY KEY,
    queue_id             INTEGER NOT NULL REFERENCES gold_queue(id),
    review_kind          TEXT    NOT NULL,
    verdict              TEXT    NOT NULL,
    correct_task_number  TEXT,
    correct_task_unknown INTEGER NOT NULL DEFAULT 0,
    reason_code          TEXT,
    note                 TEXT,
    confidence           TEXT,
    manual_checked       INTEGER NOT NULL DEFAULT 0,
    response_revision    INTEGER NOT NULL DEFAULT 1,
    reviewer_user_id     INTEGER NOT NULL REFERENCES app_user(id),
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL,
    finalized_at         TEXT    NOT NULL,
    CHECK (review_kind IN ('primary','professional')),
    CHECK (verdict IN ('yes','no','partial','unsure')),
    CHECK (confidence IS NULL OR confidence IN ('low','medium','high')),
    UNIQUE(queue_id, review_kind)
);

-- Work in progress, per reviewer. A separate table, so that saving or
-- discarding a draft cannot reach an answer even by accident.
CREATE TABLE IF NOT EXISTS gold_review_draft(
    id                   INTEGER PRIMARY KEY,
    queue_id             INTEGER NOT NULL REFERENCES gold_queue(id),
    review_kind          TEXT    NOT NULL,
    reviewer_user_id     INTEGER NOT NULL REFERENCES app_user(id),
    verdict              TEXT,
    correct_task_number  TEXT,
    correct_task_unknown INTEGER NOT NULL DEFAULT 0,
    reason_code          TEXT,
    note                 TEXT,
    confidence           TEXT,
    manual_checked       INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL,
    CHECK (review_kind IN ('primary','professional')),
    CHECK (verdict IS NULL OR verdict IN ('yes','no','partial','unsure')),
    UNIQUE(queue_id, review_kind, reviewer_user_id)
);

-- Append-only. Never updated, never deleted, and it keeps both identities:
-- who wrote the revision being replaced, and who replaced it.
CREATE TABLE IF NOT EXISTS gold_review_history(
    id                    INTEGER PRIMARY KEY,
    response_id           INTEGER NOT NULL,
    queue_id              INTEGER NOT NULL,
    review_kind           TEXT    NOT NULL,
    revision              INTEGER NOT NULL,
    snapshot              TEXT    NOT NULL,
    previous_snapshot     TEXT,
    reviewer_user_id      INTEGER,
    previous_reviewer_user_id INTEGER,
    changed_by_user_id    INTEGER,
    changed_at            TEXT    NOT NULL,
    change_reason         TEXT
);

CREATE INDEX IF NOT EXISTS ix_gold_history_queue
    ON gold_review_history(queue_id, review_kind, revision);

CREATE TABLE IF NOT EXISTS gold_set_release(
    id                   INTEGER PRIMARY KEY,
    version              INTEGER NOT NULL UNIQUE,
    case_count           INTEGER NOT NULL,
    queue_fingerprint    TEXT    NOT NULL,
    response_fingerprint TEXT    NOT NULL,
    finalized_by         INTEGER,
    finalized_at         TEXT    NOT NULL,
    status               TEXT    NOT NULL,
    unlock_reason        TEXT,
    reopened_by          INTEGER,
    reopened_at          TEXT,
    CHECK (status IN ('frozen','reopened'))
);
"""

# Columns that identify the first, flawed development schema: a `state`
# column on the response table meant drafts and answers shared a row.
_LEGACY_MARKER = "state"


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def migrate(con: sqlite3.Connection, *, allow_legacy_rebuild: bool = False) -> None:
    """Additive and idempotent. Safe on every application start.

    Creates no `gold_queue` rows and alters none: the 400 pairs are data, and
    this is schema.

    A database carrying the *first* development schema is refused rather than
    silently rebuilt, because that shape stored drafts and answers in one
    table and there is no general way to tell which rows were meant to be
    answers. `allow_legacy_rebuild=True` migrates it deliberately, keeping
    only rows that were final.
    """
    con.execute("PRAGMA foreign_keys=ON")
    cols = _table_columns(con, "gold_review_response")
    if cols and _LEGACY_MARKER in cols:
        if not allow_legacy_rebuild:
            raise MigrationBlocked(
                "This database carries the earlier development schema, in "
                "which gold_review_response had a `state` column and drafts "
                "shared a row with answers. Re-run with "
                "allow_legacy_rebuild=True to migrate it: rows with "
                "state='final' are kept as answers and drafts are discarded. "
                "Take a backup first.")
        _rebuild_legacy(con)
    con.executescript(_SCHEMA)
    _grant_default_permissions(con)
    con.commit()


def _rebuild_legacy(con: sqlite3.Connection) -> None:
    """Carry a development database across, keeping only what was final."""
    con.executescript("""
        ALTER TABLE gold_review_response RENAME TO gold_review_response_legacy;
        DROP INDEX IF EXISTS ux_gold_one_final_per_case;
        DROP INDEX IF EXISTS ix_gold_response_queue;
    """)
    con.executescript(_SCHEMA)
    legacy = _table_columns(con, "gold_review_response_legacy")
    now = utcnow()

    def src(col: str, fallback: str) -> str:
        return col if col in legacy else fallback

    # `finalized_at` is NOT NULL on the new table and was nullable on the old
    # one, so it is coalesced *in the INSERT*. Filling it afterwards would
    # arrive too late: the row would already have been dropped by the
    # constraint, and INSERT OR IGNORE would have hidden that.
    columns = ("queue_id, review_kind, verdict, correct_task_number, "
               "correct_task_unknown, reason_code, note, confidence, "
               "manual_checked, response_revision, reviewer_user_id, "
               "created_at, updated_at, finalized_at")
    select = (
        f"queue_id, review_kind, verdict, "
        f"{src('correct_task_number', 'NULL')}, "
        f"COALESCE({src('correct_task_unknown', '0')}, 0), "
        f"{src('reason_code', 'NULL')}, {src('note', 'NULL')}, "
        f"{src('confidence', 'NULL')}, "
        f"COALESCE({src('manual_checked', '0')}, 0), "
        f"COALESCE({src('response_revision', '1')}, 1), "
        f"{src('reviewer_user_id', 'NULL')}, "
        f"COALESCE({src('created_at', 'NULL')}, '{now}'), "
        f"COALESCE({src('updated_at', 'NULL')}, '{now}'), "
        f"COALESCE({src('finalized_at', 'NULL')}, "
        f"         {src('updated_at', 'NULL')}, '{now}')")
    con.execute(
        f"INSERT INTO gold_review_response({columns}) SELECT {select} "
        f"FROM gold_review_response_legacy "
        f"WHERE state='final' AND verdict IS NOT NULL")


def _grant_default_permissions(con: sqlite3.Connection) -> None:
    """Give `admin` the review permissions without disturbing other grants.

    `auth.seed` uses INSERT OR IGNORE, so an installed database never picks
    up a new permission from the ROLES map. This adds exactly the two and
    leaves every other grant as found.
    """
    try:
        rows = con.execute("SELECT id, name, permissions FROM role").fetchall()
    except sqlite3.Error:
        return
    for role_id, name, perms in rows:
        if name != "admin":
            continue
        held = [p.strip() for p in (perms or "").split(",") if p.strip()]
        added = [p for p in (GOLD_REVIEW_PERMISSION, GOLD_MANAGE_PERMISSION)
                 if p not in held]
        if added:
            con.execute("UPDATE role SET permissions=? WHERE id=?",
                        (",".join(held + added), role_id))


def permissions_for(con: sqlite3.Connection | None, user) -> frozenset[str]:
    """What this user may do, read from the `role` table.

    The table is the authority; the hardcoded `auth.ROLES` map is only the
    seed for a virgin database, and is consulted just when the row is absent.
    An unknown role yields nothing rather than everything.
    """
    if user is None:
        return frozenset()
    role = (getattr(user, "role", "") or "").strip()
    if not role:
        return frozenset()
    if con is not None:
        try:
            row = con.execute("SELECT permissions FROM role WHERE name=?",
                              (role,)).fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0]:
            return frozenset(p.strip() for p in row[0].split(",") if p.strip())
    from .ui.auth import ROLES
    return frozenset(p.strip() for p in ROLES.get(role, "").split(",") if p.strip())


def may_review(con: sqlite3.Connection | None, user) -> bool:
    return GOLD_REVIEW_PERMISSION in permissions_for(con, user)


def may_manage(con: sqlite3.Connection | None, user) -> bool:
    return GOLD_MANAGE_PERMISSION in permissions_for(con, user)


# ── models ───────────────────────────────────────────────────────────────
@dataclass
class Answer:
    """A current finalized answer, or a draft in progress.

    One shape for both, because the questionnaire form is the same either
    way; where it is stored is what differs, and that is the service's job.
    """

    queue_id: int
    review_kind: str = "primary"
    verdict: str | None = None
    correct_task_number: str | None = None
    correct_task_unknown: bool = False
    reason_code: str | None = None
    note: str | None = None
    confidence: str | None = None
    manual_checked: bool = False
    response_revision: int = 1
    reviewer_user_id: int | None = None
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""
    finalized_at: str | None = None
    is_final: bool = False

    def form_state(self) -> tuple:
        """The fields a reviewer can change. Used to detect real edits."""
        return (self.verdict, (self.correct_task_number or "").strip() or None,
                bool(self.correct_task_unknown), self.reason_code,
                (self.note or "").strip() or None, self.confidence,
                bool(self.manual_checked))


@dataclass
class ReviewPair:
    """One case as the reviewer is allowed to see it.

    There is deliberately no `stratum`, no score and no rank on this object:
    a field that does not exist cannot be rendered by mistake.
    """

    queue_id: int
    seq: int
    position: int
    total: int
    defect_id: int
    task_number: str
    defect_text: str = ""
    ata_ref: str = ""
    aircraft_model: str = ""
    task_title: str = ""
    task_body: str | None = None
    body_unavailable_reason: str = ""
    catalogue_only: bool = False
    manual_type: str = ""
    aircraft_type: str = ""
    revision: str = ""
    is_current: bool = True
    task_in_corpus: bool = False
    warnings: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    answer: Answer | None = None      # the current final answer, if any
    draft: Answer | None = None       # this reviewer's own draft, if any

    @property
    def has_body(self) -> bool:
        return bool(self.task_body and self.task_body.strip())

    @property
    def is_answered(self) -> bool:
        return self.answer is not None


@dataclass
class Progress:
    total: int = 0
    completed: int = 0
    drafts: int = 0
    yes: int = 0
    no: int = 0
    partial: int = 0
    unsure: int = 0
    per_chapter: list[tuple[str, int, int]] = field(default_factory=list)
    last_saved: str = ""
    frozen: bool = False
    release_version: int | None = None

    @property
    def remaining(self) -> int:
        return max(self.total - self.completed, 0)

    @property
    def pct(self) -> float:
        return (self.completed / self.total * 100.0) if self.total else 0.0

    @property
    def unsure_pct(self) -> float:
        return (self.unsure / self.completed * 100.0) if self.completed else 0.0


def _to_answer(row, *, final: bool) -> Answer:
    keys = row.keys()

    def get(name, default=None):
        return row[name] if name in keys else default

    return Answer(
        id=get("id"), queue_id=get("queue_id"), review_kind=get("review_kind"),
        verdict=get("verdict"), correct_task_number=get("correct_task_number"),
        correct_task_unknown=bool(get("correct_task_unknown", 0)),
        reason_code=get("reason_code"), note=get("note"),
        confidence=get("confidence"),
        manual_checked=bool(get("manual_checked", 0)),
        response_revision=get("response_revision", 1) or 1,
        reviewer_user_id=get("reviewer_user_id"),
        created_at=get("created_at", "") or "",
        updated_at=get("updated_at", "") or "",
        finalized_at=get("finalized_at"), is_final=final)


# ── validation ───────────────────────────────────────────────────────────
def validate(con: sqlite3.Connection, answer: Answer, *, final: bool) -> list[str]:
    """Every rule that decides whether an answer may be stored.

    Called by the service before any write and directly by the UI to decide
    what to enable, so a disabled button and a refused write cannot disagree.
    Drafts are checked loosely on purpose: a half-finished thought must be
    storable, it just may not become an answer.
    """
    problems: list[str] = []
    v = answer.verdict

    if v is not None and v not in VERDICTS:
        problems.append(f"verdict must be one of {', '.join(VERDICTS)}")
    if answer.review_kind not in REVIEW_KINDS:
        problems.append("review kind must be primary or professional")
    if answer.confidence is not None and answer.confidence not in CONFIDENCES:
        problems.append("confidence must be low, medium or high")

    correct = (answer.correct_task_number or "").strip()
    if correct and answer.correct_task_unknown:
        problems.append("a correct task number and 'not identifiable in the "
                        "corpus' cannot both be set")
    if correct and not task_exists(con, correct):
        problems.append(f"task {correct} is not in the corpus — select one from "
                        f"the task list, or tick 'the correct task cannot be "
                        f"identified'")
    if v == "yes" and (correct or answer.correct_task_unknown):
        problems.append("a Yes verdict cannot also carry a correction")

    if not final:
        return problems

    if not answer.reviewer_user_id:
        problems.append("a finalized answer requires an authenticated reviewer")
    if v not in VERDICTS:
        problems.append("choose Yes, No, Partial or Unsure before finalizing")
    if v in ("no", "partial"):
        if not correct and not answer.correct_task_unknown:
            problems.append("a No or Partial verdict needs the correct task "
                            "number, or 'the correct task cannot be identified "
                            "from the available corpus'")
        if answer.reason_code not in WRONG_REASON_CODES:
            problems.append("choose a reason for rejecting this pairing")
    if v == "unsure" and answer.reason_code not in UNSURE_REASON_CODES:
        problems.append("an Unsure verdict needs a reason")
    return problems


def task_exists(con: sqlite3.Connection, task_number: str) -> bool:
    return con.execute(
        "SELECT 1 FROM task WHERE UPPER(TRIM(task_number))=UPPER(TRIM(?)) LIMIT 1",
        (task_number or "",)).fetchone() is not None


# ── the service ──────────────────────────────────────────────────────────
class GoldReviewService:
    """Read the queue, store answers, keep `label_gold` in step."""

    def __init__(self, con: sqlite3.Connection, user=None,
                 review_kind: str = "primary"):
        if review_kind not in REVIEW_KINDS:
            raise ValueError(f"review_kind must be one of {REVIEW_KINDS}")
        self.con = con
        self.user = user
        self.review_kind = review_kind
        self.con.row_factory = sqlite3.Row

    # ── availability and authority ────────────────────────────────────
    @property
    def queue_exists(self) -> bool:
        return self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gold_queue'"
        ).fetchone() is not None

    def require_queue(self) -> None:
        if not self.queue_exists:
            raise QueueMissing(
                "This database has no gold-set queue. It is built by "
                "scripts/build_gold_queue.py as part of corpus preparation.")

    @property
    def user_id(self) -> int | None:
        return getattr(self.user, "id", None)

    def authorised(self) -> bool:
        return may_review(self.con, self.user)

    def may_manage(self) -> bool:
        return may_manage(self.con, self.user)

    def require_authority(self) -> None:
        if not self.authorised():
            who = getattr(self.user, "username", None) or "this account"
            raise NotAuthorised(
                f"{who} does not hold the '{GOLD_REVIEW_PERMISSION}' "
                f"permission. An administrator grants it on the role.")

    # ── release state ─────────────────────────────────────────────────
    def current_release(self):
        try:
            return self.con.execute(
                "SELECT * FROM gold_set_release ORDER BY version DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return None

    def is_frozen(self) -> bool:
        row = self.current_release()
        return bool(row and row["status"] == "frozen")

    def require_open(self) -> None:
        if self.is_frozen():
            raise SetFrozen(
                "This gold set is finalized and frozen. Someone with gold-set "
                "management authority must reopen it, with a recorded reason, "
                "before answers can change.")

    # ── navigation ────────────────────────────────────────────────────
    def sequences(self) -> list[int]:
        self.require_queue()
        return [r[0] for r in
                self.con.execute("SELECT seq FROM gold_queue ORDER BY seq")]

    def position_of(self, seq: int) -> int:
        """1-based human position. `seq` starts at 0 in the real queue, and
        'Case 0 of 400' is not something to show a person."""
        row = self.con.execute(
            "SELECT COUNT(*) FROM gold_queue WHERE seq<=?", (seq,)).fetchone()
        return int(row[0]) if row else 0

    def resume_seq(self) -> int | None:
        self.require_queue()
        row = self.con.execute(
            "SELECT q.seq FROM gold_queue q WHERE NOT EXISTS ("
            "  SELECT 1 FROM gold_review_response r WHERE r.queue_id=q.id"
            "    AND r.review_kind=?) ORDER BY q.seq LIMIT 1",
            (self.review_kind,)).fetchone()
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

    def next_unanswered_seq(self, after: int | None = None) -> int | None:
        sql = ("SELECT q.seq FROM gold_queue q WHERE NOT EXISTS ("
               "  SELECT 1 FROM gold_review_response r WHERE r.queue_id=q.id"
               "    AND r.review_kind=?)")
        params: list = [self.review_kind]
        if after is not None:
            sql += " AND q.seq>?"
            params.append(after)
        sql += " ORDER BY q.seq LIMIT 1"
        row = self.con.execute(sql, params).fetchone()
        if row:
            return row[0]
        return self.resume_seq() if after is not None else None

    def filtered_sequences(self, kind: str = "all") -> list[int]:
        base = ("SELECT q.seq FROM gold_queue q "
                "LEFT JOIN gold_review_response r ON r.queue_id=q.id "
                "  AND r.review_kind=? ")
        params: list = [self.review_kind]
        if kind == "unanswered":
            base += "WHERE r.id IS NULL "
        elif kind == "completed":
            base += "WHERE r.id IS NOT NULL "
        elif kind in VERDICTS:
            base += "WHERE r.verdict=? "
            params.append(kind)
        base += "ORDER BY q.seq"
        return [r[0] for r in self.con.execute(base, params)]

    # ── one case ──────────────────────────────────────────────────────
    def pair(self, seq: int) -> ReviewPair | None:
        self.require_queue()
        row = self.con.execute("""
            SELECT q.id, q.seq, q.defect_id, q.task_number,
                   d.defect_text, d.ata_ref, d.sdr_id
            FROM gold_queue q LEFT JOIN defect d ON d.id = q.defect_id
            WHERE q.seq = ?
        """, (seq,)).fetchone()
        if row is None:
            return None

        total = self.con.execute("SELECT COUNT(*) FROM gold_queue").fetchone()[0]
        pair = ReviewPair(
            queue_id=row["id"], seq=row["seq"], position=self.position_of(seq),
            total=int(total), defect_id=row["defect_id"],
            task_number=row["task_number"], defect_text=row["defect_text"] or "",
            ata_ref=row["ata_ref"] or "")

        if row["sdr_id"] is not None:
            m = self.con.execute("SELECT aircraft_model FROM sdr_raw WHERE id=?",
                                 (row["sdr_id"],)).fetchone()
            if m and m[0]:
                pair.aircraft_model = m[0]

        task = self.con.execute("""
            SELECT t.id, t.title, t.body, t.catalogue_only,
                   m.manual_type, m.revision, m.is_current, m.aircraft_type
            FROM task t JOIN manual m ON m.id = t.manual_id
            WHERE UPPER(TRIM(t.task_number)) = UPPER(TRIM(?))
            ORDER BY m.is_current DESC LIMIT 1
        """, (pair.task_number,)).fetchone()
        if task:
            pair.task_in_corpus = True
            pair.task_title = task["title"] or ""
            pair.task_body = task["body"]
            pair.catalogue_only = bool(task["catalogue_only"])
            pair.manual_type = task["manual_type"] or ""
            pair.revision = task["revision"] or ""
            pair.is_current = bool(task["is_current"])
            pair.aircraft_type = task["aircraft_type"] or ""
            for sec in self.con.execute(
                    "SELECT kind, text FROM task_section WHERE task_id=? "
                    "AND kind IN ('warning','caution') ORDER BY seq", (task["id"],)):
                (pair.warnings if sec["kind"] == "warning"
                 else pair.cautions).append(sec["text"])
            pair.body_unavailable_reason = _body_reason(pair)
        else:
            pair.body_unavailable_reason = (
                "This task number is not held in the corpus at all, so no "
                "title, revision or procedure can be shown. That is a fact "
                "about coverage — it is not evidence that the pairing is wrong.")

        pair.answer = self.current_answer(pair.queue_id)
        pair.draft = self.draft_for(pair.queue_id)
        return pair

    def current_answer(self, queue_id: int) -> Answer | None:
        """The finalized answer of *this* review kind. A professional review
        never loads the primary verdict, and vice versa — the query is scoped
        to `review_kind`, so the other review is not merely hidden, it is
        never read."""
        row = self.con.execute(
            "SELECT * FROM gold_review_response WHERE queue_id=? AND review_kind=?",
            (queue_id, self.review_kind)).fetchone()
        return _to_answer(row, final=True) if row else None

    def draft_for(self, queue_id: int) -> Answer | None:
        row = self.con.execute(
            "SELECT * FROM gold_review_draft WHERE queue_id=? AND review_kind=? "
            "AND reviewer_user_id=?",
            (queue_id, self.review_kind, self.user_id or -1)).fetchone()
        return _to_answer(row, final=False) if row else None

    # ── deterministic task search, for corrections ────────────────────
    def search_tasks(self, *, number: str = "", title: str = "",
                     chapter: str = "", manual_type: str = "",
                     limit: int = 200) -> list[dict]:
        """Plain SQL over `task`. No embeddings, no model, no ranking.

        A correction is evidence about what the right answer *is*; letting a
        retrieval engine propose it would fold the system under test back
        into its own reference set.
        """
        sql = ["SELECT t.task_number, t.title, t.ata_chapter, t.catalogue_only,"
               "       m.manual_type, m.revision, m.is_current, m.aircraft_type",
               "FROM task t JOIN manual m ON m.id = t.manual_id WHERE 1=1"]
        params: list = []
        if number.strip():
            sql.append("AND UPPER(t.task_number) LIKE ?")
            params.append(f"%{number.strip().upper()}%")
        if title.strip():
            sql.append("AND UPPER(COALESCE(t.title,'')) LIKE ?")
            params.append(f"%{title.strip().upper()}%")
        if chapter.strip():
            sql.append("AND t.ata_chapter = ?")
            params.append(chapter.strip())
        if manual_type.strip():
            sql.append("AND UPPER(m.manual_type) = ?")
            params.append(manual_type.strip().upper())
        sql.append("ORDER BY t.task_number LIMIT ?")
        params.append(int(limit))
        return [dict(r) for r in self.con.execute(" ".join(sql), params)]

    def chapters(self) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT DISTINCT ata_chapter FROM task WHERE ata_chapter IS NOT NULL "
            "ORDER BY ata_chapter") if r[0]]

    def manual_types(self) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT DISTINCT manual_type FROM manual WHERE manual_type IS NOT NULL "
            "ORDER BY manual_type") if r[0]]

    # ── transactions ──────────────────────────────────────────────────
    @contextmanager
    def _tx(self, name: str = "goldreview"):
        """A savepoint, so this composes instead of fighting the caller.

        The first version wrapped `BEGIN IMMEDIATE` in a bare
        `except sqlite3.OperationalError: pass`, which swallowed genuine
        `database is locked` errors and could commit a transaction the caller
        had already opened. A savepoint nests correctly and a lock error
        propagates.
        """
        self.con.execute("PRAGMA foreign_keys=ON")
        outermost = not self.con.in_transaction
        self.con.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self.con.execute(f"ROLLBACK TO {name}")
            self.con.execute(f"RELEASE {name}")
            if outermost:
                self.con.rollback()
            raise
        else:
            self.con.execute(f"RELEASE {name}")
            if outermost:
                self.con.commit()

    # ── drafts ────────────────────────────────────────────────────────
    def save_draft(self, queue_id: int, **fields) -> Answer:
        """Store work in progress.

        Structurally incapable of touching an answer: it writes one row of
        `gold_review_draft` and nothing else.
        """
        self.require_authority()
        self.require_open()
        self._require_case(queue_id)
        draft = self._merge(queue_id, fields, base=self.draft_for(queue_id))
        problems = validate(self.con, draft, final=False)
        if problems:
            raise ValidationFailed(problems)
        now = utcnow()
        with self._tx("gold_draft"):
            row = self.con.execute(
                "SELECT id, created_at FROM gold_review_draft WHERE queue_id=? "
                "AND review_kind=? AND reviewer_user_id=?",
                (queue_id, self.review_kind, self.user_id)).fetchone()
            values = (draft.verdict, draft.correct_task_number,
                      1 if draft.correct_task_unknown else 0, draft.reason_code,
                      draft.note, draft.confidence,
                      1 if draft.manual_checked else 0, now)
            if row:
                draft.id, draft.created_at = row["id"], row["created_at"]
                self.con.execute(
                    "UPDATE gold_review_draft SET verdict=?, "
                    "correct_task_number=?, correct_task_unknown=?, "
                    "reason_code=?, note=?, confidence=?, manual_checked=?, "
                    "updated_at=? WHERE id=?", values + (draft.id,))
            else:
                cur = self.con.execute(
                    "INSERT INTO gold_review_draft(queue_id, review_kind, "
                    "reviewer_user_id, verdict, correct_task_number, "
                    "correct_task_unknown, reason_code, note, confidence, "
                    "manual_checked, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (queue_id, self.review_kind, self.user_id) + values[:7]
                    + (now, now))
                draft.id, draft.created_at = cur.lastrowid, now
        draft.updated_at = now
        return draft

    def discard_draft(self, queue_id: int) -> None:
        """Drop this reviewer's draft. An answer is never touched here."""
        self.require_authority()
        with self._tx("gold_discard"):
            self.con.execute(
                "DELETE FROM gold_review_draft WHERE queue_id=? AND "
                "review_kind=? AND reviewer_user_id=?",
                (queue_id, self.review_kind, self.user_id or -1))

    # ── finalizing ────────────────────────────────────────────────────
    def finalize(self, queue_id: int, *, change_reason: str | None = None,
                 **fields) -> Answer:
        """Make this the answer, atomically.

        Everything moves together or nothing does: history, the current
        answer, this reviewer's draft, the `label_gold` projection, the queue
        flag, and the audit entry. The audit row is written *inside* the
        transaction — logging after a commit would permit a durable answer
        with no record of who made it.
        """
        self.require_authority()
        self.require_open()
        self._require_case(queue_id)
        existing = self.current_answer(queue_id)
        answer = self._merge(queue_id, fields, base=existing)
        answer.reviewer_user_id = self.user_id
        problems = validate(self.con, answer, final=True)
        if problems:
            raise ValidationFailed(problems)

        revising = existing is not None
        if revising and not (change_reason or "").strip():
            raise ValidationFailed(
                ["revising an existing answer requires a change reason"])
        previous = asdict(existing) if existing else None
        answer.response_revision = ((existing.response_revision or 1) + 1
                                    if revising else 1)
        answer.finalized_at = utcnow()

        with self._tx("gold_final"):
            self._upsert_answer(answer, existing)
            self._snapshot(answer, existing, previous, change_reason)
            self.con.execute(
                "DELETE FROM gold_review_draft WHERE queue_id=? AND "
                "review_kind=? AND reviewer_user_id=?",
                (queue_id, self.review_kind, self.user_id or -1))
            if self.review_kind == "primary":
                self._project_to_label_gold(answer)
                self.con.execute("UPDATE gold_queue SET done=1 WHERE id=?",
                                 (queue_id,))
            # No narrative text ever reaches the chain: it records that a
            # judgement happened, never the judgement's prose.
            audit.log(self.con,
                      "gold_review_revised" if revising else "gold_review_finalized",
                      user_id=self.user_id, entity="gold_queue",
                      entity_id=str(queue_id), commit=False,
                      payload={"review_kind": self.review_kind,
                               "revision": answer.response_revision,
                               "verdict": answer.verdict,
                               "correction_supplied":
                                   bool(answer.correct_task_number),
                               "correct_task_unknown":
                                   answer.correct_task_unknown,
                               "previous_reviewer":
                                   existing.reviewer_user_id if existing else None})
        return answer

    def _require_case(self, queue_id: int) -> None:
        if not self.con.execute("SELECT 1 FROM gold_queue WHERE id=?",
                                (queue_id,)).fetchone():
            raise GoldReviewError(f"no queue case with id {queue_id}")

    def _merge(self, queue_id: int, fields: dict, base: Answer | None) -> Answer:
        merged = Answer(queue_id=queue_id, review_kind=self.review_kind,
                        reviewer_user_id=self.user_id)
        if base is not None:
            merged = Answer(**{**asdict(base), "reviewer_user_id": self.user_id})
        for key, value in fields.items():
            if key not in DRAFT_FIELDS:
                raise GoldReviewError(f"unknown response field {key!r}")
            setattr(merged, key, value)
        # The UI may not move a case onto a different defect or task.
        merged.queue_id = queue_id
        merged.review_kind = self.review_kind
        if merged.correct_task_number is not None:
            merged.correct_task_number = merged.correct_task_number.strip() or None
        return merged

    def _upsert_answer(self, a: Answer, existing: Answer | None) -> None:
        now = utcnow()
        values = (a.verdict, a.correct_task_number,
                  1 if a.correct_task_unknown else 0, a.reason_code, a.note,
                  a.confidence, 1 if a.manual_checked else 0,
                  a.response_revision, a.reviewer_user_id, now, a.finalized_at)
        if existing is not None and existing.id:
            a.id, a.created_at = existing.id, existing.created_at
            self.con.execute(
                "UPDATE gold_review_response SET verdict=?, correct_task_number=?,"
                " correct_task_unknown=?, reason_code=?, note=?, confidence=?,"
                " manual_checked=?, response_revision=?, reviewer_user_id=?,"
                " updated_at=?, finalized_at=? WHERE id=?", values + (a.id,))
        else:
            cur = self.con.execute(
                "INSERT INTO gold_review_response(queue_id, review_kind, verdict,"
                " correct_task_number, correct_task_unknown, reason_code, note,"
                " confidence, manual_checked, response_revision, reviewer_user_id,"
                " created_at, updated_at, finalized_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.queue_id, a.review_kind) + values[:9] + (now, now,
                                                            a.finalized_at))
            a.id, a.created_at = cur.lastrowid, now
        a.updated_at = now

    def _snapshot(self, a: Answer, existing: Answer | None,
                  previous: dict | None, change_reason: str | None) -> None:
        self.con.execute(
            "INSERT INTO gold_review_history(response_id, queue_id, review_kind,"
            " revision, snapshot, previous_snapshot, reviewer_user_id,"
            " previous_reviewer_user_id, changed_by_user_id, changed_at,"
            " change_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (a.id, a.queue_id, a.review_kind, a.response_revision,
             json.dumps(asdict(a), sort_keys=True, default=str),
             json.dumps(previous, sort_keys=True, default=str) if previous else None,
             a.reviewer_user_id,
             existing.reviewer_user_id if existing else None,
             self.user_id, utcnow(), change_reason))

    def _project_to_label_gold(self, a: Answer) -> None:
        """Keep the old table truthful for the evaluation scripts.

        `label_gold` predates this feature and every eval script reads it, so
        it is maintained as a projection of the current finalized primary
        answers. The delete is scoped to the one pair being written, and the
        revision it replaces is already in `gold_review_history`.
        """
        q = self.con.execute(
            "SELECT defect_id, task_number FROM gold_queue WHERE id=?",
            (a.queue_id,)).fetchone()
        if q is None:
            raise GoldReviewError(f"no queue case with id {a.queue_id}")
        self.con.execute(
            "DELETE FROM label_gold WHERE defect_id=? AND task_number=?",
            (q["defect_id"], q["task_number"]))
        self.con.execute(
            "INSERT INTO label_gold(defect_id, task_number, verdict,"
            " correct_task_number, note, adjudicated_at) VALUES(?,?,?,?,?,?)",
            (q["defect_id"], q["task_number"], a.verdict,
             a.correct_task_number, a.note, a.finalized_at or utcnow()))

    def history(self, queue_id: int) -> list[dict]:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM gold_review_history WHERE queue_id=? AND review_kind=?"
            " ORDER BY revision, id", (queue_id, self.review_kind))]

    # ── progress ──────────────────────────────────────────────────────
    def progress(self) -> Progress:
        self.require_queue()
        total = self.con.execute("SELECT COUNT(*) FROM gold_queue").fetchone()[0]
        counts = {v: 0 for v in VERDICTS}
        completed = 0
        for row in self.con.execute(
                "SELECT verdict, COUNT(*) FROM gold_review_response "
                "WHERE review_kind=? GROUP BY verdict", (self.review_kind,)):
            if row[0] in counts:
                counts[row[0]] = int(row[1])
            completed += int(row[1])
        drafts = self.con.execute(
            "SELECT COUNT(*) FROM gold_review_draft WHERE review_kind=?",
            (self.review_kind,)).fetchone()[0]
        per_chapter = [
            (r[0] or "—", int(r[1]), int(r[2])) for r in self.con.execute(
                "SELECT substr(q.stratum,1,instr(q.stratum,'|')-1) AS ch, "
                "  SUM(CASE WHEN r.id IS NOT NULL THEN 1 ELSE 0 END), COUNT(*) "
                "FROM gold_queue q LEFT JOIN gold_review_response r "
                "  ON r.queue_id=q.id AND r.review_kind=? "
                "GROUP BY ch ORDER BY ch", (self.review_kind,))]
        last = self.con.execute(
            "SELECT MAX(t) FROM (SELECT MAX(updated_at) AS t FROM "
            "gold_review_response WHERE review_kind=? UNION ALL SELECT "
            "MAX(updated_at) FROM gold_review_draft WHERE review_kind=?)",
            (self.review_kind, self.review_kind)).fetchone()[0] or ""
        release = self.current_release()
        return Progress(total=int(total), completed=completed, drafts=int(drafts),
                        yes=counts["yes"], no=counts["no"],
                        partial=counts["partial"], unsure=counts["unsure"],
                        per_chapter=per_chapter, last_saved=last,
                        frozen=self.is_frozen(),
                        release_version=release["version"] if release else None)

    # ── freezing ──────────────────────────────────────────────────────
    def readiness(self, expected: int = EXPECTED_GOLD_CASES) -> list[str]:
        """Everything that would make freezing this set a lie. Empty = ready.

        A frozen release is a claim that a specific 400-case dataset was
        completely and consistently judged. The checks below are what makes
        that claim true rather than decorative.
        """
        problems: list[str] = []
        self.require_queue()
        con = self.con

        total, ids, seqs = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT id), COUNT(DISTINCT seq) "
            "FROM gold_queue").fetchone()
        if total != expected:
            problems.append(f"gold_queue holds {total} cases, expected {expected}")
        if ids != expected:
            problems.append(f"gold_queue has {ids} distinct ids, expected {expected}")
        if seqs != expected:
            problems.append(f"gold_queue has {seqs} distinct seq values, "
                            f"expected {expected}")

        missing_defect = con.execute(
            "SELECT COUNT(*) FROM gold_queue q WHERE NOT EXISTS ("
            "  SELECT 1 FROM defect d WHERE d.id=q.defect_id)").fetchone()[0]
        if missing_defect:
            problems.append(f"{missing_defect} queue rows point at a missing defect")

        finals = con.execute(
            "SELECT COUNT(*) FROM gold_review_response WHERE review_kind='primary'"
        ).fetchone()[0]
        if finals != expected:
            problems.append(f"{finals} finalized primary answers, expected {expected}")
        unanswered = con.execute(
            "SELECT COUNT(*) FROM gold_queue q WHERE NOT EXISTS ("
            "  SELECT 1 FROM gold_review_response r WHERE r.queue_id=q.id "
            "  AND r.review_kind='primary')").fetchone()[0]
        if unanswered:
            problems.append(f"{unanswered} cases have no finalized answer")
        dupes = con.execute(
            "SELECT COUNT(*) FROM (SELECT queue_id FROM gold_review_response "
            "WHERE review_kind='primary' GROUP BY queue_id HAVING COUNT(*)>1)"
        ).fetchone()[0]
        if dupes:
            problems.append(f"{dupes} cases carry more than one current answer")

        drafts = con.execute(
            "SELECT COUNT(*) FROM gold_review_draft WHERE review_kind='primary'"
        ).fetchone()[0]
        if drafts:
            problems.append(f"{drafts} unresolved draft(s) remain")

        for row in con.execute(
                "SELECT * FROM gold_review_response WHERE review_kind='primary'"):
            a = _to_answer(row, final=True)
            bad = validate(con, a, final=True)
            if bad:
                problems.append(f"case {a.queue_id}: {bad[0]}")

        lg_total = con.execute("SELECT COUNT(*) FROM label_gold").fetchone()[0]
        if lg_total != expected:
            problems.append(f"label_gold holds {lg_total} rows, expected {expected}")
        mismatch = con.execute("""
            SELECT COUNT(*) FROM gold_review_response r
            JOIN gold_queue q ON q.id = r.queue_id
            LEFT JOIN label_gold g ON g.defect_id = q.defect_id
                                  AND g.task_number = q.task_number
            WHERE r.review_kind='primary'
              AND (g.id IS NULL OR g.verdict IS NOT r.verdict
                   OR COALESCE(g.correct_task_number,'')
                      IS NOT COALESCE(r.correct_task_number,''))
        """).fetchone()[0]
        if mismatch:
            problems.append(f"{mismatch} label_gold rows do not match the "
                            f"current answer")
        orphan = con.execute(
            "SELECT COUNT(*) FROM label_gold g WHERE NOT EXISTS ("
            "  SELECT 1 FROM gold_queue q WHERE q.defect_id=g.defect_id "
            "  AND q.task_number=g.task_number)").fetchone()[0]
        if orphan:
            problems.append(f"{orphan} label_gold rows do not map to a queue case")

        flag_mismatch = con.execute(
            "SELECT COUNT(*) FROM gold_queue q LEFT JOIN gold_review_response r "
            "  ON r.queue_id=q.id AND r.review_kind='primary' "
            "WHERE q.done <> (CASE WHEN r.id IS NULL THEN 0 ELSE 1 END)"
        ).fetchone()[0]
        if flag_mismatch:
            problems.append(f"{flag_mismatch} gold_queue.done flags disagree with "
                            f"the answers")
        done_sum = con.execute(
            "SELECT COALESCE(SUM(done),0) FROM gold_queue").fetchone()[0]
        if done_sum != expected:
            problems.append(f"SUM(gold_queue.done) is {done_sum}, expected {expected}")

        try:
            fk = con.execute("PRAGMA foreign_key_check").fetchall()
            bad_fk = [r for r in fk if r[0] in (
                "gold_review_response", "gold_review_draft", "gold_queue",
                "label_gold")]
            if bad_fk:
                problems.append(f"{len(bad_fk)} foreign-key violations in the "
                                f"gold tables")
        except sqlite3.Error as exc:
            problems.append(f"foreign-key check failed: {exc}")
        return problems

    def fingerprints(self) -> tuple[str, str]:
        """Deterministic digests of the queue and of the released judgement."""
        return queue_fingerprint(self.con), response_fingerprint(self.con)

    def freeze(self, expected: int = EXPECTED_GOLD_CASES) -> int:
        """Release this version as held-out evaluation data."""
        if not self.may_manage():
            raise NotAuthorised(
                f"Freezing the gold set requires the "
                f"'{GOLD_MANAGE_PERMISSION}' permission.")
        problems = self.readiness(expected)
        if problems:
            raise ValidationFailed(problems)
        if self.is_frozen():
            raise SetFrozen("This gold set is already frozen.")
        qf, rf = self.fingerprints()
        nxt = int(self.con.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM gold_set_release").fetchone()[0])
        with self._tx("gold_freeze"):
            self.con.execute(
                "INSERT INTO gold_set_release(version, case_count, "
                "queue_fingerprint, response_fingerprint, finalized_by, "
                "finalized_at, status) VALUES(?,?,?,?,?,?, 'frozen')",
                (nxt, expected, qf, rf, self.user_id, utcnow()))
            audit.log(self.con, "gold_set_finalized", user_id=self.user_id,
                      entity="gold_set_release", entity_id=str(nxt), commit=False,
                      payload={"queue_fingerprint": qf,
                               "response_fingerprint": rf,
                               "case_count": expected})
        return nxt

    def reopen(self, reason: str) -> None:
        """Unfreeze. Management authority plus a recorded reason, or nothing.

        The frozen release row is kept: its version and fingerprints stay
        readable, so a later release can be compared against what it replaced.
        """
        if not self.may_manage():
            raise NotAuthorised(
                f"Reopening a finalized gold set requires the "
                f"'{GOLD_MANAGE_PERMISSION}' permission.")
        if not (reason or "").strip():
            raise ValidationFailed(["a reason is required to reopen the set"])
        row = self.current_release()
        if row is None or row["status"] != "frozen":
            raise GoldReviewError("no frozen gold set to reopen")
        with self._tx("gold_reopen"):
            self.con.execute(
                "UPDATE gold_set_release SET status='reopened', unlock_reason=?,"
                " reopened_by=?, reopened_at=? WHERE id=?",
                (reason.strip(), self.user_id, utcnow(), row["id"]))
            audit.log(self.con, "gold_set_reopened", user_id=self.user_id,
                      entity="gold_set_release", entity_id=str(row["version"]),
                      commit=False, payload={"reason_supplied": True})


def queue_fingerprint(con: sqlite3.Connection) -> str:
    """Digest of the 400 pairs themselves.

    Module-level and independent of the questionnaire tables, so a migration
    can be fingerprinted before and after with the same function — which is
    the only way that comparison proves anything.
    """
    rows = con.execute(
        "SELECT id, seq, defect_id, task_number, stratum FROM gold_queue "
        "ORDER BY id").fetchall()
    return hashlib.sha256(
        json.dumps([list(r) for r in rows], sort_keys=True).encode()).hexdigest()


def response_fingerprint(con: sqlite3.Connection) -> str:
    """Digest of the released judgement. Empty before any answer exists."""
    try:
        rows = con.execute(
            "SELECT queue_id, verdict, COALESCE(correct_task_number,''), "
            "correct_task_unknown, COALESCE(reason_code,''), response_revision "
            "FROM gold_review_response WHERE review_kind='primary' "
            "ORDER BY queue_id").fetchall()
    except sqlite3.Error:
        rows = []
    return hashlib.sha256(
        json.dumps([list(r) for r in rows], sort_keys=True).encode()).hexdigest()


def _body_reason(pair: ReviewPair) -> str:
    """Say exactly why there is no procedure. A missing body is a coverage
    fact, never evidence that a pairing is wrong."""
    if pair.has_body:
        return ""
    if pair.catalogue_only:
        return ("This row is catalogue only — the manual lists the task but the "
                "procedure text is not held in this corpus. Judge the pairing "
                "on the task number and title. A missing procedure is not "
                "evidence that the task is wrong.")
    return ("No procedure text is held for this task. Where the source is a "
            "DRM-protected manual the body cannot be extracted, and that is a "
            "limit of the corpus rather than a fault in the pairing.")


# ── the professional overlap subset ──────────────────────────────────────
PRO_SUBSET_SIZE = 50


def professional_subset(con: sqlite3.Connection, size: int = PRO_SUBSET_SIZE
                        ) -> list[int]:
    """The deterministic, stratified subset for the second independent review.

    Stratified on the queue's own three-part stratum — ATA chapter, the
    diagnostic/action function class, and the narrative-length tercile — not
    on chapter alone, so the overlap sample keeps the shape of the set it is
    drawn from. Largest-remainder allocation gives every stratum its
    proportional share and hands the leftovers to the largest remainders, so
    thin strata are not silently wiped out.

    Derived from the queue alone, never from anybody's answers, so the two
    reviews cannot influence each other and the same subset comes back on
    every machine without storing a list.
    """
    rows = con.execute(
        "SELECT id, stratum, defect_id, task_number FROM gold_queue "
        "ORDER BY id").fetchall()
    if not rows or size <= 0:
        return []
    size = min(size, len(rows))

    buckets: dict[str, list[tuple[str, int]]] = {}
    for qid, stratum, defect_id, task_number in rows:
        key = stratum or "—"
        digest = hashlib.sha256(f"{defect_id}|{task_number}".encode()).hexdigest()
        buckets.setdefault(key, []).append((digest, qid))
    for bucket in buckets.values():
        bucket.sort()

    total = len(rows)
    exact = {k: len(v) * size / total for k, v in buckets.items()}
    quota = {k: int(v) for k, v in exact.items()}
    # Largest remainder, with the stratum key as a deterministic tie-break.
    remaining = size - sum(quota.values())
    order = sorted(buckets, key=lambda k: (-(exact[k] - quota[k]), k))
    for key in order:
        if remaining <= 0:
            break
        if quota[key] < len(buckets[key]):
            quota[key] += 1
            remaining -= 1
    while remaining > 0:                       # strata exhausted; top up stably
        progressed = False
        for key in sorted(buckets):
            if remaining > 0 and quota[key] < len(buckets[key]):
                quota[key] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break

    chosen: list[int] = []
    for key in sorted(buckets):
        chosen.extend(qid for _digest, qid in buckets[key][:quota[key]])
    return sorted(chosen)


# ── evaluation semantics ─────────────────────────────────────────────────
# One loader, so no two scripts can disagree about what an answer means.
#
# The conservative reading is deliberate. `partial` means "related, but not
# the right entry point" — on a maintenance tool that is a wrong answer with
# a sympathetic explanation, so the strict safety metric counts it as
# incorrect while it is also reported on its own. `unsure` is excluded from
# correctness entirely and its rate is always reported: converting it to a
# failure would punish the reviewer for being honest, and converting it to a
# pass would launder a non-answer into a success.
GOLD_CORRECT = ("yes",)
GOLD_INCORRECT = ("no", "partial")
GOLD_EXCLUDED = ("unsure",)


@dataclass
class GoldLabel:
    """One finalized primary answer, as evaluation is allowed to see it."""

    queue_id: int
    defect_id: int
    task_number: str
    verdict: str
    correct_task_number: str | None = None
    correct_task_unknown: bool = False

    @property
    def is_correct(self) -> bool:
        return self.verdict in GOLD_CORRECT

    @property
    def is_incorrect(self) -> bool:
        return self.verdict in GOLD_INCORRECT

    @property
    def scorable(self) -> bool:
        return self.verdict not in GOLD_EXCLUDED


@dataclass
class GoldScoring:
    total: int = 0
    correct: int = 0
    incorrect: int = 0
    partial: int = 0
    excluded: int = 0

    @property
    def scored(self) -> int:
        return self.correct + self.incorrect

    @property
    def precision(self) -> float | None:
        """Strict: `partial` counts against. None when nothing is scorable."""
        return (self.correct / self.scored) if self.scored else None

    @property
    def unsure_rate(self) -> float:
        return (self.excluded / self.total) if self.total else 0.0


def load_gold_labels(con: sqlite3.Connection) -> list[GoldLabel]:
    """Finalized **primary** answers only.

    Drafts live in `gold_review_draft` and never appear here; professional
    overlap answers are a separate review kind and are filtered out. Reading
    from `gold_review_response` rather than `label_gold` is what makes both
    distinctions expressible at all — the old table records neither.
    """
    try:
        rows = con.execute(
            "SELECT r.queue_id, q.defect_id, q.task_number, r.verdict, "
            "       r.correct_task_number, r.correct_task_unknown "
            "FROM gold_review_response r JOIN gold_queue q ON q.id=r.queue_id "
            "WHERE r.review_kind='primary' ORDER BY q.seq").fetchall()
    except sqlite3.Error:
        return []
    return [GoldLabel(queue_id=r[0], defect_id=r[1], task_number=r[2],
                      verdict=r[3], correct_task_number=r[4],
                      correct_task_unknown=bool(r[5]))
            for r in rows if r[3] in VERDICTS]


def score_gold(labels: list[GoldLabel]) -> GoldScoring:
    s = GoldScoring(total=len(labels))
    for lab in labels:
        if not lab.scorable:
            s.excluded += 1
        elif lab.is_correct:
            s.correct += 1
        else:
            s.incorrect += 1
            if lab.verdict == "partial":
                s.partial += 1
    return s


def agreement(primary: dict[int, str], professional: dict[int, str]
              ) -> dict[str, float | int]:
    """Raw and chance-corrected agreement over the cases both reviews finalized.

    Cohen's kappa, because the two reviewers are fixed raters on the same
    nominal scale. Reported alongside raw agreement, never instead of it: on
    a set this skewed a high raw agreement with a low kappa is the normal
    result and both halves are needed to read it honestly.
    """
    shared = sorted(set(primary) & set(professional))
    n = len(shared)
    if not n:
        return {"n": 0, "raw": 0.0, "kappa": 0.0}
    agree = sum(1 for k in shared if primary[k] == professional[k])
    po = agree / n
    pe = 0.0
    for v in VERDICTS:
        pa = sum(1 for k in shared if primary[k] == v) / n
        pb = sum(1 for k in shared if professional[k] == v) / n
        pe += pa * pb
    kappa = 0.0 if pe >= 1.0 else (po - pe) / (1.0 - pe)
    return {"n": n, "raw": po, "kappa": kappa, "agreed": agree}
