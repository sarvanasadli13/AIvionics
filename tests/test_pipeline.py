"""Splitter leak-freedom, reference validation, audit chain, schema invariants."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aivionics import audit, db
from aivionics.pipeline import references, splitter


# ── splitter ─────────────────────────────────────────────────────────────
def test_split_basic_leak_free():
    n = ("CAPT AIRSPEED UNRELIABLE ON TAKEOFF ROLL, BITE CHECK SHOWS NO "
         "CURRENT FAULTS. REMOVED AND REPLACED FIRST OFFICER PITOT TUBE "
         "PER AMM 34-11-01-400-801.")
    d, r, ok = splitter.split(n)
    assert ok
    assert "REPLACED" not in d
    assert "34-11-01-400-801" in r
    assert "AIRSPEED UNRELIABLE" in d


def test_split_citation_before_verb_is_not_leak_free():
    n = ("PER AMM 34-11-01-400-801 THE CAPT AIRSPEED WAS UNRELIABLE. "
         "REPLACED PITOT PROBE.")
    _, _, ok = splitter.split(n)
    assert not ok


def test_split_no_boundary():
    d, r, ok = splitter.split("STATIC PORT BLOCKED BY INSECT NEST.")
    assert not ok and r is None


def test_split_short_defect_rejected():
    _, _, ok = splitter.split("LEAK. REPLACED VALVE PER AMM 36-11-01-400-801.")
    assert not ok


# ── reference extraction ─────────────────────────────────────────────────
def test_reference_whitelist_kills_false_positives():
    wl = {"400", "810", "200"}
    text = ("REPLACED PART PER AMM 34-11-01-400-801. ALSO SAW ERROR "
            "12-34-56-001-123 AND CODE 11-22-33-404-999.")
    refs = references.extract(text, wl)
    assert [r["task_number"] for r in refs] == ["34-11-01-400-801"]
    assert refs[0]["manual_type"] == "AMM"
    assert refs[0]["function_code"] == "400"


def test_reference_engine_prefix():
    wl = {"810"}
    refs = references.extract("SEE FIM G73-00-00-810-A73 FOR ISOLATION", wl)
    assert refs and refs[0]["task_number"] == "G73-00-00-810-A73"


# ── audit chain ──────────────────────────────────────────────────────────
def test_audit_chain_verifies_and_detects_tamper(tmp_path):
    con = db.connect(tmp_path / "t.db")
    audit.log(con, "login", user_id=1)
    audit.log(con, "search", user_id=1, payload={"q": "pitot"})
    audit.log(con, "print", user_id=1, entity="task", entity_id="34-11-01-400-801")
    ok, n = audit.verify_chain(con)
    assert ok and n == 3
    con.execute("UPDATE audit_log SET action='x' WHERE id=2")
    con.commit()
    ok, n = audit.verify_chain(con)
    assert not ok and n == 1


# ── schema invariants ────────────────────────────────────────────────────
def test_note_anchor_required(tmp_path):
    con = db.connect(tmp_path / "t.db")
    con.execute("INSERT INTO role(name,permissions) VALUES('eng','r')")
    con.execute("INSERT INTO app_user(username,pwhash,role_id) VALUES('s',x'00',1)")
    with pytest.raises(Exception):
        con.execute("INSERT INTO note(author_id,anchor_type,anchor_id,body,"
                    "created_at) VALUES(1,'floating','x','b','now')")


def test_closure_requires_finding(tmp_path):
    con = db.connect(tmp_path / "t.db")
    con.execute("INSERT INTO defect(defect_text) VALUES('x')")
    con.execute("INSERT INTO defect_closure(defect_id,complete) VALUES(1,0)")
    with pytest.raises(Exception):
        con.execute("UPDATE defect_closure SET complete=1 WHERE defect_id=1")
    con.execute("INSERT INTO defect_finding(defect_id,finding_type) "
                "VALUES(1,'no_fault_found')")
    con.execute("UPDATE defect_closure SET complete=1 WHERE defect_id=1")  # ok now


def test_stats_guard_blocks_note_table():
    with pytest.raises(ValueError):
        db.stats_guard("SELECT COUNT(*) FROM note WHERE 1")
