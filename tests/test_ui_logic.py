"""UI logic tests.

Everything the shell relies on that can be got wrong silently: the theme
token contract, password handling, the locator print block, the adjudication
queue, and the audit chain across a simulated session.

Almost all of it runs without Qt. The exception is the block at the bottom,
which grabs the login dialog and counts pixels — see the comment there for
why nothing cheaper would have caught the bug it guards.
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


def _claim_setup(connection, password="a-much-better-secret"):
    user = auth.unclaimed_setup_user(connection)
    assert user is not None and user.must_change_pw
    return auth.change_password(connection, user, password)


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


def test_setup_account_has_no_public_bootstrap_password(con):
    user = auth.unclaimed_setup_user(con)
    assert user is not None and user.role == "admin"
    assert not auth.authenticate(con, auth.SETUP_USERNAME,
                                "aivionics-setup").ok


def test_wrong_password_is_rejected_without_revealing_which_part(con):
    bad_pw = auth.authenticate(con, auth.SETUP_USERNAME, "not-the-password")
    unknown = auth.authenticate(con, "nobody", "not-the-password")
    assert not bad_pw.ok and not unknown.ok
    assert bad_pw.reason == unknown.reason


def test_must_change_flow_clears_the_flag(con):
    updated = _claim_setup(con)
    assert updated.must_change_pw is False

    again = auth.authenticate(con, auth.SETUP_USERNAME, "a-much-better-secret")
    assert again.ok and again.user.must_change_pw is False
    assert not auth.authenticate(con, auth.SETUP_USERNAME,
                                 "aivionics-setup").ok


def test_weak_or_unchanged_passwords_are_refused(con):
    user = auth.unclaimed_setup_user(con)
    with pytest.raises(ValueError):
        auth.change_password(con, user, "short")
    with pytest.raises(ValueError):
        auth.change_password(con, user, "aivionics-setup")


def test_repeated_failures_are_throttled(con):
    _claim_setup(con)
    for _ in range(auth.LOCKOUT_AFTER):
        auth.authenticate(con, auth.SETUP_USERNAME, "wrong")
    result = auth.authenticate(con, auth.SETUP_USERNAME,
                               "a-much-better-secret")
    assert not result.ok and "wait" in result.reason.lower()
    auth.reset_throttle(auth.SETUP_USERNAME)
    assert auth.authenticate(con, auth.SETUP_USERNAME,
                             "a-much-better-secret").ok


def test_signature_is_initial_plus_surname():
    user = auth.User(1, "s.asadli", "Sarvan Asadli", "engineer", False)
    assert auth.signature(user) == "S. Asadli"
    assert auth.signature(None) == "unknown"


def test_page_navigation_is_not_an_audited_action():
    """Standing rule 6 / BetrVG §87(1)(6): no per-user screen tracking."""
    assert not any("nav" in action for action in auth.AUDITED_ACTIONS)


# ── audit chain ─────────────────────────────────────────────────────────

def test_chain_intact_across_a_simulated_session(con):
    user = _claim_setup(con)
    auth.authenticate(con, auth.SETUP_USERNAME, "a-much-better-secret")
    auth.authenticate(con, auth.SETUP_USERNAME, "wrong")
    printing.record_print(con, user_id=user.id, task_id=None,
                          manual_revision="48", aircraft_id=None,
                          task_number="34-11-01-400-801")
    auth.logout(con, user)

    ok, rows = audit.verify_chain(con)
    assert ok
    assert rows >= 5
    actions = [r[0] for r in con.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions[0] == "password_change"
    assert actions[-1] == "logout"
    assert "print" in actions and "login_failed" in actions


def test_tampering_with_a_row_breaks_the_chain(con):
    _claim_setup(con)
    auth.authenticate(con, auth.SETUP_USERNAME, "a-much-better-secret")
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
    user = _claim_setup(con)
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


def _mark_done(con, seq: int, verdict: str) -> None:
    """Mark a queue row answered without going through the retired writer."""
    row = con.execute("SELECT defect_id, task_number FROM gold_queue "
                      "WHERE seq=?", (seq,)).fetchone()
    con.execute("INSERT INTO label_gold(defect_id, task_number, verdict, "
                "adjudicated_at) VALUES(?,?,?, '2026-01-01T00:00:00Z')",
                (row[0], row[1], verdict))
    con.execute("UPDATE gold_queue SET done=1 WHERE seq=?", (seq,))
    con.commit()


def test_the_legacy_write_path_is_closed(con):
    """`AdjudicationQueue.commit` used to be a second, unauthenticated writer
    of `label_gold` — no reviewer, no history, no audit entry. Gold answers
    now go through `goldreview.GoldReviewService` only; see
    tests/test_goldreview.py for the replacement's behaviour."""
    from aivionics.ui.adjudicator import GoldReviewWriteRemoved

    _seed_queue(con)
    queue = AdjudicationQueue(con)
    with pytest.raises(GoldReviewWriteRemoved):
        queue.commit(1, "yes")
    assert con.execute("SELECT COUNT(*) FROM label_gold").fetchone()[0] == 0
    assert con.execute(
        "SELECT done FROM gold_queue WHERE seq=1").fetchone()[0] == 0


