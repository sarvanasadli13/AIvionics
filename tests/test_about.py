"""The About page: identity, safety posture, and honest system information.

The owner supplied the creator block and the AI-systems list verbatim and
constrained how they may be presented. Several tests below assert what must
*not* appear — that the AI systems carry no roles or categories — because a
well-meaning later edit adding "(architecture)" beside a model name would be
a claim the owner has not made.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

# The QApplication is created before `aivionics.ui.*` is imported. Those
# modules pull in qtawesome and Qt's SVG machinery, which initialise against
# a live application; importing them first and constructing the application
# afterwards killed the interpreter with no traceback.
from PySide6.QtWidgets import QApplication      # noqa: E402

_APP = QApplication.instance() or QApplication([])

from aivionics.ui.pages import about as A      # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    yield _APP


class _Ctx:
    """A context with nothing in it — the page must cope."""
    theme_name = "light"
    con = None
    corpus = None
    user = None
    online_enabled = None
    window = None


@pytest.fixture()
def page(qt_app):
    p = A.AboutPage(_Ctx())
    p.on_shown()
    yield p
    p.deleteLater()


def _all_text(widget) -> str:
    from PySide6.QtWidgets import QLabel, QPushButton
    parts = [w.text() for w in widget.findChildren(QLabel)]
    parts += [w.text() for w in widget.findChildren(QPushButton)]
    return "\n".join(parts)


# ── entry points ─────────────────────────────────────────────────────────
def test_the_about_page_is_exported_and_registered():
    from aivionics.ui import pages
    from aivionics.ui.app import MainWindow

    assert "AboutPage" in pages.__all__
    keys = [k for k, _ in MainWindow.PAGES]
    assert "about" in keys


def test_the_rail_offers_about():
    from aivionics.ui.widgets import Rail
    assert "about" in [k for k, _, _ in Rail.ITEMS]


def test_the_login_screen_offers_about():
    import inspect
    from aivionics.ui.login import LoginDialog

    source = inspect.getsource(LoginDialog)
    assert "About AIvionics" in source
    assert hasattr(LoginDialog, "show_about")


def test_the_login_dialog_builds_the_same_page_not_a_copy():
    """A second copy of the text would drift out of step with the page."""
    import inspect
    from aivionics.ui.login import LoginDialog
    assert "AboutPage" in inspect.getsource(LoginDialog.show_about)


# ── creator, exactly as supplied ─────────────────────────────────────────
def test_the_creator_name_and_title_are_displayed_verbatim(page):
    text = _all_text(page)
    assert "Sarvan Asadli" in text
    for line in ("M.Sc. Electrical Engineering & Information Technology |",
                 "Avionics Engineer (B.Sc. Honours) |",
                 "AI & LLM Systems |",
                 "RDMA, GPU-to-GPU & FPGA Research"):
        assert line in text, f"missing exactly: {line!r}"
    assert ("AIvionics was conceived, created and directed by Sarvan Asadli."
            in text)


# ── AI systems: named, never characterised ───────────────────────────────
def test_all_six_ai_systems_are_listed(page):
    text = _all_text(page)
    for name in ("Kimi K3", "Claude Fable 5", "Claude Opus 5",
                 "DeepSeek V4 Pro", "OpenAI Codex", "NVIDIA Nemotron"):
        assert name in text, f"missing {name}"
    assert len(A.AI_SYSTEMS) == 6


def test_no_ai_system_is_given_a_role_or_a_category(page):
    """Names only. Saying which system designed, reviewed or verified any part
    of the product would be an attribution the owner has not made."""
    for entry in A.AI_SYSTEMS:
        assert "—" not in entry and "-" not in entry.replace("GPU-to-GPU", "")
        assert "(" not in entry and ":" not in entry
    text = _all_text(page).lower()
    banned = ("architecture:", "implementation:", "verification:", "reasoning:",
              "designed by", "coded by", "reviewed by", "tested by",
              "responsible for")
    for phrase in banned:
        assert phrase not in text, f"AI systems must carry no roles: {phrase!r}"


def test_the_independence_statement_is_present(page):
    assert ("does not imply sponsorship, certification or endorsement"
            in _all_text(page))


# ── required statements ──────────────────────────────────────────────────
def test_the_universal_aircraft_statement_is_present(page):
    text = _all_text(page)
    assert "not limited to one aircraft type" in text
    assert "knowledge packages" in text
    assert "737" not in text, "the product must not be described as 737-only"


def test_the_safety_statement_is_present_and_complete(page):
    text = _all_text(page)
    assert "engineering decision-support system" in text
    assert "does not replace approved aircraft maintenance documentation" in text
    assert "independently verified before maintenance action" in text
    assert "advisory recommendations, not confirmed diagnoses" in text
    assert "responsibility of authorized personnel" in text


def test_the_connectivity_section_is_present(page):
    text = _all_text(page)
    assert "can work locally" in text
    assert "does not change the authority" in text


# ── dynamic values ───────────────────────────────────────────────────────
def test_the_version_comes_from_the_single_application_source(page):
    from aivionics.admin import maintenance
    assert maintenance.app_version() in _all_text(page)


def test_the_schema_version_is_the_real_one(page):
    from aivionics.admin import maintenance
    rows = dict(page.system_information())
    assert rows["Database schema version"] == maintenance.SCHEMA_VERSION


def test_missing_information_is_reported_not_invented(page):
    """With no database and no corpus, counts must say so."""
    rows = dict(page.system_information())
    for key in ("Installed manuals", "Indexed manual tasks",
                "Aircraft knowledge packages"):
        assert rows[key] == A.UNAVAILABLE, f"{key} was invented: {rows[key]!r}"


def test_manual_statistics_are_read_from_the_real_repository(qt_app, tmp_path):
    """With a corpus present, the counts are its counts — not a constant."""
    import sqlite3
    from aivionics import db
    from aivionics.ui import store

    path = tmp_path / "a.db"
    con = sqlite3.connect(str(path))
    con.executescript(db.SCHEMA)
    con.execute("INSERT INTO manual(id,oem,aircraft_type,manual_type,revision,"
                "is_current) VALUES(1,'boeing','737-8','AMM','R1',1)")
    con.execute("INSERT INTO manual(id,oem,aircraft_type,manual_type,revision,"
                "is_current) VALUES(2,'airbus','A320','TM','R1',0)")
    con.execute("INSERT INTO task(id,manual_id,task_number,title,ata_chapter) "
                "VALUES(1,1,'34-11-00-810-801','T','34')")
    con.commit()

    class Ctx(_Ctx):
        pass
    Ctx.con = con
    Ctx.corpus = store.CorpusReader(con)

    page = A.AboutPage(Ctx())
    rows = dict(page.system_information())
    assert rows["Installed manuals"] == "2"
    assert rows["Indexed manual tasks"] == "1"
    assert rows["Aircraft knowledge packages"] == "2"
    page.deleteLater()
    con.close()


def test_the_copyright_year_is_the_current_one(page):
    from datetime import date
    assert str(date.today().year) in _all_text(page)


# ── the diagnostic summary must be safe to paste ─────────────────────────
def test_the_diagnostic_summary_carries_no_secrets_or_paths(page):
    summary = page.diagnostic_summary()
    assert "AIvionics" in summary
    lowered = summary.lower()
    for banned in ("password", "api_key", "api key", "token", "secret",
                   "bearer", "nvapi-"):
        assert banned not in lowered, f"summary leaked {banned!r}"
    # No filesystem paths: a support paste should not describe someone's disk.
    assert "C:\\" not in summary and "/home/" not in summary
    assert ".db" not in summary


def test_the_diagnostic_summary_matches_what_is_displayed(page):
    summary = page.diagnostic_summary()
    for key, value in page.system_information():
        assert key in summary and str(value) in summary


# ── licences come from the registry, not a second list ───────────────────
def test_licences_are_read_from_the_shared_registry(page):
    import inspect
    assert "LICENSES.md" in inspect.getsource(A.AboutPage.licence_text)
    text = page.licence_text()
    assert isinstance(text, str) and text.strip()


def test_the_licence_panel_toggles(page):
    assert not page.licence_body.isVisible()
    page.toggle_licences()
    assert page.licence_body.text().strip()


# ── offline and resilience ───────────────────────────────────────────────
def test_the_page_needs_no_network(page, monkeypatch):
    """Opening About must not reach the network for anything."""
    import socket

    def refuse(*_a, **_kw):
        raise AssertionError("About must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    page.on_shown()
    page.toggle_licences()
    assert page.diagnostic_summary()
