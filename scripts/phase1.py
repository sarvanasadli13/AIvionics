"""Phase 1 — extract the AMM task corpus + FIM catalogue + mmsg routing.

Run:  python scripts/phase1.py
Idempotent: re-running replaces the manual's rows.
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from aivionics import config, db
from aivionics.parsers import ata, fim_catalog
from aivionics.parsers.base import get_parser
from aivionics.parsers.boeing import classify_function


def upsert_manual(con, oem, actype, mtype, doc_std, source, revision):
    con.execute(
        "UPDATE manual SET is_current=0 WHERE aircraft_type=? AND manual_type=?",
        (actype, mtype))
    cur = con.execute(
        "INSERT INTO manual(oem,aircraft_type,manual_type,doc_standard,"
        "parser_plugin,revision,is_current,source_file,ingested_at) "
        "VALUES(?,?,?,?,?,?,1,?,?)",
        (oem, actype, mtype, doc_std, oem, revision, str(source),
         datetime.now(timezone.utc).isoformat()))
    return cur.lastrowid


def run_amm(con) -> None:
    parser = get_parser("boeing")
    mid = upsert_manual(con, "boeing", "737-8", "AMM", "ispec2200",
                        config.AMM_DIR, "2018-02-27")
    con.execute("DELETE FROM task_section WHERE task_id IN "
                "(SELECT id FROM task WHERE manual_id=?)", (mid,))
    con.execute("DELETE FROM task WHERE manual_id=?", (mid,))
    total = 0
    for pdf in sorted(config.AMM_DIR.glob("*.pdf")):
        ch = pdf.name[:2]
        try:
            tasks, mentions = parser.extract_chapter(pdf)
        except Exception as e:
            print(f"  ch {ch}: EXTRACTION FAILED — {e}", flush=True)
            con.execute("INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?)",
                        (mid, ch, 0, 0, 0.0))
            continue
        for t in tasks:
            body_hash = hashlib.sha256((t.body or "").encode()).hexdigest()
            cur = con.execute(
                "INSERT OR IGNORE INTO task(manual_id,task_number,function_code,"
                "title,ata_chapter,ata_section,ata_subject,effectivity_raw,body,"
                "body_hash,token_len,catalogue_only,embed_text,has_warning,"
                "has_caution,warning_count,caution_count) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
                (mid, t.task_number, t.function_code, t.title, t.ata_chapter,
                 t.ata_section, t.ata_subject, t.effectivity_raw, t.body,
                 body_hash, len((t.body or "").split()),
                 ata.build_embed_text(t.ata_chapter, t.ata_section,
                                      t.ata_subject, t.title),
                 int(bool(t.warnings)), int(bool(t.cautions)),
                 len(t.warnings), len(t.cautions)))
            tid = cur.lastrowid
            if tid:
                for i, w in enumerate(t.warnings):
                    con.execute("INSERT INTO task_section(task_id,seq,kind,text)"
                                " VALUES(?,?,?,?)", (tid, i, "warning", w))
                for i, c in enumerate(t.cautions):
                    con.execute("INSERT INTO task_section(task_id,seq,kind,text)"
                                " VALUES(?,?,?,?)", (tid, 100 + i, "caution", c))
                for r in t.references:
                    con.execute("INSERT INTO task_link VALUES(?,?,?)",
                                (tid, r, "reference"))
        pct = round(100 * len(tasks) / mentions, 1) if mentions else 0.0
        con.execute("INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?)",
                    (mid, ch, mentions, len(tasks), pct))
        total += len(tasks)
        print(f"  ch {ch}: {len(tasks)} tasks / {mentions} mentioned "
              f"({pct}%)", flush=True)
    con.commit()
    print(f"AMM total tasks: {total}")


def run_fim(con) -> None:
    fim = config.IFIM_DIR
    nav = next(fim.rglob("nav.js"))
    mmsg_p = next(fim.rglob("mmsg.js"))
    step = next(fim.rglob("stepIndex.js"))

    urls, airplanes = fim_catalog.parse_nav(nav)
    mmg = fim_catalog.parse_mmsg(mmsg_p)
    steps = fim_catalog.parse_step_index(step)
    n_codes = sum(len(v) for v in mmg.values() if isinstance(v, dict))
    print(f"  nav.js tasks: {len(urls)} · fleet airplanes: {len(airplanes)} · "
          f"mmsg LRUs: {len(mmg)} / codes: {n_codes} · stepIndex: {len(steps)}")
    if len(urls) != len(steps):
        print(f"  WARNING: nav.js/stepIndex task-count mismatch "
              f"({len(urls)} vs {len(steps)})", flush=True)

    mid = upsert_manual(con, "boeing", "737-8", "FIM", "metadata",
                        nav, "2017-08-15")
    con.execute("DELETE FROM task WHERE manual_id=?", (mid,))
    for tn, info in urls.items():
        core = tn[1:] if tn[0].isalpha() else tn
        ch, sec, subj, func, seq = core.split("-")
        title = info.get("title", "")
        con.execute(
            "INSERT OR IGNORE INTO task(manual_id,task_number,function_code,"
            "title,ata_chapter,ata_section,ata_subject,applic_refs,body,"
            "catalogue_only,embed_text) VALUES(?,?,?,?,?,?,?,?,NULL,1,?)",
            (mid, tn, func, title, ch, sec, subj, info.get("applicRefs"),
             ata.build_embed_text(ch, sec, subj, title)))

    con.execute("DELETE FROM mmsg_task")
    con.execute("DELETE FROM mmsg")
    # msd.mmg nests one level deeper than the code: LRU name -> code -> entry.
    # Verified 2026-08-08: 22 LRUs, 4,713 codes, no code shared between LRUs.
    for entries in mmg.values():
        if not isinstance(entries, dict):
            continue
        for code, entry in entries.items():
            text = entry.get("text", {})
            txt = (text.get("ALL") or next(iter(text.values()), "")
                   if isinstance(text, dict) else str(text))
            con.execute("INSERT OR REPLACE INTO mmsg VALUES(?,?,?,?)",
                        (code, code[:2], txt, entry.get("corr")))
            tasks = entry.get("task", {})
            if isinstance(tasks, dict):
                for seq_k, tnum in tasks.items():
                    try:
                        con.execute("INSERT OR REPLACE INTO mmsg_task VALUES(?,?,?)",
                                    (code, int(seq_k), tnum))
                    except (ValueError, TypeError):
                        pass
    con.execute("DELETE FROM effectivity_airplane")
    for ref, a in airplanes.items():
        if isinstance(a, dict) and "msn" in a:
            con.execute("INSERT OR REPLACE INTO effectivity_airplane "
                        "VALUES(?,?,?,?,?,?,?)",
                        (ref, a.get("model"), a.get("own"), a.get("msn"),
                         a.get("line"), a.get("tail") or a.get("reg"),
                         a.get("eng")))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM task WHERE manual_id=?", (mid,)).fetchone()[0]
    nm = con.execute("SELECT COUNT(*) FROM mmsg").fetchone()[0]
    nmt = con.execute("SELECT COUNT(*) FROM mmsg_task").fetchone()[0]
    ne = con.execute("SELECT COUNT(*) FROM effectivity_airplane").fetchone()[0]
    print(f"FIM catalogue rows: {n} · mmsg: {nm} · mmsg→task: {nmt} · "
          f"effectivity airplanes: {ne}")


if __name__ == "__main__":
    con = db.connect()
    print("== Phase 1: FIM catalogue + mmsg routing ==")
    run_fim(con)
    print("== Phase 1: AMM extraction ==")
    run_amm(con)
    print("== coverage ==")
    for row in con.execute(
            "SELECT ata_chapter, toc_count, extracted_count, pct FROM coverage "
            "ORDER BY ata_chapter"):
        print("  ", row)