def test_unsure_remains_a_first_class_verdict(con):
    """PLAN §5: forcing a guess into three buckets is what ruins a gold set."""
    _seed_queue(con)
    assert set(VERDICT_KEYS.values()) == {"yes", "no", "partial", "unsure"}


def test_navigation_and_resume(con):
    _seed_queue(con, n=5)
    queue = AdjudicationQueue(con)
    assert queue.resume_seq() == 1

    # setup only: the reader is what these tests exercise, and the legacy
    # write path is closed (see test_the_legacy_write_path_is_closed)
    _mark_done(con, 1, "yes")
    _mark_done(con, 2, "no")
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
    _mark_done(con, 1, "yes")
    progress = queue.progress()
    assert (progress.done, progress.total) == (1, 5)
    assert progress.pct == pytest.approx(20.0)
    assert dict((name, total) for name, _done, total in progress.per_stratum) == {
        "ata34-diagnostic": 3, "ata34-action": 2}


def test_resume_holds_at_the_end_when_everything_is_done(con):
    _seed_queue(con, n=2)
    queue = AdjudicationQueue(con)
    # setup only: the reader is what these tests exercise, and the legacy
    # write path is closed (see test_the_legacy_write_path_is_closed)
    _mark_done(con, 1, "yes")
    _mark_done(con, 2, "no")
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


def test_online_features_default_to_on(con):
    """Owner decision 2026-08-21 (BACKLOG R6). This reverses the original
    posture, so it is asserted rather than assumed: a fresh database starts
    permitted, and the switch still turns it off completely."""
    assert store.online_enabled(con) is True
    store.set_setting(con, "online_enabled", "0")
    assert store.online_enabled(con) is False
    store.set_setting(con, "online_enabled", "1")
    assert store.online_enabled(con) is True


def test_settings_tolerate_a_database_without_the_table(tmp_path):
    path = tmp_path / "bare.db"
    bare = sqlite3.connect(path)
    assert store.get_setting(bare, "online_enabled") == "1"
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


def test_the_viewer_cannot_export_a_file():
    """PLAN standing rule 1 said the viewer never produces a copy. The owner
    reversed that on 2026-08-25 to permit *printing* a page range.

    Printing to paper and exporting a file are different risks: a printed
    sheet carries the provenance stamped onto it, whereas an exported PDF is
    an unmarked duplicate of controlled data that can be mailed onward. The
    export path therefore stays closed.
    """
    from pathlib import Path
    source = Path("src/aivionics/ui/pdfview.py").read_text(encoding="utf-8")
    for forbidden in ("QFileDialog", "getSaveFileName", "doc.save(",
                      "writer.write"):
        assert forbidden not in source, f"{forbidden} must not appear in the viewer"


def test_every_printed_page_carries_its_provenance():
    """A loose sheet can outlive the revision it came from, so each one is
    stamped UNCONTROLLED COPY and names its source page, manual and time."""
    from pathlib import Path
    source = Path("src/aivionics/ui/pdfview.py").read_text(encoding="utf-8")
    assert "UNCONTROLLED COPY" in source
    assert "source page" in source
    assert "Verify against the controlled revision" in source
    assert "printed " in source


