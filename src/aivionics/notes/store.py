"""Reading and writing anchored notes, and promoting one into a finding.

The `note` table and its CHECK constraint already exist in `aivionics.db`; this
module is the only code permitted to query it. Everything here is plain SQL on
a caller-supplied connection so the notes layer has no opinion about which
database it is pointed at — the tests hand it a fixture, the app hands it the
writable application connection.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Sequence

from .. import audit

# Mirrors the CHECK constraint on note.anchor_type. Kept in Python as well so a
# bad anchor is refused with a sentence an engineer can read rather than with
# "CHECK constraint failed".
ANCHOR_TYPES: tuple[str, ...] = ("aircraft", "defect", "task", "case")

# Mirrors the comment on defect_finding.finding_type.
FINDING_TYPES: tuple[str, ...] = ("confirmed_fault", "no_fault_found",
                                  "not_recorded")

# The source stamped on a finding that came from a promoted note, so a finding
# an engineer typed is never confused with one a shop returned.
PROMOTION_SOURCE = "engineer_note"

_COLUMNS = ("id", "author_id", "anchor_type", "anchor_id", "body", "due_date",
            "shared", "tool_assisted", "created_at", "updated_at")


class AnchorRequired(ValueError):
    """Raised when a note is written without a valid anchor (PLAN 4C.1)."""


class NoteNotFound(LookupError):
    pass


class PromotionNotAllowed(ValueError):
    """Raised when a note cannot become a `defect_finding` row."""


@dataclass(frozen=True)
class Note:
    id: int
    author_id: int
    anchor_type: str
    anchor_id: str
    body: str
    due_date: str | None
    shared: bool
    tool_assisted: bool
    created_at: str
    updated_at: str | None
    author_name: str | None = None

    @property
    def anchor(self) -> tuple[str, str]:
        return self.anchor_type, self.anchor_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _row_to_note(row: Sequence) -> Note:
    return Note(
        id=row[0], author_id=row[1], anchor_type=row[2], anchor_id=row[3],
        body=row[4], due_date=row[5], shared=bool(row[6]),
        tool_assisted=bool(row[7]), created_at=row[8], updated_at=row[9],
        author_name=row[10] if len(row) > 10 else None,
    )


def _check_anchor(anchor_type: str | None, anchor_id) -> tuple[str, str]:
    if anchor_type not in ANCHOR_TYPES:
        raise AnchorRequired(
            f"anchor_type must be one of {', '.join(ANCHOR_TYPES)} — "
            f"a note cannot float free of the object it is about")
    text = "" if anchor_id is None else str(anchor_id).strip()
    if not text:
        raise AnchorRequired("anchor_id is required — every note names its object")
    return anchor_type, text


def _check_body(body: str | None) -> str:
    text = (body or "").strip()
    if not text:
        raise ValueError("a note needs a body")
    return text


def _check_due(due_date: str | None) -> str | None:
    """Accept an ISO date or nothing. A due date drives sorting and a passive
    marker; it never drives an alarm."""
    if due_date in (None, ""):
        return None
    if isinstance(due_date, date):
        return due_date.isoformat()
    date.fromisoformat(str(due_date).strip())      # raises ValueError on junk
    return str(due_date).strip()


def inherited_tool_assisted(con: sqlite3.Connection, anchor_type: str,
                            anchor_id: str) -> bool:
    """Does the anchored object already carry the tool-assisted flag?

    Standing rule 7: once engineers paste tool output into write-ups, tomorrow's
    labels are the system's own echo. A note about a contaminated defect is
    itself contaminated, and the flag cannot be added later — so it is resolved
    at write time and never afterwards.
    """
    if anchor_type != "defect":
        return False
    try:
        row = con.execute("SELECT tool_assisted FROM defect WHERE id=?",
                          (anchor_id,)).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


# ── writing ─────────────────────────────────────────────────────────────

def create(con: sqlite3.Connection, *, author_id: int, anchor_type: str,
           anchor_id, body: str, due_date: str | None = None,
           shared: bool = False, tool_assisted: bool = True) -> Note:
    """Write a note.

    `tool_assisted` defaults to **true**: a note typed in this application, next
    to retrieved locators and mined cases, is tool-assisted by construction. The
    caller may pass False only for a note imported from somewhere the tool was
    not in the room. The flag is OR-ed with the anchor's own flag.
    """
    anchor_type, anchor_id = _check_anchor(anchor_type, anchor_id)
    body = _check_body(body)
    due = _check_due(due_date)
    flag = bool(tool_assisted) or inherited_tool_assisted(con, anchor_type, anchor_id)
    ts = _now()
    cur = con.execute(
        "INSERT INTO note(author_id,anchor_type,anchor_id,body,due_date,shared,"
        "tool_assisted,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (author_id, anchor_type, anchor_id, body, due, int(bool(shared)),
         int(flag), ts, ts))
    con.commit()
    return get(con, int(cur.lastrowid))


def update(con: sqlite3.Connection, note_id: int, *, author_id: int,
           body: str | None = None, due_date: str | None = ...,
           ) -> Note:
    """Edit a note's body and/or due date. Only the author may.

    `due_date` uses `...` as "leave alone" so that passing None can clear it.
    """
    note = get(con, note_id)
    if note.author_id != author_id:
        raise PermissionError("only the author may edit a note")
    sets, args = [], []
    if body is not None:
        sets.append("body=?")
        args.append(_check_body(body))
    if due_date is not ...:
        sets.append("due_date=?")
        args.append(_check_due(due_date))
    if not sets:
        return note
    sets.append("updated_at=?")
    args.extend([_now(), note_id])
    con.execute(f"UPDATE note SET {', '.join(sets)} WHERE id=?", args)
    con.commit()
    return get(con, note_id)


def set_shared(con: sqlite3.Connection, note_id: int, *, author_id: int,
               shared: bool = True) -> Note:
    """The explicit share action. Sharing is never a side effect of anything."""
    note = get(con, note_id)
    if note.author_id != author_id:
        raise PermissionError("only the author may share or unshare a note")
    con.execute("UPDATE note SET shared=?, updated_at=? WHERE id=?",
                (int(bool(shared)), _now(), note_id))
    con.commit()
    return get(con, note_id)


def delete(con: sqlite3.Connection, note_id: int, *, author_id: int) -> None:
    note = get(con, note_id)
    if note.author_id != author_id:
        raise PermissionError("only the author may delete a note")
    con.execute("DELETE FROM note WHERE id=?", (note_id,))
    con.commit()


# ── reading ─────────────────────────────────────────────────────────────

def get(con: sqlite3.Connection, note_id: int) -> Note:
    row = con.execute(
        "SELECT n.id,n.author_id,n.anchor_type,n.anchor_id,n.body,n.due_date,"
        "n.shared,n.tool_assisted,n.created_at,n.updated_at,u.display_name"
        " FROM note n LEFT JOIN app_user u ON u.id=n.author_id"
        " WHERE n.id=?", (note_id,)).fetchone()
    if row is None:
        raise NoteNotFound(f"no note {note_id}")
    return _row_to_note(row)


def list_notes(con: sqlite3.Connection, *, viewer_id: int,
               anchor_type: str | None = None, anchor_id=None,
               author_id: int | None = None, include_shared: bool = True,
               since: str | None = None) -> list[Note]:
    """Every note the viewer is allowed to see, most urgent first.

    Visibility is the whole point of the query: a note is the author's own
    unless they explicitly shared it, so the predicate is `author = viewer OR
    shared`. There is no view anywhere that widens it.
    """
    where = ["(n.author_id=? OR n.shared=1)"] if include_shared else ["n.author_id=?"]
    args: list = [viewer_id]
    if anchor_type is not None:
        where.append("n.anchor_type=?")
        args.append(anchor_type)
    if anchor_id is not None:
        where.append("n.anchor_id=?")
        args.append(str(anchor_id))
    if author_id is not None:
        where.append("n.author_id=?")
        args.append(author_id)
    if since:
        where.append("COALESCE(n.updated_at, n.created_at) >= ?")
        args.append(since)
    rows = con.execute(
        "SELECT n.id,n.author_id,n.anchor_type,n.anchor_id,n.body,n.due_date,"
        "n.shared,n.tool_assisted,n.created_at,n.updated_at,u.display_name"
        " FROM note n LEFT JOIN app_user u ON u.id=n.author_id"
        " WHERE " + " AND ".join(where) +
        # dated notes first and soonest-first; undated notes keep their own
        # order by most recently touched. CASE rather than NULLS LAST, which
        # older SQLite builds do not have.
        " ORDER BY CASE WHEN n.due_date IS NULL THEN 1 ELSE 0 END,"
        " n.due_date ASC, COALESCE(n.updated_at,n.created_at) DESC, n.id DESC",
        args).fetchall()
    return [_row_to_note(r) for r in rows]


def for_anchor(con: sqlite3.Connection, anchor_type: str, anchor_id, *,
               viewer_id: int) -> list[Note]:
    """Notes rendered inside the object they belong to (PLAN 4C.2)."""
    anchor_type, anchor_id = _check_anchor(anchor_type, anchor_id)
    return list_notes(con, viewer_id=viewer_id, anchor_type=anchor_type,
                      anchor_id=anchor_id)


def mine(con: sqlite3.Connection, author_id: int) -> list[Note]:
    """The "My notes" list (PLAN 4C.3) — this engineer's own, shared or not."""
    return list_notes(con, viewer_id=author_id, author_id=author_id,
                      include_shared=False)


