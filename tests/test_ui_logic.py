"""UI logic tests — no Qt event loop, no widgets.

Everything the shell relies on that can be got wrong silently: the theme
token contract, password handling, the locator print block, the adjudication
queue, and the audit chain across a simulated session.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from aivionics import audit, db
from aivionics.ui import auth, printing, store
from aivionics.ui import theme as T
from aivionics.ui.adjudicator import (VERDICT_KEYS, AdjudicationQueue,
                                      QueueMissing)


@pytest.fixture
def con(tmp_path):
    connection = db.connect(tmp_path / "fixture.db")
    auth.seed(connection)
    auth.reset_throttle()
    yield connection
    connection.close()


# ── theme ───────────────────────────────────────────────────────────────

def test_qss_generated_for_every_theme():
    for name in T.THEMES:
        qss = T.build_qss(name)
        assert "$" not in qss, f"unresolved placeholder in the {name} stylesheet"
        assert T.THEMES[name]["cyf"] in qss
        assert T.THEMES[name]["line"] in qss


def test_unknown_theme_fails_loudly():
    with pytest.raises(KeyError):
        T.build_qss("solarized")


def test_light_is_the_default_and_is_white_on_a_sky_ground():
    assert T.DEFAULT_THEME == "light"
    assert T.LIGHT["s1"] == "#FFFFFF"
    assert T.LIGHT["bg"] == "#EAF2F9"
    assert T.LIGHT["line"] == "#CBE0F0"


def test_accent_is_never_a_status_colour():
    """§4A.1 rule 1: red, amber and green are reserved for data."""
    assert T.accent_is_status_free()


def test_every_status_carries_an_icon_and_a_word():
    """§4A.1 rule 2: status is never colour alone."""
    for kind in T.STATUS:
        spec = T.status_style(kind)
        assert spec["word"].strip()
        assert spec["icon"].strip()
        assert spec["color"].startswith("#")


def test_data_rows_stay_dense():
    assert 28 <= T.ROW_HEIGHT <= 32


def test_font_family_can_be_injected():
    qss = T.build_qss("light", ui_family="Segoe UI", mono_family="Consolas")
    assert "font-family: Segoe UI;" in qss


# ── authentication ──────────────────────────────────────────────────────

def test_seed_creates_roles_and_a_setup_admin(con):
    roles = {r[0] for r in con.execute("SELECT name FROM role")}
    assert {"admin", "engineer"} <= roles
    row = con.execute(
        "SELECT username, must_change_pw FROM app_user").fetchone()
    assert row == (auth.SETUP_USERNAME, 1)


def test_seed_is_idempotent(con):
    auth.seed(con)
    auth.seed(con)
    assert con.execute("SELECT COUNT(*) FROM app_user").fetchone()[0] == 1


def test_setup_login_succeeds_but_demands_a_new_password(con):
    result = auth.authenticate(con, auth.SETUP_USERNAME, auth.SETUP_PASSWORD)
    assert result.ok
    assert result.user.must_change_pw is True
    assert result.user.role == "admin"


def test_wrong_password_is_rejected_without_revealing_which_part(con):
    bad_pw = auth.authenticate(con, auth.SETUP_USERNAME, "not-the-password")
    unknown = auth.authenticate(con, "nobody", "not-the-password")
    assert not bad_pw.ok and not unknown.ok
    assert bad_pw.reason == unknown.reason


def test_must_change_flow_clears_the_flag(con):
    result = auth.authenticate(con, auth.SETUP_USERNAME, auth.SETUP_PASSWORD)
    updated = auth.change_password(con, result.user, "a-much-better-secret")
    assert updated.must_change_pw is False

    again = auth.authenticate(con, auth.SETUP_USERNAME, "a-much-better-secret")
    assert again.ok and again.user.must_change_pw is False
    assert not auth.authenticate(con, auth.SETUP_USERNAME,
                                 auth.SETUP_PASSWORD).ok


def test_weak_or_unchanged_passwords_are_refused(con):
    user = auth.authenticate(con, auth.SETUP_USERNAME, auth.SETUP_PASSWORD).user
    with pytest.raises(ValueError):
        auth.change_password(con, user, "short")
    with pytest.raises(ValueError):
        auth.change_password(con, user, auth.SETUP_PASSWORD)


def test_repeated_failures_are_throttled(con):
    for _ in range(auth.LOCKOUT_AFTER):
        auth.authenticate(con, auth.SETUP_USERNAME, "wrong")
    result = auth.authenticate(con, auth.SETUP_USERNAME, auth.SETUP_PASSWORD)
    assert not result.ok and "wait" in result.reason.lower()
    auth.reset_throttle(auth.SETUP_USERNAME)
    assert auth.authenticate(con, auth.SETUP_USERNAME, auth.SETUP_PASSWORD).ok


def test_signature_is_initial_plus_surname():
    user = auth.User(1, "s.asadli", "Sarvan Asadli", "engineer", False)
    assert auth.signature(user) == "S. Asadli"
    assert auth.signature(None) == "unknown"


def test_page_navigation_is_not_an_audited_action():
    """Standing rule 6 / BetrVG §87(1)(6): no per-user screen tracking."""
    assert not any("nav" in action for action in auth.AUDITED_ACTIONS)


# ── audit chain ─────────────────────────────────────────────────────────

def test_chain_intact_across_a_simulated_session(con):
    result = auth.authenticate(con, auth.SETUP_USERNAME, auth.SETUP_PASSWORD)
    user = auth.change_password(con, result.user, "a-much-better-secret")
    auth.authenticate(con, auth.SETUP_USERNAME, "wrong")
    printing.record_print(con, user_id=user.id, task_id=None,
                          manual_revision="48", aircraft_id=None,
                          task_number="34-11-01-400-801")
    auth.logout(con, user)

    ok, rows = audit.verify_chain(con)
    assert ok
    assert rows >= 5
    actions = [r[0] for r in con.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions[0] == "login"
    assert actions[-1] == "logout"
    assert "print" in actions and "login_failed" in actions


def test_tampering_with_a_row_breaks_the_chain(con):
    auth.authenticate(con, auth.SETUP_USERNAME, auth.SETUP_PASSWORD)
    auth.logout(con, auth.User(1, "admin", "admin", "admin", False))
    con.execute("UPDATE audit_log SET action='nothing_happened' WHERE id=1")
    con.commit()
    ok, _ = audit.verify_chain(con)
    assert not ok


# ── locator printing (standing rule 1) ──────────────────────────────────

TASK = {
    "id": 7,
    "task_number": "34-11-01-400-801",
    "title": "Pitot probe — removal / installation",
    "effectivity_raw": "MSN 28xxx–41xxx",
    # Present on purpose: a real task row carries the body, and the printer
    # must not let it through.
    "body": "1. Remove the four attach bolts and pull the probe clear.",
}
MANUAL = {"aircraft_type": "B737-8", "manual_type": "AMM", "revision": "48",
          "revision_date": "2026-06-15"}
AIRCRAFT = {"id": 3, "tail": "4K-AZ12"}
WHEN = datetime(2026, 8, 6, 14, 32, tzinfo=timezone.utc)


def test_locator_block_matches_the_specified_format():
    text = printing.format_locator(TASK, MANUAL, AIRCRAFT, "S. Asadli", WHEN)
    assert text.splitlines() == [
        "B737-8   ·   AMM Rev 48   ·   issued 2026-06-15",
        "TASK 34-11-01-400-801   PITOT PROBE — REMOVAL / INSTALLATION",
        "Effectivity: MSN 28xxx–41xxx        Aircraft: 4K-AZ12",
        "Printed 2026-08-06 14:32Z by S. Asadli",
        printing.BANNER,
    ]


def test_locator_never_contains_body_text():
    """Regression: the print path emits locators only, forever."""
    text = printing.format_locator(TASK, MANUAL, AIRCRAFT, "S. Asadli", WHEN)
    assert "attach bolts" not in text
    assert TASK["body"] not in text
    for word in ("Remove", "pull", "probe clear"):
        assert word not in text
    assert "LOCATOR ONLY" in text


def test_unresolved_effectivity_fails_closed():
    """Standing rule 8: never render an unresolved applicability as clean."""
    task = dict(TASK, effectivity_raw=None)
    text = printing.format_locator(task, MANUAL, AIRCRAFT, "S. Asadli", WHEN)
    assert printing.UNRESOLVED in text


def test_print_writes_both_a_print_log_row_and_an_audit_row(con):
    user = auth.authenticate(con, auth.SETUP_USERNAME, auth.SETUP_PASSWORD).user
    printing.print_locator(con, TASK, MANUAL, AIRCRAFT, user)
    assert con.execute("SELECT COUNT(*) FROM print_log").fetchone()[0] == 1
    assert con.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='print'").fetchone()[0] == 1
    ok, _ = audit.verify_chain(con)
    assert ok


# ── adjudication queue (PLAN 0.7) ───────────────────────────────────────

def _seed_queue(con: sqlite3.Connection, n: int = 5) -> None:
    con.execute(
        "INSERT INTO manual(id,oem,aircraft_type,manual_type,revision,is_current) "
        "VALUES(1,'boeing','B737-8','AMM','48',1)")
    con.execute(
        "INSERT INTO task(id,manual_id,task_number,function_code,title,"
        "ata_chapter,body) VALUES(1,1,'34-11-01-200-801','200',"
        "'Pitot probe heater — inspection / check','34','1. General ...')")
    con.execute("INSERT INTO task_section(task_id,seq,kind,text) "
                "VALUES(1,1,'warning','DO NOT TOUCH THE PROBE.')")
    con.execute("INSERT INTO task_section(task_id,seq,kind,text) "
                "VALUES(1,2,'caution','DO NOT USE A SOLVENT.')")
    for i in range(1, n + 1):
        con.execute(
            "INSERT INTO defect(id,aircraft_tail,reported_at,ata_ref,defect_text) "
            "VALUES(?,?,?,'3411',?)",
            (i, f"N{100 + i}AA", "2026-05-01", f"AIRSPEED COMPLAINT {i}"))
        con.execute(
            "INSERT INTO gold_queue(defect_id,task_number,stratum,seq,done) "
            "VALUES(?,'34-11-01-200-801',?,?,0)",
            (i, "ata34-diagnostic" if i % 2 else "ata34-action", i))
    con.commit()


def test_missing_queue_is_reported_clearly(con):
    con.execute("DROP TABLE gold_queue")
    con.commit()
    queue = AdjudicationQueue(con)
    assert queue.exists is False
    with pytest.raises(QueueMissing):
        queue.progress()


def test_pair_carries_the_defect_the_task_and_its_hazards(con):
    _seed_queue(con)
    pair = AdjudicationQueue(con).pair(1)
    assert pair.defect_text == "AIRSPEED COMPLAINT 1"
    assert pair.tail == "N101AA"
    assert pair.task_title.startswith("Pitot probe heater")
    assert pair.manual_type == "AMM" and pair.revision == "48"
    assert pair.warnings == ["DO NOT TOUCH THE PROBE."]
    assert pair.cautions == ["DO NOT USE A SOLVENT."]
    assert pair.has_body


def test_commit_writes_the_verdict_and_marks_the_row_done(con):
    _seed_queue(con)
    queue = AdjudicationQueue(con)
    queue.commit(1, "yes", correct_task_number=" 34-11-00-810-801 ")

    row = con.execute(
        "SELECT defect_id, task_number, verdict, correct_task_number, "
        "adjudicated_at FROM label_gold").fetchone()
    assert row[0] == 1
    assert row[2] == "yes"
    assert row[3] == "34-11-00-810-801"      # whitespace stripped
    assert row[4]
    assert con.execute(
        "SELECT done FROM gold_queue WHERE seq=1").fetchone()[0] == 1


def test_unsure_is_a_first_class_verdict(con):
    """PLAN §5: forcing a guess into three buckets is what ruins a gold set."""
    _seed_queue(con)
    queue = AdjudicationQueue(con)
    queue.commit(1, "unsure")
    assert queue.progress().unsure == 1
    assert set(VERDICT_KEYS.values()) == {"yes", "no", "partial", "unsure"}


def test_an_invalid_verdict_is_refused(con):
    _seed_queue(con)
    with pytest.raises(ValueError):
        AdjudicationQueue(con).commit(1, "probably")


def test_re_adjudicating_replaces_rather_than_duplicates(con):
    _seed_queue(con)
    queue = AdjudicationQueue(con)
    queue.commit(2, "no")
    queue.commit(2, "partial", correct_task_number="34-11-01-810-801")
    rows = con.execute(
        "SELECT verdict, correct_task_number FROM label_gold").fetchall()
    assert rows == [("partial", "34-11-01-810-801")]


def test_navigation_and_resume(con):
    _seed_queue(con, n=5)
    queue = AdjudicationQueue(con)
    assert queue.resume_seq() == 1

    queue.commit(1, "yes")
    queue.commit(2, "no")
    assert queue.resume_seq() == 3, "resumes at the first pending pair"

    assert queue.next_seq(3) == 4
    assert queue.previous_seq(3) == 2
    assert queue.previous_seq(1) is None
    assert queue.next_seq(5) is None

    # A revisited pair still shows the verdict already recorded for it.
    assert queue.pair(1).verdict == "yes"


def test_progress_reports_totals_and_strata(con):
    _seed_queue(con, n=5)
    queue = AdjudicationQueue(con)
    queue.commit(1, "yes")
    progress = queue.progress()
    assert (progress.done, progress.total) == (1, 5)
    assert progress.pct == pytest.approx(20.0)
    assert dict((name, total) for name, _done, total in progress.per_stratum) == {
        "ata34-diagnostic": 3, "ata34-action": 2}


def test_resume_holds_at_the_end_when_everything_is_done(con):
    _seed_queue(con, n=2)
    queue = AdjudicationQueue(con)
    queue.commit(1, "yes")
    queue.commit(2, "no")
    assert queue.resume_seq() == 2
    assert queue.progress().done == 2


# ── read-only corpus access ─────────────────────────────────────────────

def test_missing_database_is_not_an_error(tmp_path):
    assert store.open_readonly(tmp_path / "nope.db") is None


def test_a_file_that_is_not_a_database_is_not_an_error(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_text("this is not sqlite")
    assert store.open_readonly(junk) is None


def test_readonly_connection_refuses_writes(tmp_path):
    path = tmp_path / "corpus.db"
    db.connect(path).close()
    ro = store.open_readonly(path)
    assert ro is not None
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO role(name,permissions) VALUES('x','y')")
    ro.close()


def test_corpus_reader_degrades_gracefully_without_a_database():
    reader = store.CorpusReader(None)
    assert reader.available is False
    assert reader.manuals() == []
    assert reader.chapters(1) == []
    assert reader.counts() == {"tasks": 0, "amm": 0, "fim": 0, "cases": 0,
                               "aircraft": 0}


def test_online_features_default_to_off(con):
    assert store.online_enabled(con) is False
    assert store.online_enabled(None) is False
    store.set_setting(con, "online_enabled", "1")
    assert store.online_enabled(con) is True


def test_settings_tolerate_a_database_without_the_table(tmp_path):
    path = tmp_path / "bare.db"
    bare = sqlite3.connect(path)
    assert store.get_setting(bare, "online_enabled") == "0"
    assert store.get_setting(None, "theme") == "light"
    bare.close()


# ── PDF chapter resolution and task page lookup ─────────────────────────

def _build_pdf(path, pages: list[str]) -> None:
    """A miniature AMM chapter, one text block per page."""
    import fitz
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((54, 72), text, fontsize=9)
    doc.save(str(path))
    doc.close()


def test_flatten_removes_injected_whitespace():
    from aivionics.ui import pdfsource
    assert pdfsource.flatten("34-41-1 1-020-002") == "34-41-11-020-002"
    assert pdfsource.flatten(" 34-11-01-400-801\n") == "34-11-01-400-801"


def test_chapter_of_pads_to_two_digits():
    from aivionics.ui import pdfsource
    assert pdfsource.chapter_of("34-11-01-400-801") == "34"
    assert pdfsource.chapter_of("5-10-00-000-801") == "05"


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """A stand-in AMM directory, with config.AMM_DIR pointed at it.

    Without this the resolver falls back to the real corpus on D:, and these
    tests would pass or fail depending on whether that drive is connected.
    """
    from aivionics import config
    amm = tmp_path / "AMM"
    amm.mkdir()
    for chapter in ("34", "31"):
        (amm / f"{chapter} AMM-1176.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(config, "AMM_DIR", amm)
    return amm


def test_chapter_pdf_resolves_by_filename_prefix(corpus):
    from aivionics.ui import pdfsource
    assert pdfsource.resolve_chapter_pdf("34", corpus).name == "34 AMM-1176.pdf"
    assert pdfsource.resolve_chapter_pdf("31", corpus).name == "31 AMM-1176.pdf"
    assert pdfsource.resolve_chapter_pdf("99", corpus) is None


def test_a_stale_source_path_falls_back_to_the_configured_corpus(corpus):
    """manual.source_file records where the ingest ran, which may have moved."""
    from aivionics.ui import pdfsource
    found = pdfsource.resolve_chapter_pdf("34", r"Z:\not-mounted\AMM")
    assert found is not None and found.name == "34 AMM-1176.pdf"


def test_unreachable_everywhere_returns_none(tmp_path, monkeypatch):
    from aivionics import config
    from aivionics.ui import pdfsource
    monkeypatch.setattr(config, "AMM_DIR", tmp_path / "no-such-corpus")
    assert pdfsource.resolve_chapter_pdf("34", r"Z:\not-mounted\AMM") is None


def test_task_page_lookup_tolerates_injected_whitespace(tmp_path):
    """The real observed form of 34-41-11-020-002 is `34-41-1 1-020-002`."""
    from aivionics.ui import pdfsource
    pdf = tmp_path / "34 AMM.pdf"
    _build_pdf(pdf, [
        "CHAPTER 34 TITLE PAGE",
        "SUBJECT CHAPTER SECTION SUBJECT CONF PAGE EFFECT\n"
        "TASK 34-41-11-020-002   201   TBC ALL",
        "Some other task body text.",
        "TASK 34-41-1 1-020-002\nWEATHER RADAR - REMOVAL\n1. General\nEND OF TASK",
    ])
    hit = pdfsource.find_task_page(pdf, "34-41-11-020-002")
    assert hit is not None
    assert hit.page == 3, "must land on the heading, not the contents entry"
    assert hit.exact is True


def test_contents_entry_is_not_mistaken_for_the_heading(tmp_path):
    from aivionics.ui import pdfsource
    pdf = tmp_path / "31 AMM.pdf"
    _build_pdf(pdf, [
        "SUBJECT CHAPTER SECTION SUBJECT CONF PAGE EFFECT\n"
        "TASK 31-00-00-040-801   901   TBC ALL",
        "TASK 31-00-00-040-801\nMMEL PREPARATION\n1. General\nEND OF TASK",
    ])
    assert pdfsource.find_task_page(pdf, "31-00-00-040-801").page == 1


def test_contents_page_is_used_when_there_is_no_body_page(tmp_path):
    """Better to land on the index than nowhere."""
    from aivionics.ui import pdfsource
    pdf = tmp_path / "31 AMM.pdf"
    _build_pdf(pdf, [
        "SUBJECT CHAPTER SECTION SUBJECT CONF PAGE EFFECT\n"
        "TASK 31-00-00-040-801   901   TBC ALL",
        "An unrelated page.",
    ])
    hit = pdfsource.find_task_page(pdf, "31-00-00-040-801")
    assert hit.page == 0 and hit.exact is False


def test_cross_reference_is_the_last_resort(tmp_path):
    from aivionics.ui import pdfsource
    pdf = tmp_path / "34 AMM.pdf"
    _build_pdf(pdf, ["Cover", "Refer to 34-11-01-400-801 for the removal."])
    hit = pdfsource.find_task_page(pdf, "34-11-01-400-801")
    assert hit.page == 1 and hit.exact is False


def test_absent_task_and_unreadable_file_return_none(tmp_path):
    from aivionics.ui import pdfsource
    pdf = tmp_path / "34 AMM.pdf"
    _build_pdf(pdf, ["TASK 34-11-01-400-801\nEND OF TASK"])
    assert pdfsource.find_task_page(pdf, "99-99-99-999-999") is None
    assert pdfsource.find_task_page(tmp_path / "gone.pdf", "34-11-01-400-801") is None
    not_a_pdf = tmp_path / "broken.pdf"
    not_a_pdf.write_text("not a pdf at all")
    assert pdfsource.find_task_page(not_a_pdf, "34-11-01-400-801") is None
    assert pdfsource.page_count(not_a_pdf) == 0


def test_page_index_caches_lookups(tmp_path):
    from aivionics.ui import pdfsource
    pdf = tmp_path / "34 AMM.pdf"
    _build_pdf(pdf, ["Cover", "TASK 34-11-01-400-801\nEND OF TASK"])
    index = pdfsource.TaskPageIndex()
    first = index.find(pdf, "34-11-01-400-801")
    pdf.unlink()                       # a second real scan would now fail
    assert index.find(pdf, "34-11-01-400-801") == first
    index.clear()
    assert index.find(pdf, "34-11-01-400-801") is None


def test_viewer_offers_no_export_path():
    """Standing rule 1: the viewer reads, it never produces a copy."""
    from pathlib import Path
    source = Path("src/aivionics/ui/pdfview.py").read_text(encoding="utf-8")
    for forbidden in ("QFileDialog", "getSaveFileName", "QPrinter",
                      "QPrintDialog", "doc.save(", "writer.write"):
        assert forbidden not in source, f"{forbidden} must not appear in the viewer"