# ── login dialog: rendering ─────────────────────────────────────────────
# The only tests in this file that need Qt widgets, and they earn it. The
# minimise and close buttons were laid out at the right coordinates and
# reported isVisible() == True while painting absolutely nothing: the global
# stylesheet's `QPushButton { padding: 7px 15px }` cascades into their own
# stylesheet, and on a 30 px-wide button that leaves a content rectangle 0 px
# across, so the label was laid out into nothing. No geometry assertion can
# see that. Only pixels can.

WINDOW_BUTTONS = ("Minimise", "Close")


@pytest.fixture(scope="module")
def qt_app():
    """A QApplication for the whole module, or a skip if there is no display."""
    import os

    if os.environ.get("QT_QPA_PLATFORM") in {"offscreen", "minimal"}:
        pytest.skip("needs the native platform: offscreen reports no fonts")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    try:
        app = widgets.QApplication.instance() or widgets.QApplication([])
    except Exception as exc:                        # pragma: no cover
        pytest.skip(f"no Qt platform available: {exc}")
    return app


def _shown_login(qt_app, connection, theme):
    """Lay the dialog out without mapping it to the screen."""
    from PySide6.QtCore import Qt

    from aivionics.ui import fonts
    from aivionics.ui.login import LoginDialog

    qt_app.setStyleSheet(fonts.qss(theme))
    dialog = LoginDialog(connection, theme)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.show()
    for _ in range(10):
        qt_app.processEvents()
    return dialog


def test_virgin_database_opens_secure_first_run_setup(qt_app, tmp_path):
    connection = db.connect(tmp_path / "first-run-admin.db")
    auth.seed(connection)
    dialog = _shown_login(qt_app, connection, "light")
    try:
        assert dialog.user == auth.unclaimed_setup_user(connection)
        assert dialog.stack.currentIndex() == 1
        assert "Create administrator" in dialog.windowTitle()
        assert not auth.authenticate(connection, "admin",
                                    "aivionics-setup").ok
    finally:
        dialog.close()
        connection.close()


def _window_button(dialog, name):
    from PySide6.QtWidgets import QPushButton

    for button in dialog.findChildren(QPushButton):
        if button.accessibleName() == name:
            return button
    raise AssertionError(f"the login dialog has no {name!r} button")


def _ink_in(dialog, button) -> int:
    """Pixels inside `button` that differ from the surface it sits on.

    The grab is in *device* pixels and widget geometry is in logical ones, so
    the rectangle has to be scaled. Probing the unscaled coordinates samples a
    different part of the card entirely, and will report a blank region for a
    button that is painting perfectly well.
    """
    shot = dialog.grab()
    ratio = shot.devicePixelRatio() or 1.0
    image = shot.toImage()
    corner = button.mapTo(dialog, button.rect().topLeft())
    seen: dict[str, int] = {}
    for y in range(round(corner.y() * ratio),
                   round((corner.y() + button.height()) * ratio)):
        for x in range(round(corner.x() * ratio),
                       round((corner.x() + button.width()) * ratio)):
            key = image.pixelColor(x, y).name()
            seen[key] = seen.get(key, 0) + 1
    background = max(seen, key=lambda k: seen[k])
    return sum(seen.values()) - seen[background]


@pytest.mark.parametrize("theme", sorted(T.THEMES))
def test_window_buttons_are_actually_painted(qt_app, tmp_path, theme):
    connection = db.connect(tmp_path / f"render-{theme}.db")
    auth.seed(connection)
    dialog = _shown_login(qt_app, connection, theme)
    try:
        for name in WINDOW_BUTTONS:
            button = _window_button(dialog, name)
            assert button.isVisible()
            ink = _ink_in(dialog, button)
            assert ink > 20, (
                f"the {name} button drew {ink} pixels that differ from the card "
                f"in the {theme} theme — it is laid out but invisible")
    finally:
        dialog.close()
        connection.close()


def test_close_rejects_the_dialog_so_the_app_can_exit(qt_app, tmp_path):
    from PySide6.QtWidgets import QDialog

    connection = db.connect(tmp_path / "close.db")
    auth.seed(connection)
    dialog = _shown_login(qt_app, connection, "light")
    try:
        _window_button(dialog, "Close").click()
        for _ in range(5):
            qt_app.processEvents()
        assert dialog.result() == QDialog.DialogCode.Rejected
        assert not dialog.isVisible()
    finally:
        dialog.close()
        connection.close()


