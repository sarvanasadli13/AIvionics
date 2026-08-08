"""Engineer notes (PLAN Phase 4C) — the only feature that *generates* data.

Every other screen consumes evidence someone else recorded. This one records
what an engineer actually found, which is the field `defect_finding` exists to
hold and which nothing else in the plan can supply: SDR does not carry it and
the shop never returns it.

Three rules are structural rather than conventional, and each has a test:

* **anchored, always** — a note belongs to a tail, a defect, a task or a case.
  The schema declares `anchor_type`/`anchor_id` NOT NULL with a CHECK, so a
  free-floating note cannot be written even by hand;
* **private to the author until shared** — named engineers writing dated notes
  is GDPR personal data, and a searchable who-wrote-what record is
  performance-monitoring adjacent (BetrVG §87(1)(6));
* **never an input to a label or a statistic** — notes are evidence a human
  reads. `aivionics.db.stats_guard` refuses any aggregate SQL that names the
  table, and a test walks the source tree to prove only this package queries it.

The app owns the note. The engineer's own calendar owns the alerting — see
`aivionics.notes.ics` for why nothing here ever fires an alarm.
"""
from .ics import calendar_for_notes, escape_text, unfold
from .store import (ANCHOR_TYPES, FINDING_TYPES, AnchorRequired, Note,
                    NoteNotFound, PromotionNotAllowed, create, delete,
                    for_anchor, get, handover, is_due, list_notes, mine,
                    promote_to_finding, set_shared, update)

__all__ = [
    "ANCHOR_TYPES", "FINDING_TYPES", "AnchorRequired", "Note", "NoteNotFound",
    "PromotionNotAllowed", "create", "delete", "for_anchor", "get", "handover",
    "is_due", "list_notes", "mine", "promote_to_finding", "set_shared",
    "update", "calendar_for_notes", "escape_text", "unfold",
]
