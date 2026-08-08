"""Diagnose-page service logic, tested without a Qt event loop or a model.

The two behaviours worth locking down here are the ones a user would misread
if they broke silently: a missing index has to be reported as a state with a
reason, and a case with no recorded finding has to say so rather than render
an empty cell that reads as "nothing was found".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aivionics import db
from aivionics.ui.searchservice import case_rows, index_status


def _seed_defect(con, did=1, tail="N123AB", at="2025-03-04",
                 rect="REPLACED PITOT PROBE PER AMM 34-11-01-400-801."):
    con.execute(
        "INSERT INTO defect(id,aircraft_tail,reported_at,ata_ref,defect_text,"
        "rectification_text,sdr_year) VALUES(?,?,?,'34','AIRSPEED UNRELIABLE',?,2025)",
        (did, tail, at, rect))
    return did


# ── index status ─────────────────────────────────────────────────────────
def test_index_status_without_connection_explains_itself():
    status = index_status(None)
    assert not status.ready
    assert "database" in status.reason.lower()


def test_index_status_reports_missing_index_with_the_command_to_run(tmp_path):
    con = db.connect(tmp_path / "t.db")
    status = index_status(con, "v-test")
    assert not status.ready
    assert "phase2_index.py" in status.reason


def test_index_status_ready_counts_both_kinds(tmp_path):
    con = db.connect(tmp_path / "t.db")
    for kind, ref in (("task", 1), ("task", 2), ("case", 3)):
        con.execute(
            "INSERT INTO vec_index(kind,ref_id,index_version,dim,vec)"
            " VALUES(?,?,?,?,?)", (kind, ref, "v-test", 2, b"\x00" * 8))
    con.commit()
    status = index_status(con, "v-test")
    assert status.ready
    assert (status.task_vectors, status.case_vectors) == (2, 1)
    assert status.reason == ""


def test_index_status_is_scoped_to_its_version(tmp_path):
    """Standing rule 9: vectors from another model must not be counted."""
    con = db.connect(tmp_path / "t.db")
    con.execute("INSERT INTO vec_index(kind,ref_id,index_version,dim,vec)"
                " VALUES('task',1,'v-old',2,?)", (b"\x00" * 8,))
    con.commit()
    assert not index_status(con, "v-new").ready
    assert index_status(con, "v-old").ready


# ── case rows: the replaced / found split ────────────────────────────────
def test_case_row_reports_an_absent_finding_as_not_recorded(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _seed_defect(con)
    con.commit()
    row = case_rows(con, [1])[1]
    assert row.replaced.startswith("REPLACED PITOT PROBE")
    assert row.found == "not recorded"
    assert row.found_recorded is False


def test_case_row_uses_a_recorded_finding_when_one_exists(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _seed_defect(con)
    con.execute("INSERT INTO defect_finding(defect_id,finding_type,finding_text)"
                " VALUES(1,'confirmed_fault','CHAFED WIRE BEHIND P6-4')")
    con.commit()
    row = case_rows(con, [1])[1]
    assert row.found_recorded is True
    assert "CHAFED WIRE" in row.found


def test_case_row_survives_missing_text(tmp_path):
    con = db.connect(tmp_path / "t.db")
    _seed_defect(con, rect=None)
    con.execute("UPDATE defect SET aircraft_tail=NULL WHERE id=1")
    con.commit()
    row = case_rows(con, [1])[1]
    assert row.replaced == "—"
    assert row.tail == "—"


def test_case_rows_empty_input_hits_no_database(tmp_path):
    con = db.connect(tmp_path / "t.db")
    assert case_rows(con, []) == {}