def test_minimise_really_minimises(qt_app, tmp_path):
    """It only can because the dialog carries Qt.WindowType.Window; without
    that flag a frameless dialog has no taskbar entry to minimise into."""
    connection = db.connect(tmp_path / "minimise.db")
    auth.seed(connection)
    dialog = _shown_login(qt_app, connection, "light")
    try:
        assert not dialog.isMinimized()
        _window_button(dialog, "Minimise").click()
        for _ in range(5):
            qt_app.processEvents()
        assert dialog.isMinimized()
    finally:
        dialog.close()
        connection.close()


def test_the_dialog_can_still_be_dragged_by_its_background(qt_app, tmp_path):
    """Frameless windows move only because the dialog moves itself."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    connection = db.connect(tmp_path / "drag.db")
    auth.seed(connection)
    dialog = _shown_login(qt_app, connection, "light")
    try:
        dialog.move(200, 200)
        for _ in range(3):
            qt_app.processEvents()
        origin = dialog.pos()
        dialog.mousePressEvent(QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, QPointF(120, 300),
            QPointF(320, 500), Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        dialog.mouseMoveEvent(QMouseEvent(
            QMouseEvent.Type.MouseMove, QPointF(150, 340), QPointF(350, 540),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        for _ in range(3):
            qt_app.processEvents()
        assert dialog.pos() - origin == QPoint(30, 40)
    finally:
        dialog.close()
        connection.close()


def test_the_lockup_is_drawn_unstretched_and_tight(qt_app):
    """The asset's own viewBox decides the height, and it has no dead margin.

    Two regressions this covers. `svg_pixmap` allocates a square, so a 198x64
    lockup drawn through it comes out stretched to one; and the wordmark used
    to be live <text> whose `x` sat on a <tspan>, which QSvgRenderer ignores —
    the name then printed on top of the tile, inside a box a third of which
    was empty, so the lockup could not be centred.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer

    from aivionics import config
    from aivionics.ui.widgets import svg_pixmap_wide

    for theme in sorted(T.THEMES):
        path = config.ASSETS_DIR / "icons" / f"logo-{theme}.svg"
        assert path.exists(), path
        assert "<text" not in path.read_text(encoding="utf-8"), (
            f"{path.name} still carries live text; it re-flows on a machine "
            f"without Segoe UI Variable Display or Georgia")

        box = QSvgRenderer(str(path)).viewBoxF()
        pixmap = svg_pixmap_wide(path, 210)
        assert pixmap.width() == 210
        assert pixmap.height() == round(210 * box.height() / box.width())

        scale = 4
        canvas = QPixmap(int(box.width()) * scale, int(box.height()) * scale)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        QSvgRenderer(str(path)).render(painter)
        painter.end()
        image = canvas.toImage()
        rightmost = max(
            (x for x in range(image.width())
             if any(image.pixelColor(x, y).alpha() > 12
                    for y in range(image.height()))),
            default=-1)
        margin = (image.width() - rightmost) / scale
        assert 0 < margin <= 6, (
            f"{path.name} leaves {margin:.1f} units empty on the right; the "
            f"lockup cannot sit centred")


# ── the rail: section names, collapsing, and a dimmed item you can see ──
# BACKLOG items 3 and 4. The rail's two modes and the native window styles
# are both things that look fine in code review and are wrong on screen, so
# they are asserted against the real widget rather than against intent.

def _rail(qt_app, expanded: bool):
    from PySide6.QtCore import Qt

    from aivionics.ui import fonts
    from aivionics.ui.widgets import Rail

    qt_app.setStyleSheet(fonts.qss("light"))
    rail = Rail("light", expanded=expanded)
    rail.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    rail.resize(rail.width(), 700)
    rail.show()
    for _ in range(4):
        qt_app.processEvents()
    return rail


def test_expanded_rail_carries_every_section_name(qt_app):
    rail = _rail(qt_app, expanded=True)
    expected = {label for _key, label, _glyph in rail.ITEMS}
    expected.add(rail.ADMIN[1])
    assert {b.text() for b in rail.buttons.values()} == expected
    assert rail.width() == rail.WIDTH_EXPANDED
    rail.close()


