"""Import a CAMO compliance export (PLAN 4B.2).

The register is a **mirror**, never the legal record, and the whole safety
argument for showing alerts at all rests on every row knowing where it came
from and when. So this importer will not write a row without a source system
and an import timestamp, and it refuses the whole file rather than importing
part of it — a half-imported register looks complete and is not.

CSV only, deliberately. An .xlsx path would pull in a spreadsheet engine to
read what every CAMO can already export, and would let formatting decide what
a date means.
"""
from __future__ import annotations

import csv
import io
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import compliance

# Header aliases seen across AMOS / TRAX / Ramco style exports. Matching is
# case- and punctuation-insensitive, so "Due Date", "due_date" and "DUE-DATE"
# are the same column.
ALIASES: dict[str, tuple[str, ...]] = {
    "tail": ("tail", "registration", "reg", "aircraft", "acreg",
             "aircraftregistration", "nnumber"),
    "kind": ("kind", "type", "itemtype", "category", "recordtype"),
    "ref": ("ref", "reference", "taskref", "adref", "sbref", "melref",
            "documentref", "number", "taskno"),
    "description": ("description", "title", "subject", "text", "details",
                    "defectdescription"),
    "mel_category": ("melcategory", "category", "melcat", "cat"),
    "due_date": ("duedate", "duedt", "nextdue", "nextduedate", "limitdate",
                 "expiry", "expirydate"),
    "due_hours": ("duehours", "nextduehours", "limithours", "hours", "fh",
                  "flighthours"),
    "due_cycles": ("duecycles", "nextduecycles", "limitcycles", "cycles", "fc"),
    "raised_at": ("raisedat", "raised", "deferredon", "reporteddate",
                  "opened", "opendate", "datedeferred"),
    "status": ("status", "state", "itemstatus"),
}
REQUIRED = ("tail", "kind")

DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%d.%m.%Y",
                "%Y/%m/%d", "%d-%m-%Y", "%Y%m%d")

KIND_SYNONYMS = {
    "checkup": "checkup", "check": "checkup", "maintenance": "checkup",
    "inspection": "checkup", "task": "checkup", "scheduled": "checkup",
    "mel": "mel", "ddl": "mel", "deferred": "mel", "deferreddefect": "mel",
    "cdl": "mel",
    "adsb": "adsb", "ad": "adsb", "sb": "adsb", "airworthinessdirective": "adsb",
    "servicebulletin": "adsb", "ad/sb": "adsb",
}


