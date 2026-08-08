"""Database access for the UI.

Two distinct paths, deliberately:

``open_readonly`` opens the corpus database with ``mode=ro`` and returns
``None`` if it is absent, locked or unreadable. Every live-data view goes
through it, so a half-built or busy corpus can never be corrupted by the UI
and can never crash it either — the pages fall back to an empty state.

``ensure_ui_tables`` runs against the application's own writable connection
(login, audit, print log). It is purely additive — ``CREATE TABLE IF NOT
EXISTS`` and guarded ``ALTER TABLE`` — so it composes with the schema in
``aivionics.db`` without owning it.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import config

# ── settings (PLAN §2 rule 12: online features default off) ─────────────
DEFAULT_SETTINGS: dict[str, str] = {
    "online_enabled": "0",
    "theme": "light",
    "operator_name": "",
}

_UI_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def open_readonly(path: Path | str | None = None) -> sqlite3.Connection | None:
    """Open the corpus read-only. Returns None when unavailable.

    Tolerates: file missing, directory missing, another process holding a
    write lock, and a file that is not a database at all.
    """
    p = Path(path or config.DB_PATH)
    if not p.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True,
                              timeout=1.0, check_same_thread=False)
        con.execute("PRAGMA query_only=ON")
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return con
    except sqlite3.Error:
        return None


def ensure_ui_tables(con: sqlite3.Connection) -> None:
    """Additive DDL the UI needs on top of the core schema."""
    con.executescript(_UI_SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(app_user)")}
    if cols and "must_change_pw" not in cols:
        con.execute(
            "ALTER TABLE app_user ADD COLUMN must_change_pw INTEGER NOT NULL DEFAULT 0")
    for key, value in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                    (key, value))
    con.commit()


def get_setting(con: sqlite3.Connection | None, key: str,
                default: str | None = None) -> str | None:
    """Read a setting, tolerating a missing table or a missing connection."""
    if default is None:
        default = DEFAULT_SETTINGS.get(key)
    if con is None:
        return default
    try:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    return row[0] if row else default


def set_setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    con.commit()


def online_enabled(con: sqlite3.Connection | None) -> bool:
    return get_setting(con, "online_enabled", "0") in ("1", "true", "True")


def _safe(con: sqlite3.Connection | None, sql: str, args: tuple = ()) -> list:
    """Run a read query, returning [] on any schema or lock problem.

    The corpus is built by a separate process; tables this UI reads may not
    exist yet at any given moment. That is an empty state, not an error.
    """
    if con is None:
        return []
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


class CorpusReader:
    """Read-only view of the manual corpus for the Manuals browser."""

    def __init__(self, con: sqlite3.Connection | None):
        self.con = con

    @property
    def available(self) -> bool:
        return self.con is not None and bool(self.manuals())

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None

    def aircraft_types(self) -> list[str]:
        rows = _safe(self.con,
                     "SELECT DISTINCT aircraft_type FROM manual ORDER BY aircraft_type")
        return [r[0] for r in rows]

    def manuals(self, aircraft_type: str | None = None,
                manual_type: str | None = None) -> list[dict]:
        sql = ("SELECT id, oem, aircraft_type, manual_type, revision, revision_date, "
               "is_current, source_file FROM manual")
        where, args = [], []
        if aircraft_type:
            where.append("aircraft_type=?")
            args.append(aircraft_type)
        if manual_type:
            where.append("manual_type=?")
            args.append(manual_type)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY aircraft_type, manual_type, is_current DESC, revision DESC"
        keys = ("id", "oem", "aircraft_type", "manual_type", "revision",
                "revision_date", "is_current", "source_file")
        return [dict(zip(keys, r)) for r in _safe(self.con, sql, tuple(args))]

    def chapters(self, manual_id: int) -> list[dict]:
        """ATA chapters in a manual, with task counts and coverage %.

        Coverage is surfaced per PLAN 1.3 — a chapter below the Gate 1
        threshold must be visible as such, not silently thin.
        """
        rows = _safe(self.con, """
            SELECT t.ata_chapter,
                   COUNT(*)  AS n,
                   c.toc_count, c.extracted_count, c.pct
            FROM task t
            LEFT JOIN coverage c
                   ON c.manual_id = t.manual_id AND c.ata_chapter = t.ata_chapter
            WHERE t.manual_id = ?
            GROUP BY t.ata_chapter
            ORDER BY t.ata_chapter
        """, (manual_id,))
        return [{"chapter": r[0], "tasks": r[1], "toc_count": r[2],
                 "extracted": r[3], "pct": r[4]} for r in rows]

    def sections(self, manual_id: int, chapter: str) -> list[dict]:
        rows = _safe(self.con, """
            SELECT ata_section, COUNT(*)
            FROM task WHERE manual_id=? AND ata_chapter=?
            GROUP BY ata_section ORDER BY ata_section
        """, (manual_id, chapter))
        return [{"section": r[0], "tasks": r[1]} for r in rows]

    def tasks(self, manual_id: int, chapter: str, section: str | None = None,
              limit: int = 400) -> list[dict]:
        sql = ("SELECT task_number, title, function_code, catalogue_only, "
               "warning_count, caution_count, effectivity_raw "
               "FROM task WHERE manual_id=? AND ata_chapter=?")
        args: list = [manual_id, chapter]
        if section:
            sql += " AND ata_section=?"
            args.append(section)
        sql += " ORDER BY task_number LIMIT ?"
        args.append(limit)
        keys = ("task_number", "title", "function_code", "catalogue_only",
                "warning_count", "caution_count", "effectivity_raw")
        return [dict(zip(keys, r)) for r in _safe(self.con, sql, tuple(args))]

    def counts(self) -> dict[str, int]:
        """Headline counts for the status bar. Zeroes when the DB is absent."""
        def one(sql: str) -> int:
            rows = _safe(self.con, sql)
            return int(rows[0][0]) if rows else 0
        return {
            "tasks": one("SELECT COUNT(*) FROM task"),
            "amm": one("SELECT COUNT(*) FROM task WHERE catalogue_only=0"),
            "fim": one("SELECT COUNT(*) FROM task WHERE catalogue_only=1"),
            "cases": one("SELECT COUNT(*) FROM defect"),
            "aircraft": one("SELECT COUNT(*) FROM aircraft"),
        }