def test_collapsed_rail_drops_the_names_but_never_the_labelling(qt_app):
    """Icon-only is a visual mode, not a loss of information: the tooltip and
    the accessible name are what a screen reader and a hover both read."""
    rail = _rail(qt_app, expanded=False)
    assert all(b.text() == "" for b in rail.buttons.values())
    assert rail.width() == rail.WIDTH
    for key, label, _glyph in rail.ITEMS:
        assert rail.buttons[key].accessibleName() == label
    assert rail.buttons["home"].toolTip() == "Home"
    rail.close()


def test_the_collapse_control_reports_the_new_state(qt_app):
    rail = _rail(qt_app, expanded=True)
    seen: list[bool] = []
    rail.expanded_changed.connect(seen.append)

    rail.set_expanded(False, animate=False)
    assert seen == [False] and rail.width() == rail.WIDTH
    rail.set_expanded(True, animate=False)
    assert seen == [False, True] and rail.width() == rail.WIDTH_EXPANDED

    rail.set_expanded(True, animate=False)
    assert seen == [False, True], "setting the mode it is already in is not a change"
    rail.close()


def test_the_rail_mode_is_remembered(con):
    """It is a preference, not a session state — see MainWindow.remember_rail_state."""
    assert store.get_setting(con, "rail_expanded") == "1"
    store.set_setting(con, "rail_expanded", "0")
    assert store.get_setting(con, "rail_expanded") == "0"


def _icon_contrast(image, rect) -> float:
    """How far the strongest pixel of an item's *icon* gets from its ground.

    Deliberately not the whole button: with the rail expanded the section name
    sits in the same rectangle at full contrast, and a scan that includes it
    reports a healthy number no matter what the icon is doing. The band is the
    left edge of the item, where the 19 px glyph is drawn.
    """
    def lum(colour):
        return 0.299 * colour.red() + 0.587 * colour.green() + 0.114 * colour.blue()

    # Widget geometry is in logical pixels and the grab is in device ones. On
    # a 125% display that is a 25% drift, which quietly moves the scan onto a
    # different item — the reading stays plausible and stops meaning anything.
    ratio = image.devicePixelRatio() or 1.0

    def at(x: float, y: float):
        return image.pixelColor(int(x * ratio), int(y * ratio))

    ground = lum(at(rect.left() + 2, rect.top() + 2))
    return max(abs(lum(at(x, y)) - ground)
               for x in range(rect.left() + 6, rect.left() + 36)
               for y in range(rect.top() + 6, rect.bottom() - 6))


def test_the_dimmed_ops_item_is_dimmed_and_not_erased(qt_app):
    """Ops greys out when the online switch is off (PLAN §4A.1 rule 4).

    It used to be painted in $hair, which on this rail's own gradient is not
    grey — it is gone. Quieter than its neighbours, still legible, is the
    whole of the requirement, and only pixels can tell the two apart.
    """
    rail = _rail(qt_app, expanded=True)
    rail.set_online_enabled(False)
    for _ in range(4):
        qt_app.processEvents()
    image = rail.grab().toImage()

    dimmed = _icon_contrast(image, rail.buttons["ops"].geometry())
    normal = _icon_contrast(image, rail.buttons["fleet"].geometry())
    assert dimmed > 25, f"the dimmed Ops item is invisible (contrast {dimmed:.0f})"
    assert dimmed < normal, "dimmed has to be quieter than a normal item"
    rail.close()


# ── the frameless window keeps the desktop's own behaviour ──────────────

def test_native_frame_styles_are_restored(qt_app):
    """BACKLOG item 3. Without WS_CAPTION and WS_MINIMIZEBOX on the native
    handle, Windows does not animate a minimise — the window just vanishes.
    Qt strips both when the frame is turned off, so they are put back."""
    import sys

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    from aivionics.ui import nativewindow

    window = QWidget()
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    try:
        restored = nativewindow.restore_native_frame(window)
        if sys.platform != "win32":
            assert restored is False, "no claim is made off Windows"
            return
        assert restored is True
        import ctypes
        style = ctypes.windll.user32.GetWindowLongW(
            int(window.winId()), nativewindow.GWL_STYLE)
        for name in ("WS_CAPTION", "WS_THICKFRAME", "WS_MINIMIZEBOX",
                     "WS_MAXIMIZEBOX", "WS_SYSMENU"):
            assert style & getattr(nativewindow, name), f"{name} is missing"
    finally:
        window.close()


