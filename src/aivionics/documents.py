"""Adding and removing manual documents. No Qt in this module.

Two kinds of document live in the `manual` table and they must never be
confused:

* **Maintenance data** — AMM, FIM, TSM. Organised by ATA *task number*, with
  effectivity, warnings, cautions and numbered steps. This is what Diagnose
  retrieves and what a printed locator cites.
* **Training material** — Part-147 type-training manuals, CBT, study notes.
  Organised by topic. It explains how a system works; it does not tell you
  what to do to one, and it carries no task numbers.

Training material is genuinely useful to an engineer, so it is allowed in.
What is not allowed is letting it pass as maintenance data — so the
distinction is a **column the code can test**, `manual.doc_class`, rather
than a convention in a free-text field that a typo could defeat. Everything
downstream keys off `is_maintenance()`, never off the manual's name.

A training document therefore:
  * never produces `task` rows, so it can never be retrieved as a locator,
  * never gets cited on a printed locator block,
  * is badged wherever it appears.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ATA task locator: 27-93-34-810-801
TASK_NUMBER = re.compile(r"\b\d{2}-\d{2}-\d{2}-\d{3}-\d{3}\b")

MAINTENANCE = "maintenance"
TRAINING = "training"
REFERENCE = "reference"
DOC_CLASSES = (MAINTENANCE, TRAINING, REFERENCE)

# What each class is called on screen. The word "training" appears in the
# label itself, not only in a colour or an icon.
CLASS_LABELS = {
    MAINTENANCE: "Maintenance data",
    TRAINING: "Training material — not maintenance data",
    REFERENCE: "Reference document — not maintenance data",
}

# Manual types that are maintenance data by definition.
MAINTENANCE_TYPES = {"AMM", "FIM", "TSM", "IPC", "WDM", "SRM", "CMM", "AWM"}
TRAINING_TYPES = {"TM", "CBT", "TRAINING", "NOTES"}

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".doc"}

# How many pages to read before deciding what a document is. A real AMM
# chapter carries 80+ task numbers in its first 25 pages; a training manual
# carries none anywhere.
CLASSIFY_PAGES = 25
TASK_NUMBERS_EXPECTED = 5


class DocumentError(RuntimeError):
    """The document cannot be added, and the reason is worth reading."""


@dataclass
class Inspection:
    """What a file turned out to be, before anything is written."""

    path: Path
    readable: bool = False
    pages: int = 0
    task_numbers: int = 0
    doc_class: str = TRAINING
    manual_type: str = "TM"
    aircraft_type: str = ""
    title: str = ""
    reason: str = ""
    sample: list[str] = field(default_factory=list)

    @property
    def is_maintenance(self) -> bool:
        return self.doc_class == MAINTENANCE


def is_maintenance(row) -> bool:
    """The one predicate the rest of the application should use.

    Defaults to True for rows written before `doc_class` existed: both
    manuals present at that point were an AMM and a FIM, and treating an
    unknown legacy row as training would silently drop real maintenance data
    out of retrieval.
    """
    try:
        value = row["doc_class"]
    except (KeyError, IndexError, TypeError):
        return True
    return (value or MAINTENANCE) == MAINTENANCE


# ── schema ───────────────────────────────────────────────────────────────
def migrate(con: sqlite3.Connection) -> None:
    """Add `doc_class` to `manual`. Additive and idempotent."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(manual)")}
    if not cols:
        return
    if "doc_class" not in cols:
        con.execute(f"ALTER TABLE manual ADD COLUMN doc_class TEXT NOT NULL "
                    f"DEFAULT '{MAINTENANCE}'")
        # Everything already ingested came through the maintenance parsers.
        con.execute(f"UPDATE manual SET doc_class='{MAINTENANCE}' "
                    f"WHERE doc_class IS NULL OR doc_class=''")
    if "display_title" not in cols:
        con.execute("ALTER TABLE manual ADD COLUMN display_title TEXT")
    con.commit()


