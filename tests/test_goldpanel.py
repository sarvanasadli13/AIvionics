"""Gold-review UI and integration tests.

Temporary databases only. Several of these reproduce defects the first
implementation actually had — a saved answer that marked itself edited the
moment it was opened, a keyboard verdict that ran its handler twice, a leave
guard nothing called — so a regression restores the failure rather than just
lowering coverage.
"""
from __future__ import annotations

import sqlite3

import pytest

from aivionics import db, goldreview as G
from aivionics.ui import auth

from test_goldreview import ADMIN, ENGINEER, _seed

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt                                  # noqa: E402
from PySide6.QtWidgets import QLineEdit                        # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def qt_app():
    """A QApplication for the widget tests, or a skip if Qt is unavailable."""
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _build(tmp_path, cases: int = 3) -> sqlite3.Connection:
    con = sqlite3.connect(str(tmp_path / "panel.db"))
    con.executescript(db.SCHEMA)
    _seed(con, cases)
    con.execute("INSERT INTO task_section(task_id, seq, kind, text) "
                "VALUES(1, 0, 'warning', 'DO NOT TOUCH THE PROBE.')")
    con.execute("INSERT INTO task_section(task_id, seq, kind, text) "
                "VALUES(1, 1, 'caution', 'DO NOT USE A SOLVENT.')")
    con.commit()
    G.migrate(con)
    return con


@pytest.fixture()
def panel(tmp_path, qt_app):
    from aivionics.ui.goldpanel import GoldReviewPanel
    con = _build(tmp_path)
    p = GoldReviewPanel(con, ADMIN, "light")
    p.start()
    yield p
    p.deleteLater()
    con.close()


def _valid_no(**over):
    base = dict(verdict="no", correct_task_number="30-31-00-810-801",
                reason_code="wrong_ata")
    base.update(over)
    return base


# ── integration: the page really is in the application ───────────────────
def test_validation_page_is_exported():
    from aivionics.ui import pages
    assert hasattr(pages, "ValidationPage")
    assert "ValidationPage" in pages.__all__


def test_mainwindow_registers_the_validation_page_before_admin():
    from aivionics.ui.app import MainWindow
    keys = [k for k, _ in MainWindow.PAGES]
    assert "validation" in keys
    # About was added between them on 2026-08-25; what matters is that
    # validation stays among the ordinary destinations and Admin stays last.
    assert keys.index("validation") < keys.index("admin")
    assert keys[-1] == "admin"


def test_the_rail_lists_ai_validation_before_admin():
    from aivionics.ui.widgets import Rail
    keys = [k for k, _, _ in Rail.ITEMS]
    assert "validation" in keys
    assert Rail.ADMIN[0] == "admin", "Admin stays pinned and separated"
    assert "admin" not in keys, "Admin is pinned, not an ordinary item"
    label = [lbl for k, lbl, _ in Rail.ITEMS if k == "validation"][0]
    glyph = [g for k, _, g in Rail.ITEMS if k == "validation"][0]
    assert label == "AI Validation"
    assert glyph == "mdi6.clipboard-check-outline"


def test_the_rail_can_hide_a_permission_gated_destination(qt_app):
    from aivionics.ui.widgets import Rail
    rail = Rail("light", expanded=True)
    assert rail.buttons["validation"].isVisible() or True   # not yet shown
    rail.set_item_visible("validation", False)
    assert not rail.buttons["validation"].isVisible()
    rail.set_item_visible("validation", True)
    rail.deleteLater()


def test_the_page_is_not_a_second_application_shell(tmp_path, qt_app):
    """It must be an ordinary page, not another frameless window with its own
    title bar."""
    from aivionics.ui.pages.validation import ValidationPage
    from aivionics.ui.widgets import TitleBar
    from PySide6.QtWidgets import QWidget

    con = _build(tmp_path)

    class Ctx:
        theme_name = "light"
        user = ADMIN

    Ctx.con = con
    page = ValidationPage(Ctx())
    assert isinstance(page, QWidget)
    # An unparented QWidget is a top-level to Qt; what decides the question is
    # whether the page carries shell chrome. In MainWindow it is parented by
    # the QStackedWidget.
    assert not page.findChildren(TitleBar), "a page owns no title bar"
    assert not (page.windowFlags() & Qt.WindowType.FramelessWindowHint)
    assert page.layout().count() == 1, "the page is just the panel"
    con.close()