def _norm(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def map_header(header: list[str]) -> tuple[dict[str, int], list[str]]:
    """Column index per field, plus the required fields that are missing."""
    seen = {_norm(h): i for i, h in enumerate(header)}
    mapping: dict[str, int] = {}
    for field_name, names in ALIASES.items():
        for candidate in names:
            if candidate in seen:
                mapping[field_name] = seen[candidate]
                break
    missing = [f for f in REQUIRED if f not in mapping]
    return mapping, missing


def parse_date(value: str | None) -> date | None:
    """Parse an export's date cell, or None when it cannot be read.

    The whole string is tried before any truncation: `01-Sep-2026` is eleven
    characters, so clipping to ten silently made the day-month-name format
    unmatchable. Truncation is still needed for cells carrying a time
    ("2026-09-01T00:00:00"), so it is a fallback rather than the first move.
    """
    text = (value or "").strip()
    if not text:
        return None
    for candidate in (text, text[:11], text[:10], text[:8]):
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def parse_kind(value: str | None) -> str | None:
    return KIND_SYNONYMS.get(_norm(value or ""))


def _number(value: str | None, cast):
    text = (value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return cast(float(text))
    except ValueError:
        return None


@dataclass
class ImportReport:
    """What a run would do, or did. `ok` is the gate on committing."""

    source_system: str
    batch_id: str = ""
    total_rows: int = 0
    accepted: int = 0
    rows: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    committed: bool = False
    source_file: str = ""
    imported_by: str | None = None

    @property
    def rejected(self) -> int:
        return self.total_rows - self.accepted

    @property
    def ok(self) -> bool:
        return not self.missing_columns and not self.errors and self.accepted > 0

    def summary(self) -> str:
        if self.missing_columns:
            return ("cannot import — the export is missing required "
                    f"column(s): {', '.join(self.missing_columns)}")
        if self.errors:
            head = "; ".join(self.errors[:3])
            more = f" (+{len(self.errors) - 3} more)" if len(self.errors) > 3 else ""
            return f"{len(self.errors)} row(s) rejected — {head}{more}"
        return (f"{self.accepted} of {self.total_rows} rows ready from "
                f"{self.source_system}")


def read(text: str, *, source_system: str) -> ImportReport:
    """Parse and validate without touching the database (the dry run)."""
    if not str(source_system or "").strip():
        raise compliance.MissingProvenance(
            "an import needs a source system — a row whose origin is unknown "
            "cannot carry the provenance line standing rule 2 requires")

    report = ImportReport(source_system=source_system.strip())
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        report.errors.append("the file is empty")
        return report

    mapping, missing = map_header(header)
    if missing:
        report.missing_columns = missing
        return report

    def cell(row: list[str], name: str) -> str | None:
        idx = mapping.get(name)
        return row[idx].strip() if idx is not None and idx < len(row) else None

    for line_no, row in enumerate(reader, start=2):
        if not any((c or "").strip() for c in row):
            continue
        report.total_rows += 1
        tail = (cell(row, "tail") or "").upper()
        kind = parse_kind(cell(row, "kind"))
        if not tail:
            report.errors.append(f"line {line_no}: no tail")
            continue
        if not kind:
            report.errors.append(
                f"line {line_no}: unrecognised kind {cell(row, 'kind')!r} "
                f"(expected a checkup, MEL or AD/SB)")
            continue

        raised = parse_date(cell(row, "raised_at"))
        due = parse_date(cell(row, "due_date"))
        category = (cell(row, "mel_category") or "").strip().upper() or None
        if category not in (None, *compliance.MEL_CATEGORIES):
            category = None
        # A category B/C/D MEL carries a fixed rectification interval, so its
        # calendar limit is derivable when the export omitted it.
        if kind == "mel" and due is None:
            due = compliance.mel_due_date(raised, category)

        report.rows.append({
            "tail": tail,
            "kind": kind,
            "ref": cell(row, "ref") or "",
            "description": cell(row, "description") or "",
            "mel_category": category,
            "due_date": due.isoformat() if due else None,
            "due_hours": _number(cell(row, "due_hours"), float),
            "due_cycles": _number(cell(row, "due_cycles"), int),
            "raised_at": raised.isoformat() if raised else None,
            "status": (cell(row, "status") or "open").lower(),
        })
        report.accepted += 1

    if report.total_rows and not report.accepted and not report.errors:
        report.errors.append("no usable rows")
    return report


def commit(con: sqlite3.Connection, report: ImportReport, *,
           replace_source: bool = True) -> ImportReport:
    """Write a validated report. Refuses anything that failed the dry run.

    All rows from this source are replaced rather than merged: a CAMO export is
    a snapshot, and merging would leave an item that has since been closed
    sitting on the register forever with nothing to clear it.
    """
    if not report.ok:
        raise ValueError(f"refusing to import: {report.summary()}")

    compliance.ensure_schema(con)
    now = datetime.now(timezone.utc).isoformat()
    batch = uuid.uuid4().hex[:12]

    if replace_source:
        con.execute("DELETE FROM compliance_item WHERE source_system=?",
                    (report.source_system,))
    con.executemany(
        "INSERT INTO compliance_item(aircraft_tail,kind,ref,description,"
        "mel_category,due_date,due_hours,due_cycles,raised_at,status,"
        "source_system,imported_at,batch_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r["tail"], r["kind"], r["ref"], r["description"], r["mel_category"],
          r["due_date"], r["due_hours"], r["due_cycles"], r["raised_at"],
          r["status"], report.source_system, now, batch)
         for r in report.rows])
    con.execute(
        "INSERT INTO import_batch(batch_id,source_system,source_file,"
        "rows_total,rows_imported,rows_rejected,imported_at,imported_by)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (batch, report.source_system, report.source_file, report.total_rows,
         report.accepted, report.rejected, now, report.imported_by))
    # No write to source_freshness: that table holds the configured window
    # only. Staleness is derived from MAX(compliance_item.imported_at), so the
    # rows are their own timestamp and the two can never disagree.
    con.commit()
    report.batch_id = batch
    report.committed = True
    return report


def import_file(con: sqlite3.Connection, path: Path | str, *,
                source_system: str, dry_run: bool = False,
                imported_by: str | None = None) -> ImportReport:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    report = read(text, source_system=source_system)
    report.source_file = str(path)
    report.imported_by = imported_by
    if dry_run or not report.ok:
        return report
    return commit(con, report)
