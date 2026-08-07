"""Locator printing — standing rule 1.

The print path emits a *locator*: where to find the task in the controlled
manual, who asked and when. It never emits procedure text. Not an excerpt,
not the first step, not a warning — the engineer is sent to the approved
source, because a local extraction is uncontrolled data by definition
(Part-145 145.A.45).

`format_locator` reads a fixed whitelist of fields off whatever mapping it
is handed, so passing a full task row that happens to contain `body` cannot
leak the body into the output. The test suite asserts exactly that.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Mapping

from .. import audit

# The only task fields that may ever reach a printed page.
LOCATOR_FIELDS = ("task_number", "title", "effectivity_raw")

BANNER = "── LOCATOR ONLY — OPEN THE CONTROLLED MANUAL FOR THE PROCEDURE ──"

# Standing rule 8: unresolved applicability is stated, never rendered clean.
UNRESOLVED = "applicability unresolved — verify in controlled data"


def format_locator(task: Mapping,
                   manual: Mapping | None = None,
                   aircraft: Mapping | None = None,
                   signed_by: str = "unknown",
                   when: datetime | None = None) -> str:
    """Build the five-line locator block from PLAN §4.

        B737-8   ·   AMM Rev 48   ·   issued 2026-06-15
        TASK 34-11-01-400-801   PITOT PROBE — REMOVAL / INSTALLATION
        Effectivity: MSN 28xxx–41xxx        Aircraft: 4K-AZ12
        Printed 2026-08-06 14:32Z by S. Asadli
        ── LOCATOR ONLY — OPEN THE CONTROLLED MANUAL FOR THE PROCEDURE ──
    """
    manual = manual or {}
    aircraft = aircraft or {}
    when = when or datetime.now(timezone.utc)

    task_number = str(task.get("task_number") or "—")
    title = str(task.get("title") or "").strip().upper() or "TITLE NOT RECORDED"
    effectivity = str(task.get("effectivity_raw") or "").strip() or UNRESOLVED

    ac_type = str(manual.get("aircraft_type") or aircraft.get("type") or "—")
    man_type = str(manual.get("manual_type") or "—")
    revision = str(manual.get("revision") or "—")
    issued = str(manual.get("revision_date") or "—")
    tail = str(aircraft.get("tail") or "—")

    stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")

    return "\n".join([
        f"{ac_type}   ·   {man_type} Rev {revision}   ·   issued {issued}",
        f"TASK {task_number}   {title}",
        f"Effectivity: {effectivity}        Aircraft: {tail}",
        f"Printed {stamp} by {signed_by}",
        BANNER,
    ])


def record_print(con: sqlite3.Connection, *, user_id: int | None,
                 task_id: int | None, manual_revision: str | None,
                 aircraft_id: int | None, task_number: str | None = None) -> None:
    """Write the print_log row and its audit entry. Both, always, together."""
    ts = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO print_log(ts,user_id,task_id,manual_revision,aircraft_id) "
        "VALUES(?,?,?,?,?)", (ts, user_id, task_id, manual_revision, aircraft_id))
    con.commit()
    audit.log(con, "print", user_id=user_id, entity="task",
              entity_id=str(task_number or task_id or ""),
              payload={"manual_revision": manual_revision,
                       "aircraft_id": aircraft_id})


def print_locator(con: sqlite3.Connection, task: Mapping,
                  manual: Mapping | None = None,
                  aircraft: Mapping | None = None,
                  user=None) -> str:
    """Format the locator, log the print, return the text to render."""
    from .auth import signature

    text = format_locator(task, manual, aircraft, signed_by=signature(user))
    record_print(con,
                 user_id=getattr(user, "id", None),
                 task_id=task.get("id"),
                 manual_revision=(manual or {}).get("revision"),
                 aircraft_id=(aircraft or {}).get("id"),
                 task_number=task.get("task_number"))
    return text
