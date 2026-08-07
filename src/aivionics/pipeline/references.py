"""Phase 0.4 — reference extractor: manual type + task number + function code.

The 2026-08-06 measurement found the naive regex's top 'function code' was 001
at 19.5% (also 101/201/404/911) — not Boeing function codes at all. Fix: the
function-code whitelist is DATA-DRIVEN — the set of codes that actually occur
in the extracted AMM tasks + FIM catalogue. Nothing is guessed.
"""
from __future__ import annotations

import re
import sqlite3

TASK_IN_TEXT = re.compile(
    r"\b(?:(AMM|FIM|IFIM|TSM|SRM|CMM|DDG|MM)\s*[.:# ]?\s*)?"
    r"([A-Z]?\d{2})[-\s]?(\d{2})[-\s]?(\d{2})[-\s]?(\d{3})[-\s]?"
    r"([A-Z]\d{2}|\d{3}[A-Z]?)\b"
)


def load_function_whitelist(con: sqlite3.Connection) -> set[str]:
    rows = con.execute(
        "SELECT DISTINCT function_code FROM task WHERE function_code IS NOT NULL"
    ).fetchall()
    wl = {r[0] for r in rows}
    rows = con.execute("SELECT DISTINCT task_number FROM mmsg_task").fetchall()
    for (tn,) in rows:
        parts = tn.split("-")
        if len(parts) >= 5:
            wl.add(parts[3])
    return wl


def extract(narrative: str, whitelist: set[str]) -> list[dict]:
    """Return [{task_number, manual_type, function_code}] validated refs."""
    out = []
    seen = set()
    for m in TASK_IN_TEXT.finditer(narrative or ""):
        manual, g1, sec, subj, func, seq = m.groups()
        if func not in whitelist:
            continue                      # kills 001/101/201/404/911 FPs
        tn = f"{g1}-{sec}-{subj}-{func}-{seq}"
        if tn in seen:
            continue
        seen.add(tn)
        out.append({
            "task_number": tn,
            "manual_type": (manual or "").upper() or None,
            "function_code": func,
        })
    return out
