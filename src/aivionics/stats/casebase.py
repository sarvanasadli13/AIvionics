"""Phase 3.1 — the case base: what was **done**, and what was **found**.

The two are separate tables because they are separate claims, and conflating
them is the failure the six-model review killed the first design over: the
labels record what an engineer *did*, never what was *correct*.

``defect_action``   the corrective action and the part it was performed on.
                    Mined from the rectification half of the split narrative;
                    the part name and number prefer SDR's own structured
                    columns and fall back to the text.
``defect_finding``  what was found. ``finding_type`` is one of
                    ``confirmed_fault`` / ``no_fault_found`` / ``not_recorded``.

**`no_fault_found` here is narrative language, not a shop verdict.** True NFF
is a teardown finding and is not in this data at all (PLAN §1.3). A row typed
``no_fault_found`` means the write-up said so — *"ops check good"*, *"could not
duplicate"* — which is a different and weaker claim. Nothing downstream may
call it NFF; see ``metrics`` for the name the rate is allowed to carry.

SDR's ``part_condition`` column (CORRODED, CRACKED, INOPERATIVE...) is
deliberately **not** used as a finding. It is the condition reported when the
report was filed, not what was found on investigation, and treating it as a
finding would manufacture a confirmed-fault rate out of the reporter's own
opening description.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from . import schema

# ── actions ─────────────────────────────────────────────────────────────
# Ordered: the first pattern that matches wins, so the compound forms have to
# precede their own components ("REMOVED AND REPLACED" before "REMOVED").

ACTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("removed_replaced", re.compile(
        r"\bREMOVED\s+AND\s+REPLACED\b|\bR\s*&\s*R'?D?\b|\bR/R\b", re.I)),
    ("replaced", re.compile(
        r"\b(?:MAINTENANCE\s+)?REPLACED\b|\bREPLACEMENT\s+OF\b", re.I)),
    ("installed", re.compile(r"\bINSTALLED\b|\bRE-?INSTALLED\b", re.I)),
    ("removed", re.compile(r"\bREMOVED\b", re.I)),
    ("repaired", re.compile(r"\bREPAIRED\b|\bREPAIR\s+(?:OF|TO)\b", re.I)),
    ("complied_with", re.compile(
        r"\bCOMPLIED\s+WITH\b|\bC/W\b|\bACCOMPLISHED\b|\bCARRIED\s+OUT\b", re.I)),
    ("adjusted", re.compile(r"\bADJUSTED\b|\bRIGGED\b|\bCALIBRATED\b", re.I)),
    ("reset", re.compile(
        r"\bRESET\b|\bRE-?RACKED\b|\bRE-?SEATED\b|\bRE-?TERMINATED\b", re.I)),
    ("tightened", re.compile(r"\bTIGHTENED\b|\bTORQUED\b|\bSECURED\b", re.I)),
    ("cleaned", re.compile(r"\bCLEANED\b|\bCLEANING\b", re.I)),
    ("tested", re.compile(
        r"\bPERFORMED\s+(?:AN?\s+)?(?:OPERATION\w*|FUNCTION\w*)\s+(?:TEST|CHECK)\b"
        r"|\bTESTED\b|\bOPS?\s*C(?:HEC)?KS?\b", re.I)),
    ("inspected", re.compile(
        r"\bINSPECTED\b|\bPERFORMED\s+(?:AN?\s+)?INSPECT\w*\b|\bBORESCOPED\b", re.I)),
    ("troubleshot", re.compile(r"\bTROUBLESHOT\b|\bTROUBLE\s*SHOT\b", re.I)),
    ("corrected", re.compile(r"\bCORRECTED\b|\bRECTIFIED\b", re.I)),
]

# What counts as a removal for the Phase 3.3 metric. `installed` is excluded:
# an installation without a recorded removal is a modification, not a swap.
REMOVAL_ACTIONS = ("removed_replaced", "replaced", "removed")

# Where the part phrase ends. A citation, a clause break, or a task number.
_PART_STOP = re.compile(
    r"\b(?:IAW|PER|REF(?:ERENCE)?|USING|AS\s+REQUIRED|IN\s+ACCORDANCE|"
    r"I\.A\.W|A/W|AMM|FIM|IFIM|SRM|CMM|IPC|WDM|MM|DWG|TASK|CARD|MEL|SB|AD)\b"
    r"|[.;,:()\[\]]|\d{2}-\d{2}-\d{2}", re.I)
# Quantity noise that trails a part phrase: "... EMERGENCY LIGHT 1EA".
_QTY = re.compile(r"\b\d+\s*(?:EA|EACH|PCS?|QTY)\b\s*$", re.I)
_POSITION = re.compile(
    r"\b(L/H|LH|LEFT|R/H|RH|RIGHT|FWD|FORWARD|AFT|UPPER|LOWER|INBD|INBOARD|"
    r"OUTBD|OUTBOARD|NO\.?\s*\d|#\s*\d)\b", re.I)
_POSITION_CANON = {"L/H": "LH", "LEFT": "LH", "R/H": "RH", "RIGHT": "RH",
                   "FORWARD": "FWD", "INBOARD": "INBD", "OUTBOARD": "OUTBD"}
_PN_IN_TEXT = re.compile(
    r"\b(?:P/N|PN|PART\s+(?:NO\.?|NUMBER))\s*[:.]?\s*([A-Z0-9][A-Z0-9\-/]{3,24})",
    re.I)
_MAX_PART_CHARS = 60

# ── findings ────────────────────────────────────────────────────────────
# "No fault found" language. Narrative only — see the module docstring.
NO_FAULT_LANG = re.compile(
    r"\bNO\s+FAULT?S?\s+FOUND\b|\bNFF\b|\bCOULD\s+NOT\s+DUPLICATE\b|\bCND\b|"
    r"\bNO\s+DEFECTS?\s+(?:FOUND|NOTED)\b|\bNO\s+DAMAGE\s+(?:FOUND|NOTED)\b|"
    r"\bNO\s+DISCREPANC(?:Y|IES)\s+(?:FOUND|NOTED)\b|"
    r"\bOPS?\s*C(?:HEC)?KS?\s+(?:GOOD|NORMAL|OK|SAT\w*)\b|"
    r"\bCHECK(?:ED|S)?\s+(?:GOOD|NORMAL|OK|SAT\w*)\b|"
    r"\bFOUND\s+(?:SERVICEABLE|SATISFACTORY|WITHIN\s+LIMITS)\b", re.I)
# A condition actually found on investigation.
CONFIRMED_FAULT_LANG = re.compile(
    r"\bFOUND\b[^.;]{0,80}?\b(CHAFED|CHAFING|BROKEN|CORRODED|CORROSION|CRACKED|"
    r"CRACK|LOOSE|OPEN|SHORTED|SHORT|WORN|LEAKING|LEAK|CONTAMINAT\w*|MOISTURE|"
    r"WATER|BENT|SHEARED|FRAYED|BURNT|BURNED|SEIZED|MISSING|DENT\w*|DAMAGED|"
    r"OUT\s+OF\s+LIMITS?|EXCEED\w*)\b", re.I)
_FINDING_CHARS = 200

CONFIRMED_FAULT = "confirmed_fault"
NO_FAULT_FOUND = "no_fault_found"
NOT_RECORDED = "not_recorded"

MINED_SOURCE = "sdr_mined"


@dataclass(frozen=True)
class Action:
    """One corrective action, as mined from a rectification narrative."""

    action_type: str
    part_name: str | None = None
    part_number: str | None = None
    position: str | None = None

    @property
    def is_removal(self) -> bool:
        return self.action_type in REMOVAL_ACTIONS


@dataclass(frozen=True)
class Finding:
    """What the narrative says was found. ``not_recorded`` is the honest default."""

    finding_type: str
    finding_text: str | None = None

    @property
    def recorded(self) -> bool:
        return self.finding_type != NOT_RECORDED


# ── extraction ──────────────────────────────────────────────────────────

def extract_action(text: str | None, *, part_name: str | None = None,
                   part_number: str | None = None) -> Action | None:
    """Mine the action from a rectification narrative.

    ``part_name`` / ``part_number`` are SDR's own structured columns and win
    when present: the reporter typed them into a labelled field, which is
    better evidence than anything a regex recovers from prose.
    """
    if not text or not text.strip():
        return None
    for action_type, pattern in ACTION_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        phrase = _part_phrase(text, match.end())
        return Action(
            action_type=action_type,
            part_name=(part_name or "").strip() or phrase or None,
            part_number=(part_number or "").strip() or _pn_from_text(text),
            position=_position(phrase or (part_name or "")),
        )
    return None


def _part_phrase(text: str, start: int) -> str | None:
    """The noun phrase between the action verb and the first citation."""
    tail = text[start:start + 200].lstrip(" -:")
    stop = _PART_STOP.search(tail)
    phrase = tail[: stop.start()] if stop else tail
    phrase = _QTY.sub("", " ".join(phrase.split())).strip(" -/")
    if not phrase or len(phrase) < 3:
        return None
    return phrase[:_MAX_PART_CHARS].strip()


def _pn_from_text(text: str) -> str | None:
    match = _PN_IN_TEXT.search(text)
    return match.group(1).upper() if match else None


def _position(phrase: str | None) -> str | None:
    if not phrase:
        return None
    match = _POSITION.search(phrase)
    if match is None:
        return None
    raw = " ".join(match.group(1).upper().split())
    return _POSITION_CANON.get(raw, raw)


def extract_finding(text: str | None) -> Finding:
    """Classify the finding language in a narrative.

    ``no_fault_found`` is tested first: a narrative that both replaced a part
    and then recorded *"ops check good"* has not confirmed a fault, and
    reading the replacement as a confirmation is exactly the inference this
    product exists to stop an engineer making.
    """
    if not text:
        return Finding(NOT_RECORDED)
    confirmed = CONFIRMED_FAULT_LANG.search(text)
    no_fault = NO_FAULT_LANG.search(text)
    if no_fault and not confirmed:
        return Finding(NO_FAULT_FOUND, _snippet(text, no_fault))
    if confirmed:
        return Finding(CONFIRMED_FAULT, _snippet(text, confirmed))
    return Finding(NOT_RECORDED)


def _snippet(text: str, match: re.Match[str]) -> str:
    return " ".join(text[match.start():match.start() + _FINDING_CHARS].split())


# ── build ───────────────────────────────────────────────────────────────

_SELECT = """
SELECT d.id, d.rectification_text, d.reported_at, s.part_name, s.part_number,
       (SELECT l.task_number FROM label_silver l
         WHERE l.defect_id = d.id ORDER BY l.id LIMIT 1)
  FROM defect d
  LEFT JOIN sdr_raw s ON s.id = d.sdr_id
 WHERE d.id > ?
 ORDER BY d.id
 LIMIT ?