def handover(con: sqlite3.Connection, *, viewer_id: int,
             since: str) -> list[Note]:
    """The shift-handover view: everything touched since `since` that this
    engineer may see — their own notes plus what colleagues shared."""
    return list_notes(con, viewer_id=viewer_id, since=since)


def is_due(note: Note, today: date | None = None) -> bool:
    """Passive marker only. Nothing in this application acts on the answer."""
    if not note.due_date:
        return False
    try:
        return date.fromisoformat(note.due_date) <= (today or _today())
    except ValueError:
        return False


def counts(con: sqlite3.Connection, *, viewer_id: int) -> dict[str, int]:
    """Headline counts for the My-notes header. Not a statistic about people:
    it counts the viewer's own workload, never anyone else's output."""
    notes = list_notes(con, viewer_id=viewer_id, author_id=viewer_id,
                       include_shared=False)
    return {
        "total": len(notes),
        "dated": sum(1 for n in notes if n.due_date),
        "due": sum(1 for n in notes if is_due(n)),
        "shared": sum(1 for n in notes if n.shared),
    }


# ── promotion — the point of the feature (PLAN 4C.6) ─────────────────────

def promote_to_finding(con: sqlite3.Connection, note_id: int, *,
                       finding_type: str, user_id: int | None = None) -> int:
    """Turn a note on a defect into a structured `defect_finding` row.

    This is the one mechanism in the product that records *what was found* as
    opposed to what was replaced. One action, and the note stays where it is —
    the free text remains readable next to the structured row it produced.

    A tool-assisted note carries its flag onto the defect, because a finding
    typed next to this tool's own output must be excluded from every future
    label set (standing rule 7).
    """
    note = get(con, note_id)
    if note.anchor_type != "defect":
        raise PromotionNotAllowed(
            f"only a note anchored to a defect can become a finding — "
            f"this one is anchored to a {note.anchor_type}")
    if finding_type not in FINDING_TYPES:
        raise PromotionNotAllowed(
            f"finding_type must be one of {', '.join(FINDING_TYPES)}")
    try:
        defect_id = int(note.anchor_id)
    except (TypeError, ValueError):
        raise PromotionNotAllowed(
            f"defect anchor {note.anchor_id!r} is not a defect id") from None

    cur = con.execute(
        "INSERT INTO defect_finding(defect_id,finding_type,finding_text,"
        "found_at,source) VALUES(?,?,?,?,?)",
        (defect_id, finding_type, note.body, _now(), PROMOTION_SOURCE))
    if note.tool_assisted:
        con.execute("UPDATE defect SET tool_assisted=1 WHERE id=?", (defect_id,))
    con.commit()
    finding_id = int(cur.lastrowid)
    audit.log(con, "note_promote", user_id=user_id, entity="defect_finding",
              entity_id=str(finding_id),
              payload={"note_id": note.id, "defect_id": defect_id,
                       "finding_type": finding_type,
                       "tool_assisted": bool(note.tool_assisted)})
    return finding_id


def findings_from_notes(con: sqlite3.Connection,
                        defect_id: int) -> list[tuple[int, str, str]]:
    """Findings on this defect that came from a promoted note."""
    return [(r[0], r[1], r[2]) for r in con.execute(
        "SELECT id, finding_type, finding_text FROM defect_finding"
        " WHERE defect_id=? AND source=? ORDER BY id",
        (defect_id, PROMOTION_SOURCE))]


def anchor_labels(notes: Iterable[Note]) -> dict[tuple[str, str], int]:
    """How many notes sit on each anchor — used to badge an object without
    opening it."""
    out: dict[tuple[str, str], int] = {}
    for note in notes:
        out[note.anchor] = out.get(note.anchor, 0) + 1
    return out