def test_a_message_that_is_not_ours_is_left_to_qt(qt_app):
    from PySide6.QtWidgets import QWidget

    from aivionics.ui import nativewindow
    assert nativewindow.handle_native_event(QWidget(), b"xcb_generic_event", 0) is None


# ── Home says what to do when there is nothing to show ──────────────────

def test_home_leads_with_the_first_run_notice_when_the_corpus_is_empty(qt_app, tmp_path):
    """BACKLOG item 1. Three em-dashes and a row of NO DATA badges report the
    state accurately and give the operator nothing to act on."""
    from aivionics.ui import fonts
    from aivionics.ui.app import build_context
    from aivionics.ui.pages.home import HomePage

    qt_app.setStyleSheet(fonts.qss("light"))
    ctx = build_context(tmp_path / "empty.db")
    ctx.user = auth.User(1, "s.asadli", "Sarvan Asadli", "engineer", False)
    page = HomePage(ctx)

    assert not page.first_run.isHidden()
    assert str(tmp_path) in page.first_run.where.text(), (
        "the notice has to name the database it actually opened")

    ctx.corpus.counts = lambda: {"tasks": 2426, "amm": 2426, "fim": 0,
                                 "cases": 0, "aircraft": 0}
    page.on_shown()
    assert page.first_run.isHidden(), "it is a first-run notice, not a banner"
    page.close()


# ── the online badge reports a live fact, not a stored permission ───────
# BACKLOG item 5, owner's decision 2026-08-21: the badge has to mean what
# everybody reads it to mean. ONLINE needs both halves — permission from
# Admin and an actual route off the machine — and neither one alone.

def _shell(qt_app, tmp_path):
    from PySide6.QtCore import Qt

    from aivionics.ui import fonts
    from aivionics.ui.app import MainWindow, build_context

    ctx = build_context(tmp_path / "shell.db")
    qt_app.setStyleSheet(fonts.qss(ctx.theme_name))
    ctx.user = auth.User(1, "s.asadli", "Sarvan Asadli", "engineer", False)
    window = MainWindow(ctx)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    for _ in range(4):
        qt_app.processEvents()
    return window


def test_online_needs_both_permission_and_a_route(qt_app, tmp_path):
    window = _shell(qt_app, tmp_path)
    try:
        seen = {}
        for permitted in (False, True):
            for reachable in (False, True):
                window.ctx.online_enabled = permitted
                window.reachability.is_reachable = lambda r=reachable: r
                window.refresh_online_badge()
                seen[(permitted, reachable)] = window.titlebar.badge.text()

        assert seen[(True, True)] == "ONLINE"
        assert seen[(True, False)] == "OFFLINE", "a route is required"
        assert seen[(False, True)] == "OFFLINE", (
            "the switch is off, so this machine is making no connection — "
            "badging ONLINE would announce one that does not exist")
        assert seen[(False, False)] == "OFFLINE"
    finally:
        window.close()


def test_the_two_reasons_for_offline_are_distinguishable(qt_app, tmp_path):
    """Same word, different cause. The tooltip is where the difference lives —
    'switched off in Admin' is a decision, 'no route' is a fault to chase."""
    window = _shell(qt_app, tmp_path)
    try:
        window.ctx.online_enabled = False
        window.reachability.is_reachable = lambda: True
        window.refresh_online_badge()
        switched_off = window.titlebar.badge.toolTip()

        window.ctx.online_enabled = True
        window.reachability.is_reachable = lambda: False
        window.refresh_online_badge()
        unreachable = window.titlebar.badge.toolTip()

        assert "Admin" in switched_off
        assert "no route" in unreachable
        assert switched_off != unreachable
    finally:
        window.close()


def test_the_badge_follows_the_cable(qt_app, tmp_path):
    """Pull it out, push it back in — with nothing else called in between."""
    window = _shell(qt_app, tmp_path)
    try:
        window.ctx.online_enabled = True
        window.reachability.is_reachable = lambda: True
        window.refresh_online_badge()
        assert window.titlebar.badge.text() == "ONLINE"

        window.reachability.is_reachable = lambda: False
        window.reachability.changed.emit(False)
        assert window.titlebar.badge.text() == "OFFLINE"

        window.reachability.is_reachable = lambda: True
        window.reachability.changed.emit(True)
        assert window.titlebar.badge.text() == "ONLINE"
    finally:
        window.close()