"""
_INS_ACTION = ("INSERT INTO defect_action(defect_id,action_type,part_name,"
               "part_number,position,task_number) VALUES(?,?,?,?,?,?)")
_INS_FINDING = ("INSERT INTO defect_finding(defect_id,finding_type,finding_text,"
                "found_at,source) VALUES(?,?,?,?,?)")


def build(con: sqlite3.Connection, *, batch: int = 50_000,
         rebuild: bool = False, limit: int | None = None,
         progress=None) -> dict[str, int]:
    """Populate ``defect_action`` and ``defect_finding`` from the narratives.

    Idempotent and resumable. Rows are processed in ascending ``defect.id``
    and committed per batch, so the highest id already in ``defect_action`` is
    a valid resume point: everything below it has been seen. A defect that
    yielded no action is re-scanned on a later run and still produces nothing,
    which costs CPU and changes no data.

    ``rebuild=True`` clears the mined rows first. It removes only
    ``source='sdr_mined'`` findings, so an engineer's promoted note (Phase 4C)
    survives a full statistics rebuild.
    """
    schema.ensure(con)
    if rebuild:
        con.execute("DELETE FROM defect_action WHERE defect_id IN"
                    " (SELECT id FROM defect WHERE source='sdr')")
        con.execute("DELETE FROM defect_finding WHERE source=?", (MINED_SOURCE,))
        con.commit()

    cursor = _resume_point(con)
    counts = {"scanned": 0, "actions": 0, "findings": 0,
              "removals": 0, "confirmed_fault": 0, "no_fault_found": 0}
    while True:
        take = batch if limit is None else min(batch, limit - counts["scanned"])
        if take <= 0:
            break
        rows = con.execute(_SELECT, (cursor, take)).fetchall()
        if not rows:
            break
        actions, findings = [], []
        for did, rect, at, pname, pnum, task_number in rows:
            cursor = did
            counts["scanned"] += 1
            action = extract_action(rect, part_name=pname, part_number=pnum)
            if action is not None:
                actions.append((did, action.action_type, action.part_name,
                                action.part_number, action.position, task_number))
                counts["removals"] += int(action.is_removal)
            finding = extract_finding(rect)
            if finding.recorded:
                findings.append((did, finding.finding_type, finding.finding_text,
                                 at, MINED_SOURCE))
                counts[finding.finding_type] += 1
        con.executemany(_INS_ACTION, actions)
        con.executemany(_INS_FINDING, findings)
        con.commit()
        counts["actions"] += len(actions)
        counts["findings"] += len(findings)
        if progress is not None:
            progress(counts)
        if len(rows) < take:
            break
    return counts


def _resume_point(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT MAX(defect_id) FROM defect_action").fetchone()
    return int(row[0]) if row and row[0] is not None else 0
