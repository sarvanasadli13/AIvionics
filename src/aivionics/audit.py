"""Hash-chained audit log (PLAN 4.3): each row carries its predecessor's hash,
so an IT-admin role cannot silently rewrite history in the same file."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

GENESIS = "0" * 64


def _row_hash(ts: str, user_id, action: str, entity, entity_id,
              payload_hash: str, prev_hash: str) -> str:
    m = hashlib.sha256()
    m.update(json.dumps([ts, user_id, action, entity, entity_id,
                         payload_hash, prev_hash]).encode())
    return m.hexdigest()


def log(con: sqlite3.Connection, action: str, user_id: int | None = None,
        entity: str | None = None, entity_id: str | None = None,
        payload: dict | None = None, commit: bool = True) -> None:
    """Append one link to the chain.

    ``commit=False`` leaves the row in the caller's open transaction instead
    of committing it. That exists so an auditable act and its audit entry can
    be made atomic: committing the act first and then logging permits a
    durable change with no record of who made it, which is the one failure
    this table exists to prevent. Every existing caller keeps the old
    behaviour by default.
    """
    ts = datetime.now(timezone.utc).isoformat()
    payload_hash = hashlib.sha256(
        json.dumps(payload or {}, sort_keys=True).encode()).hexdigest()
    prev = con.execute(
        "SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = prev[0] if prev else GENESIS
    rh = _row_hash(ts, user_id, action, entity, entity_id, payload_hash, prev_hash)
    con.execute(
        "INSERT INTO audit_log(ts,user_id,action,entity,entity_id,"
        "payload_hash,prev_hash,row_hash) VALUES(?,?,?,?,?,?,?,?)",
        (ts, user_id, action, entity, entity_id, payload_hash, prev_hash, rh))
    if commit:
        con.commit()


def verify_chain(con: sqlite3.Connection) -> tuple[bool, int]:
    """Verify the whole chain. Returns (ok, rows_checked). Run on startup."""
    prev = GENESIS
    n = 0
    for (ts, user_id, action, entity, entity_id, payload_hash,
         prev_hash, row_hash) in con.execute(
            "SELECT ts,user_id,action,entity,entity_id,payload_hash,"
            "prev_hash,row_hash FROM audit_log ORDER BY id"):
        if prev_hash != prev:
            return False, n
        if _row_hash(ts, user_id, action, entity, entity_id,
                     payload_hash, prev_hash) != row_hash:
            return False, n
        prev = row_hash
        n += 1
    return True, n
