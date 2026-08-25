"""Gold-review domain tests.

Every test here runs against a temporary database. Nothing in this file may
touch `data/aivionics.db` or the demo database.

The first block of tests is adversarial by design: each one reproduces a
corruption that the first implementation of this feature actually permitted,
so a regression puts the failure back rather than merely lowering coverage.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from aivionics import audit, db, goldreview as G
from aivionics.ui import auth


# ── fixtures ─────────────────────────────────────────────────────────────
def _seed(con: sqlite3.Connection, cases: int) -> None:
    auth.seed(con)
    admin_role = con.execute("SELECT id FROM role WHERE name='admin'").fetchone()[0]
    eng_role = con.execute("SELECT id FROM role WHERE name='engineer'").fetchone()[0]
    con.execute("INSERT INTO app_user(id,username,pwhash,display_name,role_id,"
                "active,must_change_pw) VALUES(2,'rev2',x'00','Second Reviewer',"
                "?,1,0)", (admin_role,))
    con.execute("INSERT INTO app_user(id,username,pwhash,display_name,role_id,"
                "active,must_change_pw) VALUES(3,'eng',x'00','An Engineer',?,1,0)",
                (eng_role,))
    con.execute("INSERT INTO manual(id,oem,aircraft_type,manual_type,revision,"
                "is_current) VALUES(1,'BOEING','737-8','AMM','R1',1)")
    con.execute("INSERT INTO task(id,manual_id,task_number,title,ata_chapter,"
                "body,catalogue_only) VALUES(1,1,'34-11-00-810-801','Pitot',"
                "'34','DO THE TEST.',0)")
    con.execute("INSERT INTO task(id,manual_id,task_number,title,ata_chapter,"
                "body,catalogue_only) VALUES(2,1,'30-31-00-810-801','Probe heat',"
                "'30',NULL,1)")
    classes = ("diagnostic", "action")
    tiers = ("T1", "T2", "T3")
    for i in range(cases):
        con.execute("INSERT INTO defect(id,defect_text,ata_ref) VALUES(?,?,?)",
                    (i + 1, f"DEFECT NARRATIVE {i}", "3411"))
        chapter = f"{21 + (i % 9)}"
        stratum = f"{chapter}|{classes[i % 2]}|{tiers[i % 3]}"
        # seq starts at 0, exactly as the real queue does
        con.execute("INSERT INTO gold_queue(id,defect_id,task_number,stratum,"
                    "seq,done) VALUES(?,?,?,?,?,0)",
                    (i + 1, i + 1, "34-11-00-810-801", stratum, i))
    con.commit()


def make_db(tmp_path, cases: int = 3) -> sqlite3.Connection:
    con = sqlite3.connect(str(tmp_path / "gold.db"))
    con.executescript(db.SCHEMA)
    _seed(con, cases)
    G.migrate(con)
    return con


ADMIN = auth.User(1, "admin", "Setup Administrator", "admin", False)
REV2 = auth.User(2, "rev2", "Second Reviewer", "admin", False)
ENGINEER = auth.User(3, "eng", "An Engineer", "engineer", False)


@pytest.fixture()
def con(tmp_path):
    c = make_db(tmp_path)
    yield c
    c.close()


@pytest.fixture()
def svc(con):
    return G.GoldReviewService(con, ADMIN)


def _valid_no(**over):
    base = dict(verdict="no", correct_task_number="30-31-00-810-801",
                reason_code="wrong_ata")
    base.update(over)
    return base


# ── migration ────────────────────────────────────────────────────────────
def test_migration_is_idempotent(tmp_path):
    con = make_db(tmp_path)
    G.migrate(con)
    G.migrate(con)
    assert con.execute("SELECT COUNT(*) FROM gold_queue").fetchone()[0] == 3


def test_migration_preserves_queue_labels_and_done_flags(tmp_path):
    con = sqlite3.connect(str(tmp_path / "g.db"))
    con.executescript(db.SCHEMA)
    _seed(con, 3)
    con.execute("UPDATE gold_queue SET done=1 WHERE id=1")
    con.execute("INSERT INTO label_gold(defect_id,task_number,verdict) "
                "VALUES(1,'34-11-00-810-801','yes')")
    con.commit()
    before = con.execute("SELECT id,defect_id,task_number,stratum,seq,done "
                         "FROM gold_queue ORDER BY id").fetchall()
    G.migrate(con)
    after = con.execute("SELECT id,defect_id,task_number,stratum,seq,done "
                        "FROM gold_queue ORDER BY id").fetchall()
    assert before == after
    assert con.execute("SELECT COUNT(*) FROM label_gold").fetchone()[0] == 1


def test_legacy_schema_is_refused_rather_than_silently_rebuilt(tmp_path):
    """The first implementation kept drafts and answers in one row. There is
    no general way to tell which of those were meant to be answers, so the
    migration stops instead of guessing."""
    con = sqlite3.connect(str(tmp_path / "legacy.db"))
    con.executescript(db.SCHEMA)
    _seed(con, 2)
    con.execute("CREATE TABLE gold_review_response(id INTEGER PRIMARY KEY,"
                " queue_id INTEGER, review_kind TEXT, verdict TEXT,"
                " state TEXT, response_revision INTEGER, reviewer_user_id INTEGER,"
                " created_at TEXT, updated_at TEXT, finalized_at TEXT)")
    con.execute("INSERT INTO gold_review_response(queue_id,review_kind,verdict,"
                "state,response_revision,reviewer_user_id,created_at,updated_at)"
                " VALUES(1,'primary','yes','final',1,1,'t','t')")
    con.execute("INSERT INTO gold_review_response(queue_id,review_kind,verdict,"
                "state,response_revision,reviewer_user_id,created_at,updated_at)"
                " VALUES(2,'primary','no','draft',1,1,'t','t')")
    con.commit()
    with pytest.raises(G.MigrationBlocked):
        G.migrate(con)
    G.migrate(con, allow_legacy_rebuild=True)
    kept = con.execute("SELECT queue_id, verdict FROM gold_review_response"
                       ).fetchall()
    assert kept == [(1, "yes")], "only the final row survives the rebuild"


# ── permissions ──────────────────────────────────────────────────────────
def test_admin_gains_gold_permissions_from_migration(con):
    perms = G.permissions_for(con, ADMIN)
    assert G.GOLD_REVIEW_PERMISSION in perms
    assert G.GOLD_MANAGE_PERMISSION in perms


def test_migration_preserves_other_admin_permissions(con):
    perms = G.permissions_for(con, ADMIN)
    for original in ("users", "roles", "audit", "read", "print"):
        assert original in perms


def test_engineers_do_not_get_gold_review_automatically(con):
    assert not G.may_review(con, ENGINEER)
    assert not G.may_manage(con, ENGINEER)


def test_permissions_come_from_the_role_table_not_the_hardcoded_map(con):
    con.execute("UPDATE role SET permissions='read' WHERE name='admin'")
    con.commit()
    assert not G.may_review(con, ADMIN)


def test_unauthorised_writes_are_refused(con):
    bad = G.GoldReviewService(con, ENGINEER)
    with pytest.raises(G.NotAuthorised):
        bad.finalize(1, verdict="yes")
    with pytest.raises(G.NotAuthorised):
        bad.save_draft(1, verdict="yes")


# ── FAILURE A/B: drafts may never touch an answer ────────────────────────
def test_saving_a_draft_over_a_final_answer_leaves_the_answer_intact(svc, con):
    svc.finalize(1, verdict="yes")
    before = con.execute("SELECT * FROM gold_review_response WHERE queue_id=1"
                         ).fetchone()
    svc.save_draft(1, note="second thoughts", verdict="no")
    after = con.execute("SELECT * FROM gold_review_response WHERE queue_id=1"
                        ).fetchone()
    assert tuple(after) == tuple(before), "the answer must not move"
    assert con.execute("SELECT done FROM gold_queue WHERE id=1").fetchone()[0] == 1
    assert con.execute("SELECT verdict FROM label_gold").fetchone()[0] == "yes"
    assert svc.current_answer(1).verdict == "yes"
    assert svc.draft_for(1).verdict == "no"


def test_discarding_a_draft_over_a_final_answer_succeeds(svc, con):
    svc.finalize(1, verdict="yes")
    svc.save_draft(1, verdict="unsure", reason_code="ambiguous")
    svc.discard_draft(1)
    assert svc.draft_for(1) is None
    assert svc.current_answer(1).verdict == "yes"
    assert con.execute("SELECT COUNT(*) FROM gold_review_history").fetchone()[0] == 1
    assert con.execute("SELECT done FROM gold_queue WHERE id=1").fetchone()[0] == 1


def test_draft_never_marks_done_or_writes_label_gold(svc, con):
    svc.save_draft(1, verdict="yes")
    assert con.execute("SELECT done FROM gold_queue WHERE id=1").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM label_gold").fetchone()[0] == 0


# ── FAILURE C: cross-reviewer revision ───────────────────────────────────
def test_a_second_authorised_reviewer_can_revise_the_current_answer(con):
    G.GoldReviewService(con, ADMIN).finalize(1, verdict="yes")
    second = G.GoldReviewService(con, REV2)
    answer = second.finalize(1, change_reason="reread the procedure",
                             **_valid_no())
    assert answer.response_revision == 2
    assert answer.reviewer_user_id == REV2.id
    assert con.execute("SELECT COUNT(*) FROM gold_review_response "
                       "WHERE queue_id=1").fetchone()[0] == 1


def test_history_keeps_both_reviewer_identities(con):
    G.GoldReviewService(con, ADMIN).finalize(1, verdict="yes")
    G.GoldReviewService(con, REV2).finalize(1, change_reason="disagreed",
                                            **_valid_no())
    rows = G.GoldReviewService(con, ADMIN).history(1)
    assert len(rows) == 2
    assert rows[0]["reviewer_user_id"] == ADMIN.id
    assert rows[1]["reviewer_user_id"] == REV2.id
    assert rows[1]["previous_reviewer_user_id"] == ADMIN.id
    assert rows[1]["change_reason"] == "disagreed"


def test_first_answer_is_revision_one_and_needs_no_reason(svc):
    assert svc.finalize(1, verdict="yes").response_revision == 1


def test_revising_requires_a_change_reason(svc):
    svc.finalize(1, verdict="yes")
    with pytest.raises(G.ValidationFailed):
        svc.finalize(1, **_valid_no())
    assert svc.finalize(1, change_reason="corrected", **_valid_no()
                        ).response_revision == 2


def test_revision_replaces_the_projection_without_duplicating_it(svc, con):
    svc.finalize(1, verdict="yes")
    svc.finalize(1, change_reason="corrected", **_valid_no())
    rows = con.execute("SELECT verdict, correct_task_number FROM label_gold"
                       ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "no"


# ── validation ───────────────────────────────────────────────────────────
def test_no_and_partial_need_a_correction_or_an_explicit_unavailable(svc):
    for verdict in ("no", "partial"):
        with pytest.raises(G.ValidationFailed):
            svc.finalize(1, verdict=verdict, reason_code="wrong_ata")
    svc.finalize(1, verdict="no", reason_code="not_in_corpus",
                 correct_task_unknown=True)
    assert svc.current_answer(1).correct_task_unknown is True


def test_no_and_partial_need_a_reason(svc):
    with pytest.raises(G.ValidationFailed):
        svc.finalize(1, verdict="partial",
                     correct_task_number="30-31-00-810-801")


def test_unsure_needs_a_reason_and_counts_as_completed(svc):
    with pytest.raises(G.ValidationFailed):
        svc.finalize(1, verdict="unsure")
    svc.finalize(1, verdict="unsure", reason_code="no_procedure")
    assert svc.progress().completed == 1
    assert svc.progress().unsure == 1


def test_yes_cannot_carry_a_correction(svc):
    with pytest.raises(G.ValidationFailed):
        svc.finalize(1, verdict="yes", correct_task_number="30-31-00-810-801")
    with pytest.raises(G.ValidationFailed):
        svc.finalize(1, verdict="yes", correct_task_unknown=True)


def test_a_corrected_task_must_exist_in_the_corpus(svc):
    with pytest.raises(G.ValidationFailed):
        svc.finalize(1, verdict="no", reason_code="wrong_ata",
                     correct_task_number="99-99-99-999-999")


def test_correction_and_unavailable_cannot_both_be_set(svc):
    with pytest.raises(G.ValidationFailed):
        svc.finalize(1, verdict="no", reason_code="wrong_ata",
                     correct_task_number="30-31-00-810-801",
                     correct_task_unknown=True)


def test_a_verdict_is_required_before_finalizing(svc):
    with pytest.raises(G.ValidationFailed):
        svc.finalize(1, note="no verdict chosen")


def test_the_ui_cannot_write_fields_outside_the_questionnaire_form(svc):
    """`_merge` accepts only DRAFT_FIELDS, so the UI cannot repoint a case at
    another defect or task, nor forge a revision number or reviewer."""
    # `queue_id` is a positional parameter, so the signature itself already
    # makes it unreachable as a field — a stronger guarantee than the guard.
    for forbidden in ("defect_id", "task_number", "response_revision",
                      "reviewer_user_id", "review_kind", "finalized_at"):
        with pytest.raises(G.GoldReviewError):
            svc.finalize(1, verdict="yes", **{forbidden: 2})
    with pytest.raises(TypeError):
        svc.finalize(1, verdict="yes", queue_id=2)


def test_an_unknown_queue_case_is_refused(svc):
    with pytest.raises(G.GoldReviewError):
        svc.finalize(9999, verdict="yes")


# ── atomicity ────────────────────────────────────────────────────────────
def test_finalize_writes_answer_history_label_done_and_audit_together(svc, con):
    before_audit = con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    svc.finalize(1, verdict="yes")
    assert svc.current_answer(1) is not None
    assert con.execute("SELECT COUNT(*) FROM gold_review_history").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM label_gold").fetchone()[0] == 1
    assert con.execute("SELECT done FROM gold_queue WHERE id=1").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == \
        before_audit + 1


def test_a_failure_during_audit_rolls_the_whole_answer_back(svc, con, monkeypatch):
    """The audit row is written inside the transaction. If it cannot be
    written, the answer must not survive — a durable judgement with no record
    of who made it is the one outcome this chain exists to prevent."""
    before = {
        "answers": con.execute("SELECT COUNT(*) FROM gold_review_response"
                               ).fetchone()[0],
        "history": con.execute("SELECT COUNT(*) FROM gold_review_history"
                               ).fetchone()[0],
        "labels": con.execute("SELECT COUNT(*) FROM label_gold").fetchone()[0],
        "done": con.execute("SELECT COALESCE(SUM(done),0) FROM gold_queue"
                            ).fetchone()[0],
        "audit": con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
    }

    def boom(*_a, **_kw):
        raise sqlite3.OperationalError("audit unavailable")

    monkeypatch.setattr(G.audit, "log", boom)
    with pytest.raises(sqlite3.OperationalError):
        svc.finalize(1, verdict="yes")

    assert con.execute("SELECT COUNT(*) FROM gold_review_response").fetchone()[0] \
        == before["answers"]
    assert con.execute("SELECT COUNT(*) FROM gold_review_history").fetchone()[0] \
        == before["history"]
    assert con.execute("SELECT COUNT(*) FROM label_gold").fetchone()[0] \
        == before["labels"]
    assert con.execute("SELECT COALESCE(SUM(done),0) FROM gold_queue").fetchone()[0] \
        == before["done"]
    assert con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] \
        == before["audit"]


def test_transaction_errors_are_not_swallowed(con):
    """The first version wrapped BEGIN IMMEDIATE in a bare
    `except sqlite3.OperationalError: pass`, which hid `database is locked`
    and could commit a transaction the caller had already opened. A lock
    error must reach the caller."""

    class LockedConnection:
        """Passes everything through, but refuses to open a savepoint."""

        def __init__(self, inner):
            self._inner = inner
            self.row_factory = inner.row_factory

        def execute(self, sql, *a, **kw):
            if str(sql).upper().startswith("SAVEPOINT"):
                raise sqlite3.OperationalError("database is locked")
            return self._inner.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    svc = G.GoldReviewService(LockedConnection(con), ADMIN)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        svc.finalize(1, verdict="yes")
    assert con.execute("SELECT COUNT(*) FROM gold_review_response"
                       ).fetchone()[0] == 0


def test_the_audit_chain_still_verifies_after_questionnaire_work(svc, con):
    svc.finalize(1, verdict="yes")
    svc.finalize(1, change_reason="revised", **_valid_no())
    svc.save_draft(2, verdict="unsure", reason_code="ambiguous")
    ok, rows = audit.verify_chain(con)
    assert ok and rows >= 2


def test_a_nested_transaction_does_not_commit_the_callers_work(svc, con):
    con.execute("BEGIN")
    con.execute("INSERT INTO defect(id,defect_text) VALUES(99,'CALLER ROW')")
    svc.finalize(1, verdict="yes")
    con.rollback()
    assert con.execute("SELECT COUNT(*) FROM defect WHERE id=99").fetchone()[0] == 0


# ── progress and navigation ──────────────────────────────────────────────
def test_seq_may_start_at_zero_and_positions_are_one_based(svc):
    assert svc.sequences()[0] == 0
    assert svc.position_of(0) == 1
    assert svc.pair(0).position == 1


def test_resume_picks_the_first_unanswered_case(svc):
    assert svc.resume_seq() == 0
    svc.finalize(1, verdict="yes")
    assert svc.resume_seq() == 1


def test_progress_counts_and_unsure_rate(svc):
    svc.finalize(1, verdict="yes")
    svc.finalize(2, verdict="unsure", reason_code="ambiguous")
    p = svc.progress()
    assert (p.total, p.completed, p.yes, p.unsure) == (3, 2, 1, 1)
    assert p.unsure_pct == pytest.approx(50.0)


# ── leak-free presentation ───────────────────────────────────────────────
def test_the_pair_carries_no_leaking_field(svc):
    pair = svc.pair(0)
    for name in G.LEAKING_FIELDS:
        assert not hasattr(pair, name), f"{name} must never reach the reviewer"


def test_a_professional_review_never_loads_the_primary_verdict(con):
    G.GoldReviewService(con, ADMIN).finalize(1, verdict="yes")
    pro = G.GoldReviewService(con, ADMIN, review_kind="professional")
    assert pro.current_answer(1) is None
    assert pro.pair(0).answer is None


def test_a_primary_review_never_loads_the_professional_verdict(con):
    pro = G.GoldReviewService(con, ADMIN, review_kind="professional")
    pro.finalize(1, verdict="no", correct_task_number="30-31-00-810-801",
                 reason_code="wrong_ata")
    primary = G.GoldReviewService(con, ADMIN)
    assert primary.current_answer(1) is None


def test_a_professional_answer_never_touches_label_gold_or_done(con):
    pro = G.GoldReviewService(con, ADMIN, review_kind="professional")
    pro.finalize(1, verdict="yes")
    assert con.execute("SELECT COUNT(*) FROM label_gold").fetchone()[0] == 0
    assert con.execute("SELECT done FROM gold_queue WHERE id=1").fetchone()[0] == 0


def test_the_correction_search_is_plain_sql(svc):
    hits = svc.search_tasks(number="30-31")
    assert [h["task_number"] for h in hits] == ["30-31-00-810-801"]
    assert svc.search_tasks(title="pitot")[0]["task_number"] == "34-11-00-810-801"
    assert svc.search_tasks(chapter="34") and not svc.search_tasks(chapter="99")


def test_catalogue_only_rows_explain_themselves_honestly(con):
    con.execute("UPDATE gold_queue SET task_number='30-31-00-810-801' WHERE id=1")
    con.commit()
    pair = G.GoldReviewService(con, ADMIN).pair(0)
    assert pair.catalogue_only
    assert "catalogue only" in pair.body_unavailable_reason
    assert "not evidence that the task is wrong" in pair.body_unavailable_reason


# ── freezing ─────────────────────────────────────────────────────────────
def _complete_set(tmp_path, cases: int = 4):
    con = make_db(tmp_path, cases=cases)
    svc = G.GoldReviewService(con, ADMIN)
    for qid in range(1, cases + 1):
        svc.finalize(qid, verdict="yes")
    return con, svc


def test_a_complete_consistent_set_can_freeze(tmp_path):
    con, svc = _complete_set(tmp_path, cases=4)
    assert svc.readiness(expected=4) == []
    assert svc.freeze(expected=4) == 1
    assert svc.is_frozen()


@pytest.mark.parametrize("cases", [0, 1, 3, 5])
def test_freeze_refuses_a_set_of_the_wrong_size(tmp_path, cases):
    con, svc = _complete_set(tmp_path, cases=max(cases, 1))
    if cases == 0:
        con.execute("DELETE FROM gold_review_response")
        con.execute("DELETE FROM label_gold")
        con.execute("UPDATE gold_queue SET done=0")
        con.commit()
    with pytest.raises(G.ValidationFailed):
        svc.freeze(expected=4)


def test_freeze_refuses_a_missing_label_gold_row(tmp_path):
    con, svc = _complete_set(tmp_path, cases=4)
    con.execute("DELETE FROM label_gold WHERE defect_id=1")
    con.commit()
    problems = svc.readiness(expected=4)
    assert problems and any("label_gold" in p for p in problems)
    with pytest.raises(G.ValidationFailed):
        svc.freeze(expected=4)


def test_freeze_refuses_an_extra_label_gold_row(tmp_path):
    con, svc = _complete_set(tmp_path, cases=4)
    con.execute("INSERT INTO label_gold(defect_id,task_number,verdict) "
                "VALUES(1,'30-31-00-810-801','yes')")
    con.commit()
    with pytest.raises(G.ValidationFailed):
        svc.freeze(expected=4)


def test_freeze_refuses_a_mismatched_label_gold_verdict(tmp_path):
    con, svc = _complete_set(tmp_path, cases=4)
    con.execute("UPDATE label_gold SET verdict='no' WHERE defect_id=1")
    con.commit()
    problems = svc.readiness(expected=4)
    assert any("do not match" in p for p in problems)
    with pytest.raises(G.ValidationFailed):
        svc.freeze(expected=4)


def test_freeze_refuses_a_mismatched_done_flag(tmp_path):
    con, svc = _complete_set(tmp_path, cases=4)
    con.execute("UPDATE gold_queue SET done=0 WHERE id=1")
    con.commit()
    problems = svc.readiness(expected=4)
    assert any("done" in p for p in problems)
    with pytest.raises(G.ValidationFailed):
        svc.freeze(expected=4)


def test_freeze_refuses_while_a_draft_remains(tmp_path):
    con, svc = _complete_set(tmp_path, cases=4)
    svc.save_draft(1, note="still thinking")
    problems = svc.readiness(expected=4)
    assert any("draft" in p for p in problems)
    with pytest.raises(G.ValidationFailed):
        svc.freeze(expected=4)


def test_freeze_refuses_a_queue_row_pointing_at_a_missing_defect(tmp_path):
    con, svc = _complete_set(tmp_path, cases=4)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("DELETE FROM defect WHERE id=1")
    con.commit()
    problems = svc.readiness(expected=4)
    assert any("missing defect" in p for p in problems)


def test_freeze_needs_management_authority(tmp_path):
    con, _ = _complete_set(tmp_path, cases=4)
    con.execute("UPDATE role SET permissions='read,gold_review' WHERE name='admin'")
    con.commit()
    with pytest.raises(G.NotAuthorised):
        G.GoldReviewService(con, ADMIN).freeze(expected=4)


def test_a_frozen_set_refuses_edits(tmp_path):
    con, svc = _complete_set(tmp_path, cases=4)
    svc.freeze(expected=4)
    with pytest.raises(G.SetFrozen):
        svc.finalize(1, change_reason="x", **_valid_no())
    with pytest.raises(G.SetFrozen):
        svc.save_draft(1, note="x")


def test_reopening_needs_authority_and_a_reason(tmp_path):
    con, svc = _complete_set(tmp_path, cases=4)
    svc.freeze(expected=4)
    with pytest.raises(G.ValidationFailed):
        svc.reopen("")
    con.execute("UPDATE role SET permissions='read,gold_review' WHERE name='admin'")
    con.commit()
    with pytest.raises(G.NotAuthorised):
        G.GoldReviewService(con, ADMIN).reopen("a reason")


def test_reopen_preserves_the_release_and_the_next_freeze_makes_a_new_version(
        tmp_path):
    con, svc = _complete_set(tmp_path, cases=4)
    svc.freeze(expected=4)
    first_fp = con.execute("SELECT queue_fingerprint, response_fingerprint "
                           "FROM gold_set_release WHERE version=1").fetchone()
    svc.reopen("second reviewer disagreed")
    svc.finalize(1, change_reason="corrected on review", **_valid_no())
    assert svc.freeze(expected=4) == 2
    rows = con.execute("SELECT version, status, unlock_reason FROM "
                       "gold_set_release ORDER BY version").fetchall()
    assert [r[0] for r in rows] == [1, 2]
    assert rows[0][1] == "reopened" and rows[0][2] == "second reviewer disagreed"
    kept = con.execute("SELECT queue_fingerprint, response_fingerprint "
                       "FROM gold_set_release WHERE version=1").fetchone()
    assert tuple(kept) == tuple(first_fp), "history must stay intact"


def test_fingerprints_are_deterministic_and_answer_sensitive(tmp_path):
    con, svc = _complete_set(tmp_path, cases=4)
    qf1, rf1 = svc.fingerprints()
    assert (qf1, rf1) == svc.fingerprints()
    svc.finalize(1, change_reason="changed", **_valid_no())
    qf2, rf2 = svc.fingerprints()
    assert qf1 == qf2, "the queue did not change"
    assert rf1 != rf2, "the judgement did"


# ── the professional overlap subset ──────────────────────────────────────
def test_the_professional_subset_is_exactly_fifty_and_deterministic(tmp_path):
    con = make_db(tmp_path, cases=400)
    first = G.professional_subset(con, 50)
    assert len(first) == 50 and len(set(first)) == 50
    assert first == G.professional_subset(con, 50)


def test_the_professional_subset_spans_the_real_strata(tmp_path):
    con = make_db(tmp_path, cases=400)
    chosen = set(G.professional_subset(con, 50))
    strata = {r[0] for r in con.execute(
        "SELECT stratum FROM gold_queue WHERE id IN "
        f"({','.join(str(i) for i in chosen)})")}
    assert len({s.split("|")[0] for s in strata}) > 1, "spans chapters"
    assert len({s.split("|")[1] for s in strata}) == 2, "spans diagnostic+action"
    assert len({s.split("|")[2] for s in strata}) == 3, "spans all three tiers"


def test_the_professional_subset_ignores_primary_answers(tmp_path):
    con = make_db(tmp_path, cases=400)
    before = G.professional_subset(con, 50)
    svc = G.GoldReviewService(con, ADMIN)
    for qid in range(1, 30):
        svc.finalize(qid, verdict="yes")
    assert G.professional_subset(con, 50) == before


# ── evaluation semantics ─────────────────────────────────────────────────
def test_the_gold_loader_reads_only_finalized_primary_answers(con):
    svc = G.GoldReviewService(con, ADMIN)
    svc.finalize(1, verdict="yes")
    svc.save_draft(2, verdict="no")
    G.GoldReviewService(con, ADMIN, review_kind="professional").finalize(
        3, verdict="unsure", reason_code="ambiguous")
    labels = G.load_gold_labels(con)
    assert [lab.queue_id for lab in labels] == [1]


def test_verdict_semantics_are_conservative(con):
    svc = G.GoldReviewService(con, ADMIN)
    svc.finalize(1, verdict="yes")
    svc.finalize(2, verdict="partial",
                 correct_task_number="30-31-00-810-801",
                 reason_code="wrong_entry_point")
    svc.finalize(3, verdict="unsure", reason_code="ambiguous")
    s = G.score_gold(G.load_gold_labels(con))
    assert (s.total, s.correct, s.incorrect, s.partial, s.excluded) == \
        (3, 1, 1, 1, 1)
    assert s.precision == pytest.approx(0.5), "partial counts against"
    assert s.unsure_rate == pytest.approx(1 / 3)


def test_agreement_reports_raw_and_chance_corrected():
    out = G.agreement({1: "yes", 2: "no", 3: "yes"},
                      {1: "yes", 2: "yes", 3: "yes"})
    assert out["n"] == 3 and out["agreed"] == 2
    assert out["raw"] == pytest.approx(2 / 3)
    assert "kappa" in out


# ── the gold evaluation command ──────────────────────────────────────────
def _eval_module():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_gold.py"
    spec = importlib.util.spec_from_file_location("evaluate_gold", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gold_evaluation_refuses_when_nothing_is_frozen(tmp_path):
    ev = _eval_module()
    con, _svc = _complete_set(tmp_path, cases=4)
    with pytest.raises(ev.NotReleased, match="no gold release"):
        ev.released(con)


def test_gold_evaluation_refuses_a_reopened_release(tmp_path):
    ev = _eval_module()
    con, svc = _complete_set(tmp_path, cases=4)
    svc.freeze(expected=4)
    svc.reopen("a reviewer disagreed")
    with pytest.raises(ev.NotReleased, match="reopened"):
        ev.released(con)


def test_gold_evaluation_refuses_when_the_answers_moved_after_freezing(tmp_path):
    ev = _eval_module()
    con, svc = _complete_set(tmp_path, cases=4)
    svc.freeze(expected=4)
    release = ev.released(con)
    ev.verify_fingerprints(con, release)          # matches while untouched
    con.execute("UPDATE gold_review_response SET verdict='no' WHERE queue_id=1")
    con.commit()
    with pytest.raises(ev.NotReleased, match="answers have changed"):
        ev.verify_fingerprints(con, release)


def test_gold_evaluation_reports_a_frozen_release(tmp_path):
    ev = _eval_module()
    con, svc = _complete_set(tmp_path, cases=4)
    svc.finalize(2, change_reason="checked", verdict="unsure",
                 reason_code="ambiguous")
    svc.freeze(expected=4)
    data = ev.report(con, ev.released(con))
    assert data["release_version"] == 1
    assert data["counts"]["yes"] == 3 and data["counts"]["unsure"] == 1
    assert data["excluded_unsure"] == 1
    assert data["scored"] == 3
    assert data["unsure_rate"] == pytest.approx(0.25)
    assert "not for training" in data["held_out"]


def test_gold_evaluation_opens_the_database_read_only(tmp_path):
    ev = _eval_module()
    con, svc = _complete_set(tmp_path, cases=4)
    svc.freeze(expected=4)
    con.close()
    ro = ev.open_readonly(tmp_path / "gold.db")
    assert ev.report(ro, ev.released(ro))["release_version"] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("UPDATE gold_queue SET done=0")
