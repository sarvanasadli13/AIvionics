"""Adding and removing manual documents, and keeping the two kinds apart.

Temporary databases only. The safety property under test throughout is that
training material can be *present and useful* without ever being mistaken for
maintenance data — by the interface or by the code.
"""
from __future__ import annotations

import sqlite3

import pytest

from aivionics import db, documents as D


# ── fixtures ─────────────────────────────────────────────────────────────
@pytest.fixture()
def con(tmp_path):
    c = sqlite3.connect(str(tmp_path / "docs.db"))
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    c.execute("INSERT INTO manual(id,oem,aircraft_type,manual_type,revision,"
              "is_current) VALUES(1,'boeing','737-8','AMM','2018-02-27',1)")
    c.execute("INSERT INTO task(id,manual_id,task_number,title,ata_chapter) "
              "VALUES(1,1,'34-11-00-810-801','Pitot heat','34')")
    c.commit()
    D.migrate(c)
    yield c
    c.close()


def _pdf(path, text: str, pages: int = 2):
    """A tiny real PDF carrying `text`, so classification is exercised for
    real rather than against a stub."""
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


AMM_TEXT = ("TASK 27-93-34-810-801 Removal\n"
            "TASK 27-93-34-810-802 Operational Test\n"
            "TASK 27-51-00-820-801 Adjustment\n"
            "TASK 32-11-00-810-801 Landing Gear\n"
            "TASK 29-11-00-810-801 Hydraulic Power\n"
            "TASK 36-11-00-810-801 Pneumatic\n")

TRAINING_TEXT = ("Airbus A318/A319/A320/A321 ATA 27 CFM56 cat. B1.1\n"
                 "FLIGHT CONTROLS  TRAINING MANUAL\n"
                 "ISSUE 1, 03 Jan 2011  FOR TRAINING PURPOSES ONLY\n")


# ── migration ────────────────────────────────────────────────────────────
def test_migration_is_idempotent_and_defaults_existing_rows_to_maintenance(con):
    D.migrate(con)
    D.migrate(con)
    row = con.execute("SELECT doc_class FROM manual WHERE id=1").fetchone()
    assert row["doc_class"] == D.MAINTENANCE, \
        "an AMM ingested before this column existed is maintenance data"


def test_is_maintenance_defaults_true_for_a_row_without_the_column():
    """Treating an unknown legacy row as training would silently drop real
    maintenance data out of retrieval."""
    assert D.is_maintenance({"manual_type": "AMM"}) is True
    assert D.is_maintenance({"doc_class": D.MAINTENANCE}) is True
    assert D.is_maintenance({"doc_class": D.TRAINING}) is False


# ── classification ───────────────────────────────────────────────────────
def test_a_document_full_of_task_numbers_reads_as_maintenance_data(tmp_path):
    found = D.inspect(_pdf(tmp_path / "amm.pdf", AMM_TEXT))
    assert found.readable and found.is_maintenance
    assert found.manual_type == "AMM"
    assert found.task_numbers >= D.TASK_NUMBERS_EXPECTED


def test_a_training_manual_reads_as_training_however_it_is_named(tmp_path):
    """The test is structural. A file called AMM with no tasks is not an AMM."""
    found = D.inspect(_pdf(tmp_path / "AMM.pdf", TRAINING_TEXT))
    assert found.readable
    assert found.doc_class == D.TRAINING
    assert found.manual_type == "TM"
    assert "training purposes" in found.reason.lower()


def test_an_unreadable_document_says_why_and_is_refused(tmp_path, con):
    bad = tmp_path / "drm.pdf"
    bad.write_bytes(b"%PDF-1.7\nnot really a pdf\n")
    found = D.inspect(bad)
    assert not found.readable
    assert "DRM" in found.reason or "could not be read" in found.reason
    with pytest.raises(D.DocumentError):
        D.add_document(con, bad)


def test_an_unsupported_file_type_is_refused_by_name(tmp_path, con):
    odd = tmp_path / "notes.txt"
    odd.write_text("hello")
    assert "not supported" in D.inspect(odd).reason
    with pytest.raises(D.DocumentError):
        D.add_document(con, odd)


def test_a_missing_file_is_reported_plainly(tmp_path):
    assert "does not exist" in D.inspect(tmp_path / "nope.pdf").reason


# ── adding ───────────────────────────────────────────────────────────────
def test_adding_a_training_document_stores_its_class(tmp_path, con):
    mid = D.add_document(con, _pdf(tmp_path / "t.pdf", TRAINING_TEXT),
                         aircraft_type="A320 family")
    row = con.execute("SELECT * FROM manual WHERE id=?", (mid,)).fetchone()
    assert row["doc_class"] == D.TRAINING
    assert row["manual_type"] == "TM"
    assert row["aircraft_type"] == "A320 family"
    assert not D.is_maintenance(row)


def test_a_training_document_creates_no_tasks(tmp_path, con):
    """It can never be retrieved as a locator, because there is nothing to
    retrieve — not because a filter hides it."""
    before = con.execute("SELECT COUNT(*) FROM task").fetchone()[0]
    mid = D.add_document(con, _pdf(tmp_path / "t.pdf", TRAINING_TEXT))
    assert con.execute("SELECT COUNT(*) FROM task").fetchone()[0] == before
    assert con.execute("SELECT COUNT(*) FROM task WHERE manual_id=?",
                       (mid,)).fetchone()[0] == 0


