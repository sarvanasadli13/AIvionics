"""Phase 0.2 — normalise the 32 SDR year files into sdr_raw.

Column names are header-introspected per file against alias lists; a file
whose header lacks a required field is reported loudly, never skipped
silently. Provenance columns: source_year, ingested_at.
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import config

csv.field_size_limit(10_000_000)

ALIASES: dict[str, list[str]] = {
    "control_no": ["OperatorControlNumber", "ControlNumber", "OperatorDesignator",
                   "ReportNumber", "SDRNumber", "C5FEControlNumber"],
    "difficulty_date": ["DifficultyDate", "DateOfDifficulty", "EventDate",
                        "DateAdded", "ReceivedDate", "ReportDate"],
    "tail": ["RegistryNNumber", "AircraftRegistrationNumber",
             "RegistrationNumber", "NNumber", "RegistryNumber"],
    "aircraft_make": ["AircraftMake", "Make"],
    "aircraft_model": ["AircraftModel", "Model"],
    "jasc_code": ["JASCCode", "JASC_Code", "JASC"],
    "part_name": ["PartName"],
    "part_number": ["PartNumber"],
    "part_condition": ["PartCondition"],
    "stage": ["StageOfOperation", "Stage"],
    "discrepancy": ["Discrepancy", "DiscrepancyText", "Remarks"],
}
REQUIRED = {"difficulty_date", "jasc_code", "discrepancy"}


def _map_header(header: list[str]) -> tuple[dict[str, int], set[str]]:
    lower = {h.strip().lower(): i for i, h in enumerate(header)}
    mapping: dict[str, int] = {}
    for field, cands in ALIASES.items():
        for c in cands:
            if c.lower() in lower:
                mapping[field] = lower[c.lower()]
                break
    missing = REQUIRED - set(mapping)
    return mapping, missing


def ingest_file(con: sqlite3.Connection, path: Path, year: int) -> tuple[int, set[str]]:
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader)
        mapping, missing = _map_header(header)
        if missing:
            return 0, missing
        g = lambda row, k: (row[mapping[k]].strip()
                            if k in mapping and mapping[k] < len(row) else None)
        rows = []
        for row in reader:
            if not row:
                continue
            rows.append((
                year, g(row, "control_no"), g(row, "difficulty_date"),
                g(row, "tail"), g(row, "aircraft_make"), g(row, "aircraft_model"),
                g(row, "jasc_code"), g(row, "part_name"), g(row, "part_number"),
                g(row, "part_condition"), g(row, "stage"),
                g(row, "discrepancy"), now,
            ))
            if len(rows) >= 20000:
                con.executemany(_INS, rows); n += len(rows); rows = []
        if rows:
            con.executemany(_INS, rows); n += len(rows)
    con.commit()
    return n, set()


_INS = ("INSERT INTO sdr_raw(source_year,control_no,difficulty_date,tail,"
        "aircraft_make,aircraft_model,jasc_code,part_name,part_number,"
        "part_condition,stage,discrepancy,ingested_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)")


def run(con: sqlite3.Connection, sdr_dir: Path | None = None) -> None:
    sdr_dir = Path(sdr_dir or config.SDR_DIR)
    total = 0
    for year in config.SDR_YEARS:
        p = sdr_dir / f"SDR-{year}.csv"
        if not p.exists() or p.stat().st_size < 1024:
            print(f"  {year}: MISSING", flush=True)
            continue
        done = con.execute(
            "SELECT COUNT(*) FROM sdr_raw WHERE source_year=?", (year,)).fetchone()[0]
        if done:
            print(f"  {year}: already ingested ({done:,})", flush=True)
            total += done
            continue
        n, missing = ingest_file(con, p, year)
        if missing:
            print(f"  {year}: HEADER MISSING {sorted(missing)} — inspect!",
                  file=sys.stderr, flush=True)
        else:
            print(f"  {year}: {n:,} rows", flush=True)
            total += n
    print(f"TOTAL sdr_raw: {total:,}")
