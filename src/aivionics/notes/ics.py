"""iCalendar export for notes that carry a due date (RFC 5545, PLAN 4C.5).

**The application never fires an alarm, and this file is why it does not need
to.** Three failure modes, and the third is the serious one: the app has to be
running, so close it on Friday and the Monday reminder never happens; the
engineer has one PC but already carries Outlook everywhere; and a missed in-app
reminder manufactures exactly the false confidence a shadow compliance clock
does — a second record of a deadline with no source system and no import
timestamp to stamp it with, which is the one mitigation standing rule 2 leans
on. So the app owns the *note*, because it is anchored to an engineering object
the calendar knows nothing about, and the calendar owns the *alerting*.

Consequently nothing here emits a `VALARM`. The exported entry lands in the
engineer's real calendar and inherits that calendar's own reminder policy,
which is a system their organisation already administers.

Two component types, because the two calendars in the room disagree:
`VEVENT` (the default) is an all-day entry every client renders, and `VTODO` is
the semantically correct task — supported by Thunderbird and Apple Calendar,
and quietly dropped by Outlook, which is why it is not the default.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

CRLF = "\r\n"
FOLD_LIMIT = 75                 # octets, RFC 5545 §3.1
PRODID = "-//AIvionics//Engineer Notes//EN"
UID_DOMAIN = "aivionics.local"
COMPONENTS = ("VEVENT", "VTODO")

# RFC 5545 §3.3.11 — the four characters a TEXT value must escape. Order
# matters: the backslash has to go first or it re-escapes the others.
_ESCAPES = (("\\", "\\\\"), (";", "\\;"), (",", "\\,"))

_SUMMARY_CHARS = 72

ANCHOR_WORDS = {
    "aircraft": "Tail",
    "defect": "Defect",
    "task": "Task",
    "case": "Case",
}


@dataclass(frozen=True)
class CalendarEntry:
    """What one note contributes to the exported calendar."""

    uid: str
    summary: str
    description: str
    due: date
    created: str | None = None
    modified: str | None = None
    anchor: str = ""


def escape_text(value: str) -> str:
    """Escape a TEXT value. Newlines become the literal two-character `\\n`."""
    out = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    for old, new in _ESCAPES:
        out = out.replace(old, new)
    return out.replace("\n", "\\n")


def fold_line(line: str, limit: int = FOLD_LIMIT) -> list[str]:
    """Split one content line into folded lines of at most `limit` octets.

    Folding counts octets, not characters, and a multi-byte character may not
    be split across the fold — so the walk is per character with a byte budget.
    """
    if len(line.encode("utf-8")) <= limit:
        return [line]
    pieces: list[str] = []
    chunk: list[str] = []
    used = 0
    budget = limit
    for ch in line:
        width = len(ch.encode("utf-8"))
        if used + width > budget:
            pieces.append("".join(chunk))
            chunk, used = [], 0
            budget = limit - 1          # continuation lines carry a leading space
        chunk.append(ch)
        used += width
    pieces.append("".join(chunk))
    return [pieces[0]] + [" " + p for p in pieces[1:]]


def unfold(text: str) -> str:
    """Inverse of the fold, for anything that needs to read a calendar back."""
    return re.sub(r"\r?\n[ \t]", "", text or "")


def _stamp(value: str | datetime | None) -> str | None:
    """`20260808T143200Z` from an ISO-8601 string or a datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _date_value(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def summarise(body: str, anchor_type: str = "", anchor_id: str = "") -> str:
    """A one-line calendar title: which object, then the engineer's first line.

    The calendar entry has to be readable in a list of thirty other entries, so
    the anchor leads. Nothing is invented — the text is the note's own.
    """
    first = next((ln.strip() for ln in (body or "").splitlines() if ln.strip()), "")
    if len(first) > _SUMMARY_CHARS:
        first = first[:_SUMMARY_CHARS - 1].rstrip() + "…"
    label = ANCHOR_WORDS.get(anchor_type, anchor_type.title() if anchor_type else "")
    where = f"{label} {anchor_id}".strip()
    return f"AIvionics · {where} — {first}" if where else f"AIvionics · {first}"


def entry_for_note(note) -> CalendarEntry | None:
    """Build the entry for a note, or None when it has no due date."""
    if not getattr(note, "due_date", None):
        return None
    anchor_type = getattr(note, "anchor_type", "") or ""
    anchor_id = str(getattr(note, "anchor_id", "") or "")
    return CalendarEntry(
        uid=f"note-{note.id}@{UID_DOMAIN}",
        summary=summarise(note.body, anchor_type, anchor_id),
        description=note.body,
        due=_date_value(note.due_date),
        created=getattr(note, "created_at", None),
        modified=getattr(note, "updated_at", None),
        anchor=f"{anchor_type}:{anchor_id}" if anchor_type else "",
    )


def _component(entry: CalendarEntry, kind: str, dtstamp: str) -> list[str]:
    lines = [f"BEGIN:{kind}", f"UID:{entry.uid}", f"DTSTAMP:{dtstamp}"]
    if kind == "VTODO":
        lines.append(f"DUE;VALUE=DATE:{entry.due.strftime('%Y%m%d')}")
        lines.append("STATUS:NEEDS-ACTION")
    else:
        # For a DATE value DTEND is exclusive, so an all-day entry on the due
        # date ends the following day (RFC 5545 §3.6.1).
        lines.append(f"DTSTART;VALUE=DATE:{entry.due.strftime('%Y%m%d')}")
        lines.append(
            f"DTEND;VALUE=DATE:{(entry.due + timedelta(days=1)).strftime('%Y%m%d')}")
        lines.append("TRANSP:TRANSPARENT")
    lines.append(f"SUMMARY:{escape_text(entry.summary)}")
    lines.append(f"DESCRIPTION:{escape_text(entry.description)}")
    lines.append("CATEGORIES:AIVIONICS")
    created = _stamp(entry.created)
    modified = _stamp(entry.modified)
    if created:
        lines.append(f"CREATED:{created}")
    if modified:
        lines.append(f"LAST-MODIFIED:{modified}")
    if entry.anchor:
        lines.append(f"X-AIVIONICS-ANCHOR:{escape_text(entry.anchor)}")
    lines.append(f"END:{kind}")
    return lines


def build_calendar(entries: Sequence[CalendarEntry], *, component: str = "VEVENT",
                   now: datetime | None = None) -> str:
    """Serialise entries into one RFC 5545 stream, CRLF-terminated and folded."""
    if component not in COMPONENTS:
        raise ValueError(f"component must be one of {', '.join(COMPONENTS)}")
    dtstamp = _stamp(now or datetime.now(timezone.utc))
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for entry in entries:
        lines.extend(_component(entry, component, dtstamp))
    lines.append("END:VCALENDAR")
    folded: list[str] = []
    for line in lines:
        folded.extend(fold_line(line))
    return CRLF.join(folded) + CRLF


def calendar_for_notes(notes: Iterable, *, component: str = "VEVENT",
                       now: datetime | None = None) -> str:
    """Export every note that carries a due date. Notes without one are simply
    not calendar entries — they are still notes."""
    entries = [e for e in (entry_for_note(n) for n in notes) if e is not None]
    return build_calendar(entries, component=component, now=now)


def write_calendar(path, notes: Iterable, *, component: str = "VEVENT") -> int:
    """Write the `.ics` file and return the number of entries in it.

    `newline=""` keeps Python from translating the CRLF pairs the format
    requires into the platform's line ending.
    """
    entries = [e for e in (entry_for_note(n) for n in notes) if e is not None]
    text = build_calendar(entries, component=component)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return len(entries)