def test_online_is_the_accent_and_never_a_status_colour(qt_app, tmp_path):
    """Green for ONLINE and red for OFFLINE would say a disconnected machine
    is faulty. This one is designed to run with the cable out — that reading
    is what got the badge questioned. The accent carries no status meaning."""
    window = _shell(qt_app, tmp_path)
    try:
        window.ctx.online_enabled = True
        window.reachability.is_reachable = lambda: True
        window.refresh_online_badge()
        assert window.titlebar.badge.property("live") is True

        window.reachability.is_reachable = lambda: False
        window.refresh_online_badge()
        assert window.titlebar.badge.property("live") is False
    finally:
        window.close()
    assert T.accent_is_status_free()


def test_reachability_maps_a_local_only_network_to_offline(qt_app):
    """`Local` and `Site` mean a network was found and the internet was not —
    which is precisely the case this badge exists to get right."""
    from PySide6.QtNetwork import QNetworkInformation

    from aivionics.ui.connectivity import Reachability

    watcher = Reachability()
    if not watcher.supported:
        pytest.skip("no QNetworkInformation backend on this platform")

    states = QNetworkInformation.Reachability

    class _Stub:
        def __init__(self, value):
            self.value = value

        def reachability(self):
            return self.value

    for state, expected in ((states.Online, True), (states.Unknown, True),
                            (states.Local, False), (states.Site, False),
                            (states.Disconnected, False)):
        watcher._info = _Stub(state)
        assert watcher.is_reachable() is expected, state


def test_an_unavailable_backend_is_not_reported_as_a_disconnection(qt_app):
    """An unknown state is not evidence of a fault. Badging OFFLINE on a guess
    is the failure this whole change replaces."""
    from aivionics.ui.connectivity import Reachability

    watcher = Reachability()
    watcher._info = None
    assert watcher.supported is False
    assert watcher.is_reachable() is True
    assert watcher.backend_name() == "none"


# ── the title bar describes the corpus, not one manual out of it ─────────

def _manual(aircraft, kind, revision="R1", current=1):
    return dict(aircraft_type=aircraft, manual_type=kind, revision=revision,
                revision_date="2018-02-27", is_current=current)


def test_the_titlebar_names_every_current_manual_not_just_the_first():
    """It used to render `next(m for m in manuals if m["is_current"])`.

    With a 737-8 AMM and a 737-8 FIM both current, AMM sorted first and the
    FIM was simply absent from the interface — 5,768 of 8,194 task locators,
    70% of the shipped corpus, unnamed anywhere in the chrome.
    """
    from aivionics.ui.app import corpus_context

    text = corpus_context([_manual("737-8", "AMM", "2018-02-27"),
                           _manual("737-8", "FIM", "2017-08-15")])
    assert "AMM" in text and "FIM" in text
    assert "2 current manuals" in text


def test_the_titlebar_does_not_name_one_aircraft_for_a_multi_type_corpus():
    """A single aircraft type in the application chrome makes a multi-type
    tool look like a single-type one."""
    from aivionics.ui.app import corpus_context

    two = corpus_context([_manual("737-8", "AMM"), _manual("A320", "AMM")])
    assert "737-8" in two and "A320" in two

    many = corpus_context([_manual("737-8", "AMM"), _manual("A320", "AMM"),
                           _manual("E175", "FIM"), _manual("CRJ900", "AMM")])
    assert "4 aircraft types" in many
    assert "737-8" not in many, "no single type may stand for the whole corpus"


def test_the_titlebar_still_names_a_single_manual_precisely():
    from aivionics.ui.app import corpus_context

    text = corpus_context([_manual("737-8", "AMM", "2018-02-27")])
    assert "737-8" in text and "AMM Rev 2018-02-27" in text


def test_the_titlebar_reports_an_empty_or_superseded_corpus_honestly():
    from aivionics.ui.app import corpus_context

    assert corpus_context([]) == "No manual corpus loaded"
    assert corpus_context([_manual("737-8", "AMM", current=0)]) == \
        "No manual corpus loaded"
