"""SQLite layer. One host, WAL, local disk (standing rule 5).

Schema follows PLAN.md §4 plus the 2026-08-08 addendum: the IFIM metadata
(nav.js / mmsg.js) is plaintext, so the maintenance-message -> FIM-task routing
table exists as real data (mmsg / mmsg_task) and FIM catalogue rows carry
titles and applicability refs.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

-- ── fleet ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS aircraft(
    id INTEGER PRIMARY KEY,
    tail TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,
    msn TEXT, line_number TEXT, year_built INTEGER, delivery_date TEXT,
    total_time_hrs REAL, total_cycles INTEGER
);
CREATE TABLE IF NOT EXISTS aircraft_config(
    id INTEGER PRIMARY KEY,
    aircraft_id INTEGER NOT NULL REFERENCES aircraft(id),
    sb_embodied TEXT, stc TEXT, software_load TEXT, effective_from TEXT
);

-- ── manuals & tasks ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS manual(
    id INTEGER PRIMARY KEY,
    oem TEXT NOT NULL,                 -- boeing|airbus|embraer|atr|...
    aircraft_type TEXT NOT NULL,
    manual_type TEXT NOT NULL,         -- AMM|FIM|TSM|IPC|WDM|SRM|CMM|MEL|DDG
    doc_standard TEXT,                 -- ispec2200|s1000d|pdf_legacy|metadata
    parser_plugin TEXT,
    revision TEXT, revision_date TEXT,
    is_current INTEGER NOT NULL DEFAULT 1,
    source_file TEXT, source_hash TEXT, ingested_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_manual_current
    ON manual(aircraft_type, manual_type) WHERE is_current=1;

CREATE TABLE IF NOT EXISTS task(
    id INTEGER PRIMARY KEY,
    manual_id INTEGER NOT NULL REFERENCES manual(id),
    task_number TEXT NOT NULL,
    function_code TEXT,
    title TEXT,
    ata_chapter TEXT, ata_section TEXT, ata_subject TEXT,
    effectivity_raw TEXT,
    applic_refs TEXT,                  -- FIM: nav.js applicRefs
    body TEXT,                         -- NULL for catalogue-only rows
    body_hash TEXT,
    token_len INTEGER,
    catalogue_only INTEGER NOT NULL DEFAULT 0,
    embed_text TEXT,                   -- ATA hierarchy + title (Jo 2025)
    has_warning INTEGER DEFAULT 0, has_caution INTEGER DEFAULT 0,
    warning_count INTEGER DEFAULT 0, caution_count INTEGER DEFAULT 0,
    UNIQUE(manual_id, task_number)
);
CREATE INDEX IF NOT EXISTS ix_task_ata ON task(ata_chapter, ata_section);

CREATE TABLE IF NOT EXISTS task_section(
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES task(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,                -- warning|caution|effectivity|step|reference|other
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_link(
    from_task_id INTEGER NOT NULL REFERENCES task(id),
    to_task_number TEXT NOT NULL,
    link_type TEXT NOT NULL            -- reference
);
CREATE TABLE IF NOT EXISTS coverage(
    manual_id INTEGER NOT NULL REFERENCES manual(id),
    ata_chapter TEXT NOT NULL,
    toc_count INTEGER, extracted_count INTEGER, pct REAL,
    PRIMARY KEY(manual_id, ata_chapter)
);

-- ── maintenance messages (fault-code routing, from IFIM metadata) ────
CREATE TABLE IF NOT EXISTS mmsg(
    code TEXT PRIMARY KEY,             -- e.g. 34-21102
    ata_chapter TEXT,
    text TEXT,
    corr INTEGER
);
CREATE TABLE IF NOT EXISTS mmsg_task(
    code TEXT NOT NULL REFERENCES mmsg(code),
    seq INTEGER NOT NULL,
    task_number TEXT NOT NULL,
    PRIMARY KEY(code, seq)
);
CREATE TABLE IF NOT EXISTS effectivity_airplane(
    eff_ref TEXT PRIMARY KEY,
    model TEXT, owner TEXT, msn TEXT, line_no TEXT, tail TEXT, engine TEXT
);

-- ── SDR raw + normalized defects ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS sdr_raw(
    id INTEGER PRIMARY KEY,
    source_year INTEGER NOT NULL,
    control_no TEXT,
    difficulty_date TEXT,
    tail TEXT,
    aircraft_make TEXT, aircraft_model TEXT,
    jasc_code TEXT,
    part_name TEXT, part_number TEXT, part_condition TEXT,
    stage TEXT,
    discrepancy TEXT,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sdr_tail ON sdr_raw(tail);
CREATE INDEX IF NOT EXISTS ix_sdr_jasc ON sdr_raw(jasc_code);
CREATE INDEX IF NOT EXISTS ix_sdr_year ON sdr_raw(source_year);

CREATE TABLE IF NOT EXISTS defect(
    id INTEGER PRIMARY KEY,
    sdr_id INTEGER REFERENCES sdr_raw(id),
    aircraft_tail TEXT,
    reported_at TEXT,
    ata_ref TEXT,
    fault_code TEXT, fault_code_source TEXT,
    defect_text TEXT,                  -- pre-rectification (leak-free query)
    rectification_text TEXT,
    source TEXT NOT NULL DEFAULT 'sdr',
    tool_assisted INTEGER NOT NULL DEFAULT 0,   -- standing rule 7
    sdr_year INTEGER
);
CREATE INDEX IF NOT EXISTS ix_defect_tail ON defect(aircraft_tail);

CREATE TABLE IF NOT EXISTS defect_action(
    id INTEGER PRIMARY KEY,
    defect_id INTEGER NOT NULL REFERENCES defect(id),
    action_type TEXT, part_name TEXT, part_number TEXT, position TEXT,
    task_number TEXT
);
CREATE TABLE IF NOT EXISTS defect_finding(
    id INTEGER PRIMARY KEY,
    defect_id INTEGER NOT NULL REFERENCES defect(id),
    finding_type TEXT NOT NULL,        -- confirmed_fault|no_fault_found|not_recorded
    finding_text TEXT,
    found_at TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS defect_closure(
    defect_id INTEGER PRIMARY KEY REFERENCES defect(id),
    closed_at TEXT, closed_by TEXT, reason_for_removal TEXT,
    complete INTEGER NOT NULL DEFAULT 0
);
-- invariant: closure.complete=1 requires a finding row
CREATE TRIGGER IF NOT EXISTS trg_closure_needs_finding
BEFORE UPDATE OF complete ON defect_closure
WHEN NEW.complete=1 AND NOT EXISTS(
    SELECT 1 FROM defect_finding WHERE defect_id=NEW.defect_id)
BEGIN
    SELECT RAISE(ABORT, 'defect_finding required before closure.complete');
END;

-- ── labels ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS label_silver(
    id INTEGER PRIMARY KEY,
    defect_id INTEGER NOT NULL REFERENCES defect(id),
    task_number TEXT NOT NULL,
    manual_type TEXT,
    function_code TEXT,
    confidence_tier TEXT,              -- HIGH|MED|LOW
    leak_free INTEGER NOT NULL DEFAULT 0,
    split TEXT                         -- train|test
);
CREATE INDEX IF NOT EXISTS ix_silver_defect ON label_silver(defect_id);
-- Phase 0.5 outcome linkage. A pair is linked when the same tail reports the
-- same ATA chapter twice inside the window. days_apart carries the actual gap
-- so the 30d and 90d variants are one table, not two.
CREATE TABLE IF NOT EXISTS repeat_link(
    defect_id INTEGER NOT NULL REFERENCES defect(id),
    repeat_defect_id INTEGER NOT NULL REFERENCES defect(id),
    days_apart INTEGER NOT NULL,
    same_tail INTEGER NOT NULL,
    same_ata INTEGER NOT NULL,
    PRIMARY KEY(defect_id, repeat_defect_id)
);
CREATE INDEX IF NOT EXISTS ix_repeat_days ON repeat_link(days_apart);

-- Phase 0.8 stratified frequency baseline (train split only — the baseline
-- Gate 2 must beat, so it may never see test data).
CREATE TABLE IF NOT EXISTS baseline_freq(
    ata_chapter TEXT NOT NULL,
    task_number TEXT NOT NULL,
    cnt INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY(ata_chapter, task_number)
);

-- Phase 0.7 adjudication queue (the UI reads this; see build_gold_queue.py)
CREATE TABLE IF NOT EXISTS gold_queue(
    id INTEGER PRIMARY KEY,
    defect_id INTEGER NOT NULL REFERENCES defect(id),
    task_number TEXT NOT NULL,
    stratum TEXT NOT NULL,
    seq INTEGER NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    UNIQUE(defect_id, task_number)
);

CREATE TABLE IF NOT EXISTS label_gold(
    id INTEGER PRIMARY KEY,
    defect_id INTEGER NOT NULL REFERENCES defect(id),
    task_number TEXT NOT NULL,
    verdict TEXT,                      -- yes|no|partial|unsure
    correct_task_number TEXT,
    note TEXT,
    adjudicated_at TEXT
);
CREATE TABLE IF NOT EXISTS label_gold_pro(
    id INTEGER PRIMARY KEY,
    gold_id INTEGER NOT NULL REFERENCES label_gold(id),
    verdict TEXT, correct_task_number TEXT, adjudicator TEXT, adjudicated_at TEXT
);

-- ── users / audit (hash-chained) / notes / compliance ────────────────
CREATE TABLE IF NOT EXISTS role(
    id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, permissions TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_user(
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    pwhash BLOB NOT NULL,
    display_name TEXT,
    role_id INTEGER NOT NULL REFERENCES role(id),
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user_id INTEGER,
    action TEXT NOT NULL,
    entity TEXT, entity_id TEXT,
    payload_hash TEXT,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS print_log(
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL, user_id INTEGER, task_id INTEGER,
    manual_revision TEXT, aircraft_id INTEGER
);
CREATE TABLE IF NOT EXISTS note(
    id INTEGER PRIMARY KEY,
    author_id INTEGER NOT NULL REFERENCES app_user(id),
    anchor_type TEXT NOT NULL CHECK(anchor_type IN ('aircraft','defect','task','case')),
    anchor_id TEXT NOT NULL,
    body TEXT NOT NULL,
    due_date TEXT,
    shared INTEGER NOT NULL DEFAULT 0,
    tool_assisted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS compliance_item(
    id INTEGER PRIMARY KEY,
    aircraft_tail TEXT NOT NULL,
    kind TEXT NOT NULL,                -- checkup|mel|adsb
    ref TEXT, description TEXT,
    mel_category TEXT,                 -- A|B|C|D
    due_date TEXT, due_hours REAL, due_cycles INTEGER,
    source_system TEXT NOT NULL,       -- provenance (standing rule 2)
    imported_at TEXT NOT NULL,
    batch_id TEXT NOT NULL
);

-- ── vectors ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vec_index(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                -- task|case
    ref_id INTEGER NOT NULL,
    index_version TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL,
    UNIQUE(kind, ref_id, index_version)
);

-- ── FTS5 (exact tokens: task numbers, part numbers, SMYD...) ─────────
CREATE VIRTUAL TABLE IF NOT EXISTS task_fts USING fts5(
    task_number, title, embed_text, content=''
);
CREATE VIRTUAL TABLE IF NOT EXISTS case_fts USING fts5(
    defect_text, content=''
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    # Phase 0/1 ingest holds long write transactions while other processes
    # read; wait rather than fail instantly on a locked database.
    con.execute("PRAGMA busy_timeout=60000")
    con.executescript(SCHEMA)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def stats_guard(sql: str) -> None:
    """Standing rule (Phase 4C): no statistics query may touch the note table."""
    if "note" in sql.lower().split():
        raise ValueError("statistics queries must not reference the note table")
