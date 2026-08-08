"""Notes behaviour that the UI depends on (PLAN 4C), tested headlessly.

The promotion path is the one that matters: it is the only mechanism in the
product that records *what was found* rather than what was replaced.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aivionics import db
from aivionics.notes import ics, store


@pytest.fixture()
def con(tmp_path):
    c = db.connect(tmp_path / "n.db")
    c.execute("INSERT INTO role(name,permissions) VALUES('engineer','r')")
    c.execute("INSERT INTO app_user(id,username,pwhash,role_id)"
              " VALUES(1,'s.asadli',x'00',1)")
    c.execute("INSERT INTO app_user(id,username,pwhash,role_id)"
              " VALUES(2,'other',x'00',1)")
    c.execute("INSERT INTO defect(id,defect_text,ata_ref) VALUES(7,'AIRSPEED','34')")
    c.commit()
    return c


# ── anchoring ────────────────────────────────────────────────────────────
def test_a_note_must_be_anchored(con):
    with pytest.raises((store.AnchorRequired, ValueError)):
        store.create(con, author_id=1, anchor_type="", anchor_id="",
                     body="floating")


def test_notes_come_back_for_their_anchor(con):
    store.create(con, author_id=1, anchor_type="defect", anchor_id="7",
                 body="found chafed wire behind P6-4")
    store.create(con, author_id=1, anchor_type="aircraft", anchor_id="N101AV",
                 body="different object")
    got = store.for_anchor(con, "defect", "7", viewer_id=1)
    assert len(got) == 1
    assert "chafed wire" in got[0].body


# ── privacy ──────────────────────────────────────────────────────────────
def test_a_note_is_private_to_its_author_by_default(con):
    """GDPR and BetrVG §87(1)(6): a searchable who-wrote-what record is
    performance-monitoring adjacent."""
    note = store.create(con, author_id=1, anchor_type="defect", anchor_id="7",
                        body="mine only")
    assert note.shared is False
    assert store.for_anchor(con, "defect", "7", viewer_id=2) == []
    store.set_shared(con, note.id, author_id=1, shared=True)
    assert len(store.for_anchor(con, "defect", "7", viewer_id=2)) == 1


def test_only_the_author_may_share_or_delete(con):
    note = store.create(con, author_id=1, anchor_type="defect", anchor_id="7",
                        body="mine")
    with pytest.raises((PermissionError, store.NoteNotFound, ValueError)):
        store.set_shared(con, note.id, author_id=2, shared=True)
    with pytest.raises((PermissionError, store.NoteNotFound, ValueError)):
        store.delete(con, note.id, author_id=2)


# ── promotion: the point of the feature ──────────────────────────────────
def test_promoting_a_note_records_a_structured_finding(con):
    note = store.create(con, author_id=1, anchor_type="defect", anchor_id="7",
                        body="found chafed wire at the connector, not the LRU")
    fid = store.promote_to_finding(con, note.id,
                                   finding_type="confirmed_fault", user_id=1)
    row = con.execute("SELECT defect_id, finding_type, finding_text"
                      " FROM defect_finding WHERE id=?", (fid,)).fetchone()
    assert row[0] == 7 and row[1] == "confirmed_fault"
    assert "chafed wire" in row[2]
    # the note stays: the free text remains readable beside the structured row
    assert store.get(con, note.id).body == note.body


def test_only_a_defect_note_can_become_a_finding(con):
    note = store.create(con, author_id=1, anchor_type="aircraft",
                        anchor_id="N101AV", body="not a defect")
    with pytest.raises(store.PromotionNotAllowed):
        store.promote_to_finding(con, note.id, finding_type="confirmed_fault")


def test_an_unknown_finding_type_is_refused(con):
    note = store.create(con, author_id=1, anchor_type="defect", anchor_id="7",
                        body="x")
    with pytest.raises(store.PromotionNotAllowed):
        store.promote_to_finding(con, note.id, finding_type="probably_fine")


def test_promotion_carries_the_tool_assisted_flag_onto_the_defect(con):
    """Standing rule 7: a finding typed next to this tool's own output must be
    excluded from every future label set, and the flag cannot be retrofitted."""
    note = store.create(con, author_id=1, anchor_type="defect", anchor_id="7",
                        body="from the tool", tool_assisted=True)
    store.promote_to_finding(con, note.id, finding_type="no_fault_found")
    assert con.execute("SELECT tool_assisted FROM defect WHERE id=7"
                       ).fetchone()[0] == 1


def test_promotion_is_audited(con):
    note = store.create(con, author_id=1, anchor_type="defect", anchor_id="7",
                        body="x")
    store.promote_to_finding(con, note.id, finding_type="confirmed_fault",
                             user_id=1)
    actions = [r[0] for r in con.execute("SELECT action FROM audit_log")]
    assert "note_promote" in actions


# ── calendar export ──────────────────────────────────────────────────────
def test_ics_export_produces_a_parseable_calendar(con):
    store.create(con, author_id=1, anchor_type="defect", anchor_id="7",
                 body="check the MEL on 4K-AZ12", due_date="2026-09-01")
    notes = store.for_anchor(con, "defect", "7", viewer_id=1)
    text = ics.calendar_for_notes(notes)
    assert text.startswith("BEGIN:VCALENDAR")
    assert text.rstrip().endswith("END:VCALENDAR")
    for required in ("VERSION:2.0", "BEGIN:VEVENT", "UID:", "DTSTAMP:",
                     "END:VEVENT"):
        assert required in text
    assert all(len(line.encode()) <= 75 for line in text.splitlines())


def test_ics_escapes_the_characters_that_would_break_a_calendar():
    raw = "found: wire; chafed, badly\nsecond line"
    escaped = ics.escape_text(raw)
    assert "\\;" in escaped and "\\," in escaped and "\\n" in escaped
    assert "\n" not in escaped


def test_a_note_without_a_due_date_is_not_exported(con):
    store.create(con, author_id=1, anchor_type="defect", anchor_id="7",
                 body="no date")
    notes = store.for_anchor(con, "defect", "7", viewer_id=1)
    assert ics.entry_for_note(notes[0]) is None


# ── notes never reach a statistic ────────────────────────────────────────
def test_no_statistics_source_reads_the_note_table():
    """Notes are evidence a human reads, never a row in an aggregate."""
    import re
    pattern = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+note\b", re.I)
    for name in ("casebase.py", "repeat.py", "metrics.py"):
        path = ROOT / "src" / "aivionics" / "stats" / name
        assert not pattern.findall(path.read_text(encoding="utf-8"))
