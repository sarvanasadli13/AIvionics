"""Index builders (PLAN 2.1-2.3).

What gets indexed, and why:

  vec_index kind='task'   every task row — AMM bodies AND FIM catalogue rows —
                          over ``task.embed_text`` (ATA hierarchy + title).
                          The body is deliberately excluded: it churns across
                          revisions, 61.8% of it truncates at 512 tokens, and
                          the 3,674 FIM rows have no body at all.
  vec_index kind='case'   only defects carrying at least one label_silver row.
                          Embedding all ~1.7M SDR defects would cost ~2.6 GB
                          for rows nothing can be evaluated against.
  task_fts / case_fts     everything. FTS is cheap and it is the only channel
                          that reliably hits exact tokens (task numbers, part
                          numbers, SMYD).

Idempotent: a rebuild for a given index_version deletes that version's vectors
first and clears the FTS tables, so re-running never double-counts. Other
index versions are left alone.
"""
from __future__ import annotations

import sqlite3
from typing import Callable, Iterator, Sequence

from .embedder import Embedder, vec_to_blob

Progress = Callable[[str], None]

TASK_SELECT = """
SELECT t.id, COALESCE(NULLIF(TRIM(t.embed_text), ''),
                      NULLIF(TRIM(t.title), ''), t.task_number)
FROM task t ORDER BY t.id
"""

# labelled cases only — see module docstring
CASE_SELECT = """
SELECT d.id, d.defect_text
FROM defect d
WHERE TRIM(COALESCE(d.defect_text, '')) <> ''
  AND EXISTS (SELECT 1 FROM label_silver ls WHERE ls.defect_id = d.id)
ORDER BY d.id
"""

TASK_FTS_SELECT = """
SELECT id, COALESCE(task_number, ''), COALESCE(title, ''),
       COALESCE(embed_text, '')
FROM task ORDER BY id
"""

CASE_FTS_SELECT = """
SELECT id, defect_text FROM defect
WHERE TRIM(COALESCE(defect_text, '')) <> '' ORDER BY id
"""


def _chunks(rows: Sequence, size: int) -> Iterator[Sequence]:
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def _clear_fts(con: sqlite3.Connection, table: str) -> None:
    """Contentless FTS5 tables are emptied with the 'delete-all' command."""
    try:
        con.execute(f"INSERT INTO {table}({table}) VALUES('delete-all')")
    except sqlite3.OperationalError:
        con.execute(f"DELETE FROM {table}")


def _build_vectors(
    con: sqlite3.Connection,
    embedder: Embedder,
    kind: str,
    select: str,
    batch: int,
    progress: Progress | None,
) -> int:
    con.execute(
        "DELETE FROM vec_index WHERE kind=? AND index_version=?",
        (kind, embedder.index_version),
    )
    rows = con.execute(select).fetchall()
    written = 0
    for chunk in _chunks(rows, batch):
        vecs = embedder.embed([text or "" for _, text in chunk])
        con.executemany(
            "INSERT OR REPLACE INTO vec_index(kind, ref_id, index_version, dim, vec)"
            " VALUES(?,?,?,?,?)",
            [
                (kind, ref_id, embedder.index_version, embedder.dim,
                 vec_to_blob(vecs[i]))
                for i, (ref_id, _) in enumerate(chunk)
            ],
        )
        written += len(chunk)
        if progress:
            progress(f"  {kind} vectors: {written:,}/{len(rows):,}")
    con.commit()
    return written


def build_task_vectors(con, embedder, batch: int = 256,
                       progress: Progress | None = None) -> int:
    return _build_vectors(con, embedder, "task", TASK_SELECT, batch, progress)


def build_case_vectors(con, embedder, batch: int = 256,
                       progress: Progress | None = None) -> int:
    return _build_vectors(con, embedder, "case", CASE_SELECT, batch, progress)


def build_task_fts(con: sqlite3.Connection) -> int:
    """rowid is set explicitly to task.id — the table is contentless, so the
    rowid is the only link back to the row it describes."""
    _clear_fts(con, "task_fts")
    rows = con.execute(TASK_FTS_SELECT).fetchall()
    con.executemany(
        "INSERT INTO task_fts(rowid, task_number, title, embed_text)"
        " VALUES(?,?,?,?)", rows)
    con.commit()
    return len(rows)


def build_case_fts(con: sqlite3.Connection) -> int:
    _clear_fts(con, "case_fts")
    rows = con.execute(CASE_FTS_SELECT).fetchall()
    con.executemany("INSERT INTO case_fts(rowid, defect_text) VALUES(?,?)", rows)
    con.commit()
    return len(rows)


def build_all(
    con: sqlite3.Connection,
    embedder: Embedder,
    batch: int = 256,
    progress: Progress | None = None,
    cases: bool = True,
) -> dict:
    """Full rebuild for one index_version. Returns the row counts written."""
    out = {
        "index_version": embedder.index_version,
        "model": embedder.model_name,
        "dim": embedder.dim,
        "task_fts": build_task_fts(con),
        "case_fts": build_case_fts(con),
        "task_vectors": build_task_vectors(con, embedder, batch, progress),
    }
    out["case_vectors"] = (
        build_case_vectors(con, embedder, batch, progress) if cases else 0)
    return out


def index_stats(con: sqlite3.Connection) -> dict:
    rows = con.execute(
        "SELECT kind, index_version, COUNT(*), MIN(dim) FROM vec_index"
        " GROUP BY kind, index_version ORDER BY index_version, kind").fetchall()
    return {
        "vectors": [
            {"kind": k, "index_version": v, "rows": n, "dim": d}
            for (k, v, n, d) in rows
        ],
        "task_fts": con.execute("SELECT COUNT(*) FROM task_fts").fetchone()[0],
        "case_fts": con.execute("SELECT COUNT(*) FROM case_fts").fetchone()[0],
    }
