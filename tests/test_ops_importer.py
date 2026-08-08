"""CAMO import (PLAN 4B.2).

The register is a mirror of someone else's legal record, so the tests that
matter are the ones about refusing: no provenance, no import; one bad row, no
partial import; a snapshot replaces rather than merges.
"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aivionics import db
from aivionics.ops import compliance, importer

HEADER = ("Registration,Item Type,Reference,Description,MEL Cat,"
          "Next Due Date,Due Hours,Cycles,Date Deferred\n")


@pytest.fixture()
def con(tmp_path):
    c = db.connect(tmp_path / "ops.db")
    compliance.ensure_schema(c)
    c.execute("INSERT INTO aircraft(tail,type,total_time_hrs,total_cycles)"
              " VALUES('N101AV','B737-8',20000,9000)")
    c.commit()
    return c


# ── header mapping ───────────────────────────────────────────────────────
def test_header_aliases_survive_case_and_punctuation():
    mapping, missing = importer.map_header(
        ["A/C Reg", "Item_Type", "DUE DATE", "Next Due Hours"])
    assert not missing
    assert set(mapping) >= {"tail", "kind", "due_date", "due_hours"}


def test_a_missing_required_column_stops_the_import():
    report = importer.read("Reference,Description\nX,Y\n",
                           source_system="AMOS")
    assert not report.ok
    assert report.missing_columns == ["tail", "kind"]
    assert "missing required" in report.summary()


# ── provenance ───────────────────────────────────────────────────────────
def test_import_without_a_source_system_is_refused():
    """Standing rule 2: a row whose origin is unknown cannot carry a
    provenance line, so it must never reach the register at all."""
    with pytest.raises(compliance.MissingProvenance):
        importer.read(HEADER + "N101AV,Check,A,B,,2026-09-01,,,\n",
                      source_system="   ")


def test_every_committed_row_carries_source_and_timestamp(con):
    report = importer.read(HEADER + "N101AV,Check,A-CHECK,A check,,2026-09-01,,,\n",
                           source_system="AMOS")
    importer.commit(con, report)
    rows = con.execute("SELECT source_system, imported_at, batch_id"
                       " FROM compliance_item").fetchall()
    assert rows and all(all(field for field in r) for r in rows)


# ── row validation ───────────────────────────────────────────────────────
def test_rows_without_a_tail_or_a_known_kind_are_rejected():
    report = importer.read(
        HEADER
        + "N101AV,Check,A,ok row,,2026-09-01,,,\n"
        + ",Check,B,no tail,,2026-09-01,,,\n"
        + "N202AV,Wombat,C,bad kind,,2026-09-01,,,\n",
        source_system="AMOS")
    assert report.accepted == 1
    assert report.rejected == 2
    assert not report.ok                       # one bad row blocks the file


def test_a_file_with_any_rejected_row_will_not_commit(con):
    report = importer.read(HEADER + ",Check,B,no tail,,2026-09-01,,,\n",
                           source_system="AMOS")
    with pytest.raises(ValueError):
        importer.commit(con, report)


# ── MEL intervals ────────────────────────────────────────────────────────
@pytest.mark.parametrize("category,days", [("B", 3), ("C", 10), ("D", 120)])
def test_a_mel_without_a_due_date_derives_one_from_its_category(category, days):
    raised = date(2026, 3, 1)
    report = importer.read(
        HEADER + f"N101AV,Deferred,MEL 34-11,pitot,{category},,,,{raised}\n",
        source_system="AMOS")
    assert report.rows[0]["due_date"] == (raised + timedelta(days=days)).isoformat()


def test_category_a_has_no_standard_interval_so_nothing_is_invented():
    """A is whatever that MEL's own remarks say. Deriving a date would be
    fabricating a limit."""
    report = importer.read(
        HEADER + "N101AV,Deferred,MEL 25-01,seat,A,,,,2026-03-01\n",
        source_system="AMOS")
    assert report.rows[0]["due_date"] is None


# ── dates and numbers ────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["2026-09-01", "01/09/2026", "01-Sep-2026",
                                  "01.09.2026", "20260901"])
def test_common_export_date_formats_parse(text):
    assert importer.parse_date(text) is not None


def test_an_unparseable_date_becomes_absent_rather_than_wrong():
    assert importer.parse_date("next tuesday") is None


def test_thousands_separators_in_hours_and_cycles():
    report = importer.read(
        HEADER + "N101AV,Check,C-CHECK,C check,,,\"21,000\",\"9,500\",\n",
        source_system="AMOS")
    assert report.rows[0]["due_hours"] == 21000.0
    assert report.rows[0]["due_cycles"] == 9500


# ── snapshot semantics ───────────────────────────────────────────────────
def test_reimporting_a_source_replaces_rather_than_accumulates(con):
    first = importer.read(
        HEADER + "N101AV,Check,A-CHECK,first,,2026-09-01,,,\n"
        + "N101AV,Check,B-CHECK,second,,2026-10-01,,,\n",
        source_system="AMOS")
    importer.commit(con, first)
    second = importer.read(HEADER + "N101AV,Check,A-CHECK,first,,2026-09-01,,,\n",
                           source_system="AMOS")
    importer.commit(con, second)
    refs = [r[0] for r in con.execute("SELECT ref FROM compliance_item")]
    # B-CHECK was closed in the CAMO and is gone from the export; merging would
    # leave it on the register forever with nothing to clear it.
    assert refs == ["A-CHECK"]


def test_another_source_is_left_alone_on_reimport(con):
    importer.commit(con, importer.read(
        HEADER + "N101AV,AD,AD-1,from trax,,2026-09-01,,,\n",
        source_system="TRAX"))
    importer.commit(con, importer.read(
        HEADER + "N101AV,Check,A-CHECK,from amos,,2026-09-01,,,\n",
        source_system="AMOS"))
    sources = {r[0] for r in con.execute(
        "SELECT DISTINCT source_system FROM compliance_item")}
    assert sources == {"TRAX", "AMOS"}


def test_the_batch_is_recorded_with_its_counts(con):
    report = importer.read(HEADER + "N101AV,Check,A,ok,,2026-09-01,,,\n",
                           source_system="AMOS")
    importer.commit(con, report)
    row = con.execute("SELECT source_system, rows_total, rows_imported"
                      " FROM import_batch").fetchone()
    assert row == ("AMOS", 1, 1)


# ── it reaches the register with a state ─────────────────────────────────
def test_imported_rows_come_back_as_evaluated_register_rows(con):
    today = date.today()
    importer.commit(con, importer.read(
        HEADER
        + f"N101AV,Check,OVERDUE,past due,,{today - timedelta(days=3)},,,\n"
        + f"N101AV,Check,LATER,far off,,{today + timedelta(days=300)},,,\n",
        source_system="AMOS"))
    rows = compliance.load_rows(con)
    assert [r.item.ref for r in rows] == ["OVERDUE", "LATER"]   # triage order
    assert rows[0].due.state == compliance.BREACHED
    assert all(r.provenance.source_system == "AMOS" for r in rows)


def test_a_stale_import_degrades_the_whole_register(con):
    importer.commit(con, importer.read(
        HEADER + "N101AV,Check,A,ok,,2027-01-01,,,\n", source_system="AMOS"))
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    con.execute("UPDATE compliance_item SET imported_at=?", (old,))
    con.commit()
    state = compliance.module_state(con)
    assert state.degraded
    assert "verify against the CAMO" in state.banner()
    row = compliance.load_rows(con, state=state)[0]
    assert row.provenance.stale
    assert row.badge_kind == "unknown"          # loses its triage colour
    assert "STALE" in row.badge_word
