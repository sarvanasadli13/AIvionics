"""Additive DDL owned by Phase 3.

Kept out of ``db.SCHEMA`` deliberately. ``repeat_link`` already holds the
Phase 0.5 **naive** linkage — 10.9 M pairs, 87.6% of them ATA 53 structural
inspections — and that table is cited in `docs/status/phase0-1-report.md` as
the measurement that proved normalisation was needed. Overwriting it would
destroy the evidence for the decision. So Phase 3.2 writes its normalised
links to a second table and the two stay comparable side by side.

The pattern matches ``ui.store.ensure_ui_tables``: ``CREATE TABLE IF NOT
EXISTS`` only, never a migration, so it composes with whatever else holds the
database open.
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
-- Phase 3.2 normalised repeat linkage. Same tail, same ATA chapter, symptom
-- similarity above threshold, inside the elapsed window. `similarity` is kept
-- so a link can be audited rather than trusted.
CREATE TABLE IF NOT EXISTS repeat_norm(
    defect_id        INTEGER NOT NULL REFERENCES defect(id),
    repeat_defect_id INTEGER NOT NULL REFERENCES defect(id),
    days_apart       INTEGER NOT NULL,
    similarity       REAL    NOT NULL,
    same_action      INTEGER NOT NULL DEFAULT 0,
    ata_chapter      TEXT,
    PRIMARY KEY(defect_id, repeat_defect_id)
);
CREATE INDEX IF NOT EXISTS ix_repeat_norm_days   ON repeat_norm(days_apart);
CREATE INDEX IF NOT EXISTS ix_repeat_norm_repeat ON repeat_norm(repeat_defect_id);
CREATE INDEX IF NOT EXISTS ix_repeat_norm_ata    ON repeat_norm(ata_chapter);

-- Supporting indexes for the statistics queries. defect_action is scanned by
-- part number and by defect; without these the fleet view is a table scan of
-- ~1.4 M rows per window change.
CREATE INDEX IF NOT EXISTS ix_action_defect ON defect_action(defect_id);
CREATE INDEX IF NOT EXISTS ix_action_pn     ON defect_action(part_number);
CREATE INDEX IF NOT EXISTS ix_action_type   ON defect_action(action_type);
CREATE INDEX IF NOT EXISTS ix_finding_defect ON defect_finding(defect_id);
CREATE INDEX IF NOT EXISTS ix_defect_reported ON defect(reported_at);
CREATE INDEX IF NOT EXISTS ix_defect_tail_ata ON defect(aircraft_tail, ata_ref);
"""


def ensure(con: sqlite3.Connection) -> None:
    """Create the Phase 3 tables and indexes if they are not already there."""
    con.executescript(SCHEMA)
    con.commit()