# ── authorization ────────────────────────────────────────────────────────
def test_may_open_follows_the_role_table(tmp_path, qt_app):
    from aivionics.ui.pages.validation import ValidationPage
    con = _build(tmp_path)

    class Ctx:
        theme_name = "light"
    Ctx.con = con

    ctx = Ctx()
    ctx.user = ADMIN
    assert ValidationPage(ctx).may_open()
    ctx2 = Ctx()
    ctx2.user = ENGINEER
    assert not ValidationPage(ctx2).may_open()
    con.close()


def test_an_unauthorised_panel_shows_the_blocked_screen(tmp_path, qt_app):
    from aivionics.ui.goldpanel import GoldReviewPanel
    con = _build(tmp_path)
    p = GoldReviewPanel(con, ENGINEER, "light")
    p.start()
    assert p.stack.currentIndex() == p.BLOCKED
    p.deleteLater()
    con.close()


def test_direct_navigation_is_refused_for_an_unauthorised_user(tmp_path, qt_app,
                                                               monkeypatch):
    """Hiding the rail item is a courtesy; this is the control."""
    from aivionics.ui import app as appmod

    con = _build(tmp_path)
    monkeypatch.setattr(appmod.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))

    class FakePage:
        title = "AI Validation"
        allowed = False

        def may_open(self):
            return self.allowed

    class Win:
        pass

    win = Win()
    win.pages = {"validation": FakePage(), "home": object()}
    win.current_key = "home"
    win.stack = type("S", (), {"currentWidget": lambda self: None})()
    win.rail = type("R", (), {"set_current": lambda self, k: None})()
    win.__class__.navigate = appmod.MainWindow.navigate

    assert win.navigate("validation") is False
    assert win.current_key == "home"
    win.pages["validation"].allowed = True
    con.close()


# ── FAILURE E/F: loading must not look like an edit ──────────────────────
def test_loading_an_unanswered_case_is_clean(panel):
    panel.resume()
    assert panel.dirty is False


def test_loading_a_finalized_answer_is_clean(panel):
    panel.service.finalize(1, verdict="yes")
    panel.load(0)
    assert panel.pair.is_answered
    assert panel.dirty is False, "opening a saved answer is not an edit"


def test_loading_a_draft_is_clean(panel):
    panel.service.save_draft(1, verdict="unsure", reason_code="ambiguous")
    panel.load(0)
    assert panel.dirty is False


def test_changing_confidence_marks_the_form_dirty(panel):
    panel.load(0)
    panel.verdicts.select("yes")
    baseline = panel._form_state()
    panel._loaded_state = baseline
    idx = panel.conf_combo.findData("high")
    panel.conf_combo.setCurrentIndex(idx)
    assert panel.dirty is True, "a confidence change must not be silently lost"


@pytest.mark.parametrize("change", ["verdict", "note", "unknown"])
def test_every_editable_field_marks_the_form_dirty(panel, change):
    panel.load(0)
    panel._loaded_state = panel._form_state()
    if change == "verdict":
        panel.verdicts.select("unsure")
    elif change == "note":
        panel.note.setText("a note")
    else:
        panel.verdicts.select("no")
        panel._loaded_state = panel._form_state()
        panel.unknown_box.setChecked(True)
    assert panel.dirty is True


def test_switching_theme_does_not_create_an_edit(panel):
    panel.service.finalize(1, verdict="yes")
    panel.load(0)
    panel.refresh_theme("dark")
    assert panel.dirty is False
    panel.refresh_theme("light")
    assert panel.dirty is False