# ── inspection ───────────────────────────────────────────────────────────
def _extract(path: Path, pages: int) -> tuple[bool, int, str]:
    """Return (readable, page_count, text). Never raises on a bad file."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            import fitz
        except ImportError as exc:                              # pragma: no cover
            raise DocumentError(f"PDF support unavailable: {exc}") from exc
        try:
            doc = fitz.open(str(path))
        except Exception:                                       # noqa: BLE001
            return False, 0, ""
        try:
            n = len(doc)
            if n == 0:
                return False, 0, ""
            text = "".join(doc[i].get_text() for i in range(min(n, pages)))
            return True, n, text
        finally:
            doc.close()
    if suffix in (".docx", ".doc"):
        try:
            import docx
        except ImportError:
            return False, 0, ""
        try:
            document = docx.Document(str(path))
        except Exception:                                       # noqa: BLE001
            return False, 0, ""
        paragraphs = [p.text for p in document.paragraphs]
        return True, len(paragraphs), "\n".join(paragraphs[:2000])
    return False, 0, ""


def _guess_aircraft(text: str, path: Path) -> str:
    """Pull an aircraft type out of the document or its path, or say nothing.

    A wrong guess here mislabels a manual, so an uncertain answer is left
    empty for the operator to fill rather than invented.
    """
    hay = f"{path.parent.name} {path.stem} {text[:4000]}".upper()
    for pattern, label in (
            (r"\bA3(18|19|20|21)\b", "A320 family"), (r"\bA330\b", "A330"),
            (r"\bA340\b", "A340"), (r"\bA350\b", "A350"),
            (r"\bA380\b", "A380"), (r"737[- ]?(8|MAX)", "737-8"),
            (r"\b737\b", "737"), (r"\b747\b", "747"), (r"\b767\b", "767"),
            (r"\b777\b", "777"), (r"\b787\b", "787"),
            (r"\bE1?(70|75|90|95)\b", "E-Jet"), (r"\bATR[- ]?72\b", "ATR 72"),
            (r"\bATR[- ]?42\b", "ATR 42"), (r"\bCRJ\b", "CRJ"),
            (r"\bCONCORDE\b", "Concorde")):
        if re.search(pattern, hay):
            return label
    return ""


def inspect(path: str | Path) -> Inspection:
    """Decide what a file is, by reading it. Writes nothing.

    The test is structural, not the filename: a document called "AMM" with no
    task numbers is not maintenance data, and one called "notes" that is full
    of them probably is.
    """
    path = Path(path)
    out = Inspection(path=path)
    if not path.exists():
        out.reason = "That file does not exist."
        return out
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        out.reason = (f"{path.suffix or 'This file type'} is not supported. "
                      f"Add a PDF, DOCX or DOC.")
        return out

    readable, pages, text = _extract(path, CLASSIFY_PAGES)
    out.readable, out.pages = readable, pages
    if not readable:
        out.reason = ("This file could not be read. Encrypted or "
                      "DRM-protected documents cannot be opened for text "
                      "extraction, and a scanned document with no text layer "
                      "has nothing to extract.")
        return out

    found = TASK_NUMBER.findall(text)
    out.task_numbers = len(set(found))
    out.sample = sorted(set(found))[:5]
    out.aircraft_type = _guess_aircraft(text, path)
    out.title = path.stem.strip()

    upper = text[:6000].upper()
    if out.task_numbers >= TASK_NUMBERS_EXPECTED:
        out.doc_class = MAINTENANCE
        out.manual_type = ("FIM" if "FAULT ISOLATION" in upper
                           else "TSM" if "TROUBLE SHOOTING" in upper
                           else "AMM")
        out.reason = (f"{out.task_numbers} ATA task numbers found in the "
                      f"first {min(pages, CLASSIFY_PAGES)} pages — this reads "
                      f"as maintenance data.")
    else:
        out.doc_class = TRAINING
        out.manual_type = "TM"
        marker = ("FOR TRAINING PURPOSES ONLY" in upper
                  or "TRAINING MANUAL" in upper)
        out.reason = (
            "No ATA task numbers found"
            + (" and the document is marked for training purposes"
               if marker else "")
            + ". It will be added as training material: readable in Manuals, "
              "but never cited as a maintenance task.")
    return out


# ── writing ──────────────────────────────────────────────────────────────
def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def add_document(con: sqlite3.Connection, path: str | Path, *,
                 aircraft_type: str = "", manual_type: str = "",
                 doc_class: str = "", revision: str = "",
                 display_title: str = "", oem: str = "") -> int:
    """Register one document. Returns the new `manual.id`.

    Only ever writes a `manual` row. Task extraction for maintenance data is
    the ingest pipeline's job, and training material has no tasks to extract
    — so this cannot accidentally create retrievable content.
    """
    migrate(con)
    path = Path(path)
    found = inspect(path)
    if not found.readable:
        raise DocumentError(found.reason)

    doc_class = (doc_class or found.doc_class).strip().lower()
    if doc_class not in DOC_CLASSES:
        raise DocumentError(f"doc_class must be one of {', '.join(DOC_CLASSES)}")
    manual_type = (manual_type or found.manual_type).strip().upper()
    if doc_class == MAINTENANCE and manual_type not in MAINTENANCE_TYPES:
        raise DocumentError(
            f"{manual_type} is not a maintenance manual type "
            f"({', '.join(sorted(MAINTENANCE_TYPES))})")

    # `uq_manual_current` is UNIQUE(aircraft_type, manual_type, is_current):
    # the schema assumes one *manual* per type, which is right for an AMM —
    # a single controlled document set — and wrong for training material,
    # where each course module is its own document. Individually added
    # documents are therefore not flagged current; "current" is a property of
    # a controlled revision, and a training PDF has no revision service.
    is_current = 1 if doc_class == MAINTENANCE else 0

    digest = _sha256(path)
    existing = con.execute("SELECT id FROM manual WHERE source_hash=?",
                           (digest,)).fetchone()
    if existing:
        raise DocumentError(
            "This exact document is already in the corpus "
            f"(manual #{existing[0]}). Remove it first to replace it.")

    cur = con.execute(
        "INSERT INTO manual(oem, aircraft_type, manual_type, doc_standard,"
        " parser_plugin, revision, revision_date, is_current, source_file,"
        " source_hash, ingested_at, doc_class, display_title)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oem or "", aircraft_type or found.aircraft_type or "unspecified",
         manual_type, "document", "document",
         revision or (display_title or found.title or path.stem)[:60],
         None, is_current, str(path), digest,
         datetime.now(timezone.utc).isoformat(), doc_class,
         display_title or found.title))
    con.commit()
    return int(cur.lastrowid)


def remove_document(con: sqlite3.Connection, manual_id: int, *,
                    force: bool = False) -> dict:
    """Remove a manual and anything indexed from it.

    Refuses by default when the manual has extracted tasks: deleting those
    silently would break every locator and gold-queue row that cites them.
    `force=True` removes them, and reports exactly what went.
    """
    migrate(con)
    row = con.execute(
        "SELECT id, aircraft_type, manual_type, doc_class, display_title,"
        " source_file FROM manual WHERE id=?", (manual_id,)).fetchone()
    if row is None:
        raise DocumentError(f"There is no manual #{manual_id}.")

    tasks = con.execute("SELECT COUNT(*) FROM task WHERE manual_id=?",
                        (manual_id,)).fetchone()[0]
    cited = con.execute(
        "SELECT COUNT(*) FROM gold_queue q JOIN task t"
        "  ON UPPER(TRIM(t.task_number))=UPPER(TRIM(q.task_number))"
        " WHERE t.manual_id=?", (manual_id,)).fetchone()[0] if tasks else 0

    if tasks and not force:
        raise DocumentError(
            f"This manual has {tasks:,} extracted tasks"
            + (f", and {cited} of them are cited by the gold-set queue"
               if cited else "")
            + ". Removing it would break every locator that cites them. "
              "Confirm explicitly to remove it and its tasks.")

    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.execute("BEGIN IMMEDIATE")
        if tasks:
            con.execute(
                "DELETE FROM task_section WHERE task_id IN "
                "(SELECT id FROM task WHERE manual_id=?)", (manual_id,))
            con.execute("DELETE FROM task WHERE manual_id=?", (manual_id,))
        con.execute("DELETE FROM manual WHERE id=?", (manual_id,))
    except Exception:
        con.rollback()
        raise
    con.commit()
    return {"manual_id": manual_id, "tasks_removed": tasks,
            "gold_queue_citations_affected": cited,
            "title": row["display_title"] if "display_title" in row.keys()
            else row["manual_type"]}


def documents(con: sqlite3.Connection) -> list[dict]:
    """Every registered document, maintenance and training alike."""
    migrate(con)
    rows = con.execute(
        "SELECT m.id, m.oem, m.aircraft_type, m.manual_type, m.revision,"
        "       m.is_current, m.source_file, m.doc_class, m.display_title,"
        "       (SELECT COUNT(*) FROM task t WHERE t.manual_id=m.id) AS tasks"
        " FROM manual m ORDER BY m.doc_class, m.aircraft_type, m.manual_type"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["doc_class"] = d.get("doc_class") or MAINTENANCE
        d["class_label"] = CLASS_LABELS.get(d["doc_class"], d["doc_class"])
        d["is_maintenance"] = d["doc_class"] == MAINTENANCE
        out.append(d)
    return out
