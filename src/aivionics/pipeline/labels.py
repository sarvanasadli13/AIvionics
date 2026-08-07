"""Phase 0.3-0.6 — build defect rows + silver labels + confidence tiers.

The metric is named correctly throughout: 'removals followed by a repeat of
the same defect within 30 days' — NEVER 'NFF' (a shop finding, absent here).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from . import references, splitter

NFF_LANG = re.compile(
    r"\bNO\s+FAULT\s+FOUND\b|\bNFF\b|\bCOULD\s+NOT\s+DUPLICATE\b|\bCND\b|"
    r"\bOPS?\s*C(?:Hec)?K\s+(?:GOOD|NORMAL|OK)\b|\bNO\s+DEFECT\s+(?:FOUND|NOTED)\b",
    re.I)
ROOT_CAUSE = re.compile(
    r"\bFOUND\b.{0,80}\b(CHAFED|BROKEN|CORRODED|CRACKED|LOOSE|OPEN|SHORTED|"
    r"WORN|LEAKING|CONTAMINAT|MOISTURE|WATER|BENT|SHEARED|FRAYED|BURNT|BURNED)\b",
    re.I)
REPLACED = re.compile(r"\bREPLACED?\b|\bR&R\b|\bREMOVED\s+AND\s+REPLACED\b", re.I)


def parse_date(s: str | None):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y%m%d", "%m/%d/%Y", "%Y-%m-%d", "%d-%b-%y", "%m/%d/%y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def build(con: sqlite3.Connection, batch: int = 50000) -> dict:
    """Populate defect + label_silver from sdr_raw. Idempotent (skips done)."""
    done = con.execute("SELECT COUNT(*) FROM defect").fetchone()[0]
    if done:
        print(f"  defect rows already built: {done:,}")
        return summary(con)

    whitelist = references.load_function_whitelist(con)
    if not whitelist:
        raise RuntimeError("function-code whitelist empty — run Phase 1 first")
    print(f"  function-code whitelist: {sorted(whitelist)[:12]}... "
          f"({len(whitelist)} codes)")

    cur = con.execute(
        "SELECT id, source_year, difficulty_date, tail, jasc_code, "
        "part_name, part_number, discrepancy FROM sdr_raw")
    d_rows, s_rows, n = [], [], 0
    while True:
        chunk = cur.fetchmany(batch)
        if not chunk:
            break
        for (sid, year, ddate, tail, jasc, pname, pno, disc) in chunk:
            defect_text, rect, leak_free = splitter.split(disc or "")
            d = parse_date(ddate)
            d_rows.append((sid, tail, d.isoformat() if d else None,
                           (jasc or "")[:2] or None, defect_text, rect, year))
        con.executemany(
            "INSERT INTO defect(sdr_id,aircraft_tail,reported_at,ata_ref,"
            "defect_text,rectification_text,sdr_year) VALUES(?,?,?,?,?,?,?)",
            d_rows)
        n += len(d_rows)
        d_rows = []
        print(f"  defects: {n:,}", flush=True)
    con.commit()

    # silver labels from citations in the RECTIFICATION side (post-split)
    cur = con.execute(
        "SELECT d.id, d.defect_text, d.rectification_text, d.sdr_year, "
        "s.discrepancy FROM defect d JOIN sdr_raw s ON s.id=d.sdr_id")
    n = 0
    while True:
        chunk = cur.fetchmany(batch)
        if not chunk:
            break
        for (did, dtext, rect, year, disc) in chunk:
            refs = references.extract(disc or "", whitelist)
            if not refs:
                continue
            leak_free = 1 if (rect and not references.extract(dtext or "", whitelist)
                              and len(dtext or "") >= splitter.MIN_QUERY_CHARS) else 0
            split_tag = "test" if (year or 0) >= 2024 else "train"
            for r in refs:
                s_rows.append((did, r["task_number"], r["manual_type"],
                               r["function_code"], leak_free, split_tag))
        if s_rows:
            con.executemany(
                "INSERT INTO label_silver(defect_id,task_number,manual_type,"
                "function_code,leak_free,split) VALUES(?,?,?,?,?,?)", s_rows)
            n += len(s_rows)
            s_rows = []
    con.commit()

    _tier(con)
    return summary(con)


def _tier(con: sqlite3.Connection) -> None:
    """HIGH: root-cause language, no NFF. LOW: NFF language or replaced+NFF.
    MED: everything else with a citation."""
    rows = con.execute(
        "SELECT ls.id, d.rectification_text, d.defect_text "
        "FROM label_silver ls JOIN defect d ON d.id=ls.defect_id").fetchall()
    upd = []
    for (lid, rect, dtext) in rows:
        blob = f"{dtext or ''} {rect or ''}"
        if NFF_LANG.search(blob):
            tier = "LOW"
        elif ROOT_CAUSE.search(blob):
            tier = "HIGH"
        else:
            tier = "MED"
        upd.append((tier, lid))
    con.executemany("UPDATE label_silver SET confidence_tier=? WHERE id=?", upd)
    con.commit()


def summary(con: sqlite3.Connection) -> dict:
    s = {}
    s["defects"] = con.execute("SELECT COUNT(*) FROM defect").fetchone()[0]
    s["silver"] = con.execute("SELECT COUNT(*) FROM label_silver").fetchone()[0]
    s["leak_free"] = con.execute(
        "SELECT COUNT(*) FROM label_silver WHERE leak_free=1").fetchone()[0]
    s["tiers"] = dict(con.execute(
        "SELECT confidence_tier, COUNT(*) FROM label_silver "
        "GROUP BY confidence_tier").fetchall())
    return s
