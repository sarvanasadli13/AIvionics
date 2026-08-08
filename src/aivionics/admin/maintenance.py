"""Backup, integrity and version reporting (PLAN Phase 6).

Three operational rules the plan states, implemented rather than documented:

* **Back up with `VACUUM INTO`, never by copying the file.** A live SQLite
  database in WAL mode is a file plus a write-ahead log; copying the file alone
  captures a torn state that restores as corruption, and copying both without
  a checkpoint captures them at different instants. `VACUUM INTO` asks SQLite
  for a consistent snapshot and writes it as a single clean file.
* **A backup nobody has restored is not a backup.** `verify_backup` opens the
  copy, runs an integrity check and counts the rows that matter, so a failure
  surfaces at backup time instead of on the day it is needed.
* **Every version is stamped and reported together** — app, schema, index and
  each model. Changing the embedding model silently invalidates every stored
  vector and every measurement taken with it (standing rule 9), so the version
  set is one object and appears whole or not at all.
"""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import audit, config

SCHEMA_VERSION = "1"

# Tables whose row counts are compared across a backup. Losing any of these
# silently is the failure mode a copied file produces.
CHECKED_TABLES = ("task", "defect", "label_silver", "vec_index",
                  "compliance_item", "audit_log")


@dataclass
class BackupResult:
    path: Path
    bytes_written: int = 0
    duration_s: float = 0.0
    integrity: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return (not self.error and self.integrity == "ok"
                and self.counts == self.source_counts)

    def summary(self) -> str:
        if self.error:
            return f"backup failed — {self.error}"
        if self.integrity != "ok":
            return f"backup written but integrity check said: {self.integrity}"
        missing = {t: (self.source_counts.get(t, 0), self.counts.get(t, 0))
                   for t in self.source_counts
                   if self.source_counts.get(t) != self.counts.get(t)}
        if missing:
            return f"backup row counts differ from the source: {missing}"
        rows = sum(self.counts.values())
        return (f"{self.path.name} · {self.bytes_written / 1e6:.1f} MB · "
                f"{rows:,} rows verified · {self.duration_s:.1f}s")


def table_counts(con: sqlite3.Connection,
                 tables: tuple[str, ...] = CHECKED_TABLES) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in tables:
        try:
            out[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            continue            # table not in this schema version; not an error
    return out


def integrity_check(con: sqlite3.Connection) -> str:
    """`ok`, or the first problem SQLite reports."""
    try:
        rows = con.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        return f"unreadable: {exc}"
    return rows[0][0] if rows else "no result"


def backup(con: sqlite3.Connection, dest: Path | str,
           *, overwrite: bool = False) -> BackupResult:
    """Snapshot the database with `VACUUM INTO`, then verify the copy."""
    dest = Path(dest)
    result = BackupResult(path=dest)
    if dest.exists():
        if not overwrite:
            result.error = f"{dest} already exists"
            return result
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    result.source_counts = table_counts(con)
    try:
        # VACUUM INTO takes the path as a bound parameter; it cannot be
        # interpolated, which also keeps a path with quotes in it harmless.
        con.execute("VACUUM INTO ?", (str(dest),))
    except sqlite3.Error as exc:
        result.error = str(exc)
        return result
    result.duration_s = (datetime.now(timezone.utc) - started).total_seconds()
    result.bytes_written = dest.stat().st_size
    verify_backup(result)
    return result


def verify_backup(result: BackupResult) -> BackupResult:
    """Open the copy and check it — a backup nobody opened is not a backup."""
    try:
        con = sqlite3.connect(f"file:{result.path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        result.error = f"cannot open the backup: {exc}"
        return result
    try:
        result.integrity = integrity_check(con)
        result.counts = table_counts(con, tuple(result.source_counts) or CHECKED_TABLES)
    finally:
        con.close()
    return result


def restore(backup_path: Path | str, dest: Path | str,
            *, overwrite: bool = False) -> Path:
    """Put a verified backup back. Refuses to clobber unless told to.

    The check runs before anything is replaced: restoring a corrupt backup over
    a working database turns a recoverable situation into an unrecoverable one.
    """
    backup_path, dest = Path(backup_path), Path(dest)
    if not backup_path.exists():
        raise FileNotFoundError(backup_path)
    con = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    try:
        state = integrity_check(con)
    finally:
        con.close()
    if state != "ok":
        raise ValueError(f"refusing to restore a backup that fails its "
                         f"integrity check: {state}")
    if dest.exists() and not overwrite:
        raise FileExistsError(f"{dest} exists; pass overwrite=True to replace it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # The -wal and -shm of the live database belong to the file being replaced;
    # leaving them behind would let SQLite reapply a log to a database that no
    # longer matches it.
    for suffix in ("-wal", "-shm"):
        sidecar = dest.with_name(dest.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    shutil.copyfile(backup_path, dest)
    return dest


@dataclass(frozen=True)
class Versions:
    """What this installation is running, reported as one object."""

    app: str
    schema: str
    index_version: str
    embed_model: str
    embed_dim: int
    task_vectors: int = 0
    case_vectors: int = 0
    reranker: str = ""
    llm_model: str = ""
    audit_chain_ok: bool | None = None
    audit_rows: int = 0

    def lines(self) -> list[str]:
        chain = ("verified" if self.audit_chain_ok
                 else "BROKEN" if self.audit_chain_ok is False else "not checked")
        return [
            f"Application   {self.app}",
            f"Schema        {self.schema}",
            f"Index         {self.index_version} "
            f"({self.task_vectors:,} task · {self.case_vectors:,} case vectors)",
            f"Embedding     {self.embed_model} · {self.embed_dim} dim",
            f"Reranker      {self.reranker or 'none'}",
            f"LLM           {self.llm_model or 'not configured'}",
            f"Audit chain   {chain} over {self.audit_rows:,} rows",
        ]


def app_version() -> str:
    try:
        from importlib.metadata import version
        return version("aivionics")
    except Exception:                                            # noqa: BLE001
        return "0.1.0+source"


def versions(con: sqlite3.Connection | None = None, *,
             reranker: str = "", llm_model: str = "") -> Versions:
    task_vectors = case_vectors = 0
    chain_ok: bool | None = None
    rows = 0
    if con is not None:
        try:
            counts = dict(con.execute(
                "SELECT kind, COUNT(*) FROM vec_index WHERE index_version=?"
                " GROUP BY kind", (config.INDEX_VERSION,)).fetchall())
            task_vectors = counts.get("task", 0)
            case_vectors = counts.get("case", 0)
        except sqlite3.Error:
            pass
        try:
            chain_ok, rows = audit.verify_chain(con)
        except sqlite3.Error:
            chain_ok = None
    return Versions(
        app=app_version(), schema=SCHEMA_VERSION,
        index_version=config.INDEX_VERSION, embed_model=config.EMBED_MODEL,
        embed_dim=config.EMBED_DIM, task_vectors=task_vectors,
        case_vectors=case_vectors, reranker=reranker, llm_model=llm_model,
        audit_chain_ok=chain_ok, audit_rows=rows)


def startup_report(con: sqlite3.Connection) -> dict:
    """Run at launch: integrity, audit chain, versions. Cheap enough to always do."""
    chain_ok, rows = audit.verify_chain(con)
    return {
        "integrity": integrity_check(con),
        "audit_chain_ok": chain_ok,
        "audit_rows": rows,
        "versions": versions(con),
    }


def default_backup_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"aivionics-{stamp}.db"
