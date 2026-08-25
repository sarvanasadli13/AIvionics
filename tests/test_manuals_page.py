"""Regression tests for the Manuals page's browser/empty-state boundary."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from aivionics import db, documents
from aivionics.ui import store
from aivionics.ui.pages.manuals import ManualsPage


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("WINDIR", os.environ.get("SystemRoot", r"C:\Windows"))
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def manuals_page(qt_app, tmp_path):
    path = tmp_path / "manuals-page.db"
    con = db.connect(path)
    documents.migrate(con)
    con.execute(
        "INSERT INTO manual(id,oem,aircraft_type,manual_type,revision,"
        "is_current) VALUES(1,'boeing','737-8','FIM','2017-08-15',1)")
    con.execute(
        "INSERT INTO task(id,manual_id,task_number,title,ata_chapter) "
        "VALUES(1,1,'34-11-00-810-801','Pitot heat fault isolation','34')")
    con.commit()
    corpus = store.CorpusReader(store.open_readonly(path))
    ctx = SimpleNamespace(
        con=con, corpus=corpus, theme_name="dark", window=None)
    page = ManualsPage(ctx)
    yield page
    page.close()
    corpus.close()
    con.close()


def test_valid_manual_selection_recovers_from_the_empty_panel(manuals_page):
    """The screenshot showed live selectors above a stale empty-state panel."""
    page = manuals_page
    assert page.current_manual and page._has_tasks(page.current_manual)

    page.stack.setCurrentWidget(page.empty)  # reproduce the stale panel
    page._on_revision_changed()

    assert page.stack.currentWidget() is not page.empty
    assert page.tree.topLevelItemCount() == 1


def test_task_presence_comes_from_the_read_only_corpus(manuals_page):
    """A UI/write-connection fault must not turn 5,768 FIM tasks into none."""
    page = manuals_page

    class BrokenUiConnection:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("UI connection is unavailable")

    page.ctx.con = BrokenUiConnection()

    assert page._has_tasks(page.current_manual) is True