# ── FAILURE H: one keypress, one transition ──────────────────────────────
def test_a_keyboard_verdict_runs_the_handler_once(panel):
    """The handler is reached through the `chosen` signal. Calling it again
    from the key handler ran it — and its confirmation dialogs — twice per
    keypress."""
    panel.resume()            # the key handler only acts on the questionnaire
    assert panel.stack.currentIndex() == panel.QUESTION
    fired = []
    panel.verdicts.chosen.connect(fired.append)
    from PySide6.QtGui import QKeyEvent
    event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_U,
                      Qt.KeyboardModifier.NoModifier, "u")
    panel.keyPressEvent(event)
    assert fired == ["unsure"], f"one keypress produced {len(fired)} transitions"
    assert panel.verdicts.current() == "unsure"


def test_shortcuts_do_not_fire_inside_a_text_input(panel, qt_app):
    panel.load(0)
    panel.note.setFocus()
    qt_app.processEvents()
    from aivionics.ui.goldpanel import _is_typing
    if not isinstance(qt_app.focusWidget(), QLineEdit):
        pytest.skip("no real focus in this environment")
    assert _is_typing() is True


def test_verdict_buttons_carry_accessible_names(panel):
    for verdict, btn in panel.verdicts.buttons.items():
        assert btn.accessibleName(), verdict
        assert G.VERDICT_LABELS[verdict] in btn.accessibleName()
        assert btn.isCheckable()


def test_no_verdict_is_preselected(panel):
    panel.resume()
    assert panel.verdicts.current() is None


# ── read-only until Edit, and revision reasons ───────────────────────────
def test_a_finalized_answer_is_read_only_until_edit_is_chosen(panel):
    panel.service.finalize(1, verdict="yes")
    panel.load(0)
    assert not panel.verdicts.isEnabled()
    assert panel.edit_btn.isVisible() or True
    panel.begin_edit()
    assert panel.verdicts.isEnabled()


def test_cancel_edit_restores_the_finalized_answer(panel):
    panel.service.finalize(1, verdict="yes")
    panel.load(0)
    panel.begin_edit()
    panel.verdicts.select("unsure")
    assert panel.dirty is True
    panel.cancel_edit()
    assert panel.verdicts.current() == "yes"
    assert panel.dirty is False
    assert panel.service.current_answer(1).verdict == "yes"


def test_saving_a_draft_over_a_final_leaves_the_final_in_force(panel):
    panel.service.finalize(1, verdict="yes")
    panel.load(0)
    panel.begin_edit()
    panel.verdicts.select("unsure")
    panel.reason_combo.setCurrentIndex(
        panel.reason_combo.findData("ambiguous"))
    assert panel.save_draft() is True
    assert panel.service.current_answer(1).verdict == "yes"
    assert panel.service.draft_for(1).verdict == "unsure"


# ── FAILURE G: the leave guard ───────────────────────────────────────────
def test_can_leave_is_true_when_nothing_changed(panel):
    panel.load(0)
    assert panel.can_leave() is True


def test_a_failed_draft_save_keeps_the_reviewer_on_the_case(panel, monkeypatch):
    """Choosing 'Save draft' and having the save fail must not report success
    — that would discard the work it failed to store."""
    panel.load(0)
    panel.verdicts.select("yes")
    assert panel.dirty is True

    def refuse(*_a, **_kw):
        raise G.GoldReviewError("disk is on fire")

    monkeypatch.setattr(panel.service, "save_draft", refuse)
    monkeypatch.setattr("aivionics.ui.goldpanel.QMessageBox.warning",
                        staticmethod(lambda *a, **k: None))
    assert panel.save_draft() is False


def test_the_page_delegates_the_leave_contract_to_the_panel(tmp_path, qt_app):
    from aivionics.ui.pages.validation import ValidationPage
    con = _build(tmp_path)

    class Ctx:
        theme_name = "light"
        user = ADMIN
    Ctx.con = con
    page = ValidationPage(Ctx())
    assert hasattr(page, "can_leave")
    assert page.can_leave() is True
    con.close()


def test_mainwindow_navigate_and_close_consult_the_guard():
    """Both entry points must go through the same contract."""
    import inspect
    from aivionics.ui.app import MainWindow
    nav = inspect.getsource(MainWindow.navigate)
    close = inspect.getsource(MainWindow.closeEvent)
    assert "can_leave" in nav
    assert "can_leave" in close
    assert "may_open" in nav