def test_the_same_document_cannot_be_added_twice(tmp_path, con):
    path = _pdf(tmp_path / "t.pdf", TRAINING_TEXT)
    D.add_document(con, path)
    with pytest.raises(D.DocumentError, match="already in the corpus"):
        D.add_document(con, path)


def test_a_document_cannot_be_forced_to_be_maintenance_data_by_type(tmp_path, con):
    with pytest.raises(D.DocumentError, match="not a maintenance manual type"):
        D.add_document(con, _pdf(tmp_path / "t.pdf", TRAINING_TEXT),
                       doc_class=D.MAINTENANCE, manual_type="TM")


def test_documents_lists_both_kinds_with_a_readable_label(tmp_path, con):
    D.add_document(con, _pdf(tmp_path / "t.pdf", TRAINING_TEXT))
    rows = {d["doc_class"]: d for d in D.documents(con)}
    assert rows[D.MAINTENANCE]["is_maintenance"] is True
    assert rows[D.TRAINING]["is_maintenance"] is False
    assert "not maintenance data" in rows[D.TRAINING]["class_label"]


# ── removing ─────────────────────────────────────────────────────────────
def test_removing_a_training_document_is_straightforward(tmp_path, con):
    mid = D.add_document(con, _pdf(tmp_path / "t.pdf", TRAINING_TEXT))
    out = D.remove_document(con, mid)
    assert out["tasks_removed"] == 0
    assert con.execute("SELECT COUNT(*) FROM manual WHERE id=?",
                       (mid,)).fetchone()[0] == 0


def test_removing_a_manual_with_tasks_is_refused_until_confirmed(con):
    with pytest.raises(D.DocumentError, match="Confirm explicitly"):
        D.remove_document(con, 1)
    assert con.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 1, \
        "the refusal must not have deleted anything"


def test_a_confirmed_removal_reports_exactly_what_went(con):
    out = D.remove_document(con, 1, force=True)
    assert out["tasks_removed"] == 1
    assert con.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM manual WHERE id=1").fetchone()[0] == 0


def test_removal_warns_when_the_gold_queue_cites_the_tasks(con):
    con.execute("INSERT INTO defect(id,defect_text) VALUES(1,'X')")
    con.execute("INSERT INTO gold_queue(id,defect_id,task_number,stratum,seq,done)"
                " VALUES(1,1,'34-11-00-810-801','34|action|T1',0,0)")
    con.commit()
    with pytest.raises(D.DocumentError, match="gold-set queue"):
        D.remove_document(con, 1)


def test_removing_an_unknown_manual_says_so(con):
    with pytest.raises(D.DocumentError, match="no manual #999"):
        D.remove_document(con, 999)


# ── the safety property ──────────────────────────────────────────────────
def test_a_locator_refuses_to_cite_training_material():
    """A locator sends an engineer to a task in a controlled manual. Training
    material is neither controlled nor task-numbered, so it may never appear
    on one — enforced in the printer, not only hidden in the UI."""
    from aivionics.ui import printing

    task = {"id": 1, "task_number": "34-11-00-810-801", "title": "Pitot heat"}
    training = {"id": 3, "manual_type": "TM", "revision": "—",
                "doc_class": D.TRAINING}
    with pytest.raises(ValueError, match="only cite maintenance data"):
        printing.print_locator(None, task, training)


# ── the native-library ordering that keeps the viewer from eating the UI ──
def test_pymupdf_is_loaded_before_qt_initialises():
    """PySide6 and PyMuPDF link their own copies of the same native
    dependencies. Loading MuPDF *after* Qt has initialised corrupts shared
    state, and the symptom is not an error — live QWidgets are destroyed
    underneath their Python wrappers.

    Reproduced 2026-08-25: opening a document in the Manuals viewer deleted
    the ATA tree, so switching aircraft afterwards raised
    `Internal C++ object (QTreeWidget) already deleted` and the manual list
    came back empty. `aivionics.ui.app` therefore imports fitz at module
    scope, before any caller can construct a QApplication.
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src" / "aivionics" /
              "ui" / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    fitz_line = qt_line = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            module = getattr(node, "module", None) or ""
            if "fitz" in names and fitz_line is None:
                fitz_line = node.lineno
            if module.startswith("PySide6") and qt_line is None:
                qt_line = node.lineno

    assert fitz_line is not None, "app.py must import fitz at module scope"
    assert qt_line is not None
    assert fitz_line < qt_line, (
        f"fitz is imported at line {fitz_line}, after PySide6 at line "
        f"{qt_line} — MuPDF must initialise first or it corrupts Qt")


def test_the_manuals_viewer_does_not_destroy_the_browser(tmp_path, qt_app_docs):
    """The regression test for the failure above, exercised end to end."""
    import shiboken6
    from aivionics.ui.pdfview import PdfViewer

    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "doc.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "TRAINING MANUAL")
    doc.save(str(pdf))
    doc.close()

    from PySide6.QtWidgets import QTreeWidget
    tree = QTreeWidget()
    viewer = PdfViewer("light")
    assert shiboken6.isValid(tree)
    viewer.open(pdf, "test", None)
    assert shiboken6.isValid(tree), \
        "opening a document must not destroy unrelated widgets"
    viewer.deleteLater()
    tree.deleteLater()


@pytest.fixture(scope="module")
def qt_app_docs():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])
