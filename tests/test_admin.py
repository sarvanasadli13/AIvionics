"""Backup, restore and version reporting (PLAN Phase 6).

The restore round-trip is the point. A backup that has never been opened is a
belief, not a backup, so these tests take the copy, put it back, and check the
rows are still there.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aivionics import audit, db
from aivionics.admin import maintenance


@pytest.fixture()
def populated(tmp_path):
    path = tmp_path / "live.db"
    con = db.connect(path)
    con.execute("INSERT INTO manual(oem,aircraft_type,manual_type)"
                " VALUES('boeing','737-8','AMM')")
    for i in range(25):
        con.execute("INSERT INTO task(manual_id,task_number,title,ata_chapter)"
                    " VALUES(1,?,?,?)", (f"34-11-{i:02d}-400-801", f"task {i}", "34"))
        con.execute("INSERT INTO defect(defect_text,ata_ref) VALUES(?,'34')",
                    (f"symptom {i}",))
    audit.log(con, "login", user_id=1)
    audit.log(con, "search", user_id=1, payload={"q": "pitot"})
    con.commit()
    return path, con


# ── backup ───────────────────────────────────────────────────────────────
def test_backup_writes_a_verified_snapshot(populated, tmp_path):
    path, con = populated
    result = maintenance.backup(con, tmp_path / "b" / "snap.db")
    assert result.ok, result.summary()
    assert result.integrity == "ok"
    assert result.counts["task"] == 25
    assert result.counts == result.source_counts
    assert result.bytes_written > 0


def test_backup_will_not_silently_overwrite(populated, tmp_path):
    path, con = populated
    dest = tmp_path / "snap.db"
    assert maintenance.backup(con, dest).ok
    again = maintenance.backup(con, dest)
    assert not again.ok
    assert "already exists" in again.error
    assert maintenance.backup(con, dest, overwrite=True).ok


def test_backup_leaves_the_live_database_usable(populated, tmp_path):
    """VACUUM INTO must not disturb the source — the app keeps running."""
    path, con = populated
    maintenance.backup(con, tmp_path / "snap.db")
    con.execute("INSERT INTO defect(defect_text) VALUES('after backup')")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM defect").fetchone()[0] == 26


# ── restore round trip ───────────────────────────────────────────────────
def test_restore_round_trip_returns_every_row(populated, tmp_path):
    path, con = populated
    snap = tmp_path / "snap.db"
    assert maintenance.backup(con, snap).ok

    con.execute("DELETE FROM task")          # the disaster
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 0
    con.close()

    maintenance.restore(snap, path, overwrite=True)
    back = sqlite3.connect(path)
    assert back.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 25
    assert back.execute("SELECT COUNT(*) FROM defect").fetchone()[0] == 25
    ok, rows = audit.verify_chain(back)
    assert ok and rows == 2                  # the hash chain survives the trip
    back.close()


def test_restore_refuses_a_corrupt_backup(populated, tmp_path):
    path, con = populated
    snap = tmp_path / "snap.db"
    maintenance.backup(con, snap)
    # Corrupt the copy in place: restoring this over a working database would
    # turn a recoverable situation into an unrecoverable one.
    data = bytearray(snap.read_bytes())
    for i in range(4096, min(len(data), 60000)):
        data[i] = 0
    snap.write_bytes(bytes(data))
    with pytest.raises(ValueError):
        maintenance.restore(snap, path, overwrite=True)


def test_restore_will_not_clobber_without_being_told(populated, tmp_path):
    path, con = populated
    snap = tmp_path / "snap.db"
    maintenance.backup(con, snap)
    with pytest.raises(FileExistsError):
        maintenance.restore(snap, path)


def test_restore_removes_the_stale_write_ahead_log(populated, tmp_path):
    """A -wal left from the replaced database would be reapplied to one it no
    longer matches."""
    path, con = populated
    snap = tmp_path / "snap.db"
    maintenance.backup(con, snap)
    con.close()
    wal = path.with_name(path.name + "-wal")
    wal.write_bytes(b"stale log")
    maintenance.restore(snap, path, overwrite=True)
    assert not wal.exists()


# ── integrity and versions ───────────────────────────────────────────────
def test_integrity_check_passes_on_a_healthy_database(populated):
    _, con = populated
    assert maintenance.integrity_check(con) == "ok"


def test_versions_report_every_component_together(populated):
    """Standing rule 9: changing the embedding model invalidates every stored
    vector, so the version set is reported whole."""
    _, con = populated
    v = maintenance.versions(con, reranker="ms-marco-MiniLM-L-12-v2")
    assert v.app and v.schema and v.index_version and v.embed_model
    text = "\n".join(v.lines())
    for expected in ("Application", "Schema", "Index", "Embedding",
                     "Reranker", "LLM", "Audit chain"):
        assert expected in text
    assert v.audit_chain_ok is True
    assert v.audit_rows == 2


def test_startup_report_detects_a_tampered_audit_chain(populated):
    _, con = populated
    con.execute("UPDATE audit_log SET action='forged' WHERE id=1")
    con.commit()
    report = maintenance.startup_report(con)
    assert report["audit_chain_ok"] is False


def test_backup_name_is_sortable_and_unique_per_second():
    from datetime import datetime, timezone
    name = maintenance.default_backup_name(
        datetime(2026, 8, 8, 17, 30, 5, tzinfo=timezone.utc))
    assert name == "aivionics-20260808-173005.db"