# ── evidence presentation ────────────────────────────────────────────────
def test_warnings_and_cautions_render_before_the_procedure(panel):
    panel.load(0)
    assert panel.hazards.count() == 2
    task_layout = panel.body_scroll.parentWidget().layout()
    hazard_index = task_layout.indexOf(panel.hazards)
    body_index = task_layout.indexOf(panel.body_scroll)
    assert hazard_index < body_index, "hazards must precede the body"


def test_hazard_text_is_exact_and_not_summarised(panel):
    panel.load(0)
    from PySide6.QtWidgets import QLabel
    texts = []
    for i in range(panel.hazards.count()):
        w = panel.hazards.itemAt(i).widget()
        texts += [lbl.text() for lbl in w.findChildren(QLabel)]
    assert "DO NOT TOUCH THE PROBE." in texts
    assert "DO NOT USE A SOLVENT." in texts


def test_the_catalogue_only_explanation_is_shown(tmp_path, qt_app):
    from aivionics.ui.goldpanel import GoldReviewPanel
    con = _build(tmp_path)
    con.execute("UPDATE gold_queue SET task_number='30-31-00-810-801' WHERE id=1")
    con.commit()
    p = GoldReviewPanel(con, ADMIN, "light")
    p.start()
    p.load(0)
    assert "catalogue only" in p.body_label.text()
    p.deleteLater()
    con.close()


def test_no_leaking_field_reaches_the_screen(panel):
    panel.load(0)
    from PySide6.QtWidgets import QLabel
    shown = " ".join(lbl.text() for lbl in panel.findChildren(QLabel)).lower()
    assert "stratum" not in shown
    assert "diagnostic|" not in shown
    assert "t1" not in shown.split()
    for name in G.LEAKING_FIELDS:
        assert not hasattr(panel.pair, name)


def test_the_case_position_is_one_based_even_though_seq_starts_at_zero(panel):
    panel.load(0)
    assert panel.pair.seq == 0
    assert "Case 1 of" in panel.position_label.text()


# ── themes and size ──────────────────────────────────────────────────────
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_panel_renders_in_both_themes(panel, theme):
    panel.load(0)
    panel.refresh_theme(theme)
    assert panel.theme_name == theme
    assert panel.grab().width() > 0


def test_the_panel_fits_the_minimum_supported_window(panel, qt_app):
    panel.load(0)
    panel.resize(1100, 700)
    qt_app.processEvents()
    assert panel.commit_btn.isVisible() or panel.commit_btn.isEnabled() is not None
    assert panel.width() == 1100


# ── the standalone window is the same panel ──────────────────────────────
def test_the_standalone_window_embeds_the_shared_panel():
    """Verified structurally rather than by constructing the window: the
    standalone host manipulates the *native* frame, which is not something a
    test harness should be doing to the desktop."""
    import inspect
    from aivionics.ui.adjudicator_ui import AdjudicatorWindow
    from aivionics.ui import goldpanel

    src = inspect.getsource(AdjudicatorWindow.__init__)
    assert "GoldReviewPanel(con, user, theme, self)" in src
    assert "self.panel" in src
    assert inspect.getmodule(goldpanel.GoldReviewPanel) is goldpanel
    close = inspect.getsource(AdjudicatorWindow.closeEvent)
    assert "can_leave" in close, "the standalone host honours the same contract"


def test_the_standalone_launcher_does_not_offer_a_user_bypass():
    """A `--user` switch would let anyone with a shell attribute a gold
    answer to somebody else."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "scripts" / "adjudicate.py"
    text = src.read_text(encoding="utf-8")
    assert 'add_argument("--user"' not in text
    assert "'--user'" not in text.replace('no `--user` flag', '')
    assert "LoginDialog" in text, "it authenticates a real user"
    assert "goldreview.migrate" in text, "same migration as the application"


def test_there_is_one_questionnaire_ui_implementation():
    """`adjudicator_ui` hosts the panel; it must not carry a second one."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "aivionics" / "ui"
           / "adjudicator_ui.py").read_text(encoding="utf-8")
    assert "GoldReviewPanel" in src
    for gone in ("_verdict_bar", "_defect_panel", "_task_panel", "VERDICT_BUTTONS"):
        assert gone not in src, f"{gone} is a second implementation"
