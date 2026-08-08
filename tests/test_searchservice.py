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
from aivionics.ui.searchservice import CaseRow, case_rows, index_status


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


# ── applicability wording (standing rule 8) ──────────────────────────────
def test_no_machine_token_reaches_the_applicability_label():
    """Fail-closed states must read as instructions, not as engine tokens.

    `not_checked` on screen tells an engineer nothing about whether the task
    applies to the aircraft in front of them.
    """
    from aivionics.retrieval.search import Effectivity, resolve_applicability
    from aivionics.ui.pages.diagnose import APPLICABILITY_TEXT

    tail = Effectivity(tail="N123AB")
    produced = {
        resolve_applicability({}, None, {}),                          # not_checked
        resolve_applicability(
            {"applic_refs": "", "effectivity_raw": "TBC ALL"}, tail, {}),
        resolve_applicability(
            {"applic_refs": "", "effectivity_raw": "AIRPLANES 1-40"}, tail, {}),
        resolve_applicability(
            {"applic_refs": "AP1", "effectivity_raw": ""}, tail,
            {"AP1": {"tail": "N123AB"}}),                             # applicable
        resolve_applicability(
            {"applic_refs": "AP1", "effectivity_raw": ""}, tail,
            {"AP1": {"tail": "N999ZZ"}}),                             # not_applicable
    }

    for token in produced:
        assert token in APPLICABILITY_TEXT, f"unmapped applicability: {token}"
    for text in APPLICABILITY_TEXT.values():
        assert "_" not in text


# ── the whole run(), which had no coverage until it broke ────────────────
def test_run_returns_every_key_the_page_reads(tmp_path, monkeypatch):
    """A regression test for a NameError that reached a live render.

    `run()` assembles the dict the Diagnose page unpacks, and nothing exercised
    it: a local named `case_rows` shadowed the module function of the same
    name, so the first real call raised UnboundLocalError. Unit tests on the
    parts could not catch that — only calling the whole thing does.
    """
    from aivionics.retrieval.embedder import FakeEmbedder
    from aivionics.retrieval.indexer import build_all
    from aivionics.ui.searchservice import SearchService

    path = tmp_path / "s.db"
    con = db.connect(path)
    con.execute("INSERT INTO manual(id,oem,aircraft_type,manual_type,revision)"
                " VALUES(1,'boeing','737-8','AMM','48')")
    for i in range(6):
        con.execute(
            "INSERT INTO task(manual_id,task_number,title,ata_chapter,"
            "ata_section,ata_subject,embed_text) VALUES(1,?,?,?,?,?,?)",
            (f"34-11-{i:02d}-400-801", f"Pitot probe variant {i}", "34",
             "11", f"{i:02d}", f"Navigation > 34-11-{i:02d} > Pitot probe {i}"))
    for i in range(4):
        _seed_defect(con, did=i + 1, tail=f"N{i}00AV")
    con.execute("INSERT INTO label_silver(defect_id,task_number,leak_free,split)"
                " VALUES(1,'34-11-00-400-801',1,'test')")
    con.commit()

    embedder = FakeEmbedder()
    build_all(con, embedder)
    con.commit()
    con.close()

    service = SearchService(path, index_version=embedder.index_version)
    monkeypatch.setattr(service, "searcher", lambda: __import__(
        "aivionics.retrieval.search", fromlist=["Searcher"]
    ).Searcher(service._connect(), embedder,
               index_version=embedder.index_version))
    # no model on a test machine, and the search must not depend on one
    monkeypatch.setattr(service, "llm", lambda: None)

    result = service.run("AIRSPEED UNRELIABLE ON TAKEOFF", top_k=5)
    for key in ("query", "tasks", "cases", "case_rows", "abstain",
                "calibrated", "calibration_note", "chapter", "summary"):
        assert key in result, f"the page reads {key!r} and run() did not set it"
    assert result["query"].startswith("AIRSPEED")
    assert isinstance(result["case_rows"], list)
    assert all(isinstance(r, CaseRow) for r in result["case_rows"])
    # uncalibrated install: answer rather than suppress everything
    assert result["abstain"] is False
    assert result["summary"] is None or result["summary"].accepted is False
    service.close()
