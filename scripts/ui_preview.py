"""Render the UI to PNGs so it can be reviewed without a display.

    python scripts/ui_preview.py

Writes docs/status/ui-preview-*.png. Everything runs against throwaway
fixture databases in a temp directory — this script never touches
data/aivionics.db.

Run it on the native platform, not with QT_QPA_PLATFORM=offscreen: the
offscreen plugin reports an empty font database, so the renders come back in
a substituted face and show a typography problem the real app does not have.
Windows are laid out and grabbed with WA_DontShowOnScreen, so nothing is ever
mapped to the display.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt                       # noqa: E402
from PySide6.QtWidgets import QApplication          # noqa: E402

from aivionics import config, db                     # noqa: E402
from aivionics.ui import auth                        # noqa: E402
from aivionics.ui import fonts                        # noqa: E402
from aivionics.ui import theme as T                  # noqa: E402
from aivionics.ui.adjudicator_ui import AdjudicatorWindow   # noqa: E402
from aivionics.ui.app import AppContext, MainWindow, build_context  # noqa: E402
from aivionics.ui.login import LoginDialog           # noqa: E402

OUT = ROOT / "docs" / "status"


def settle(app: QApplication, rounds: int = 8) -> None:
    for _ in range(rounds):
        app.processEvents()


def settle_for(app: QApplication, seconds: float) -> None:
    """Pump the event loop for a while — QPdfView renders pages off-thread,
    so a bare processEvents() can grab a blank canvas."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def shoot(app: QApplication, widget, name: str) -> Path:
    """Lay the widget out fully, then grab it without putting it on screen.

    Deliberately *not* run under QT_QPA_PLATFORM=offscreen: that plugin
    reports an empty font database on this machine, so every render came
    back in a substituted face and the screenshots showed a typography
    problem the real app does not have. WA_DontShowOnScreen gives the full
    show/layout/polish cycle on the native platform — real Segoe UI Variable
    and Cascadia Mono — while never mapping a window.
    """
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    settle(app)
    path = OUT / f"ui-preview-{name}.png"
    widget.grab().save(str(path))
    print(f"  {path.relative_to(ROOT)}  {widget.width()}x{widget.height()}")
    return path


def seed_corpus(con) -> None:
    """A minimal but realistic corpus for the adjudicator screenshot."""
    # source_file points at the real corpus so the PDF viewer preview opens an
    # actual AMM chapter rather than a stub.
    con.execute(
        "INSERT INTO manual(id,oem,aircraft_type,manual_type,doc_standard,"
        "revision,revision_date,is_current,source_file) VALUES"
        "(1,'boeing','B737-8','AMM','ispec2200','48','2026-06-15',1,?)",
        (str(config.AMM_DIR),))
    # A task that genuinely exists in chapter 34 of the corpus (heading on
    # page 39), so "Open in manual" can demonstrate the page jump.
    con.execute(
        "INSERT INTO task(id,manual_id,task_number,function_code,title,"
        "ata_chapter,ata_section,ata_subject,effectivity_raw,catalogue_only) "
        "VALUES(2,1,'34-00-00-440-801','440',"
        "'MMEL 34-11-01 (DDG) Restoration — air data system','34','00','00',"
        "'TBC ALL',0)")
    con.execute(
        "INSERT INTO task(id,manual_id,task_number,function_code,title,ata_chapter,"
        "ata_section,ata_subject,effectivity_raw,body,catalogue_only,"
        "warning_count,caution_count) VALUES(1,1,'34-11-01-200-801','200',"
        "'Pitot probe heater — inspection / check','34','11','01',"
        "'AIRPLANES WITH PITOT PROBE HEATER MOD 34-1180',?,0,1,2)",
        ("1. General\n"
         "   A. This task gives the procedure to do a check of the pitot probe\n"
         "      heater element and its bonding at the connector.\n"
         "   B. Do this check when an airspeed disagree is reported and the\n"
         "      BITE shows no current fault.\n\n"
         "2. Prepare for the check\n"
         "   A. Make sure that the probe is cool before you touch it.\n"
         "   B. Open, tag and safety these circuit breakers:\n"
         "      P6-4  CAPT PITOT HEATER\n"
         "      P6-4  F/O PITOT HEATER\n\n"
         "3. Do the resistance check\n"
         "   A. Disconnect connector D2418 at the probe.\n"
         "   B. Measure across pins 1 and 2. The resistance must be 8 to 14 ohms.\n"
         "   C. If the resistance is not in that range, replace the probe.\n",))
    for seq, kind, text in (
            (1, "warning", "DO NOT TOUCH THE PITOT PROBE WHEN THE HEATER IS ON. "
                           "THE PROBE IS HOT ENOUGH TO CAUSE BURN INJURIES."),
            (2, "caution", "DO NOT USE A SOLVENT ON THE PROBE. SOLVENT CAN CAUSE "
                           "DAMAGE TO THE HEATER ELEMENT SEAL."),
            (3, "caution", "MAKE SURE THE CIRCUIT BREAKERS STAY OPEN. AN ENERGISED "
                           "HEATER WILL GIVE A FALSE RESISTANCE READING.")):
        con.execute("INSERT INTO task_section(task_id,seq,kind,text) VALUES(1,?,?,?)",
                    (seq, kind, text))
    con.execute(
        "INSERT INTO coverage(manual_id,ata_chapter,toc_count,extracted_count,pct) "
        "VALUES(1,'34',120,110,91.7)")
    con.execute(
        "INSERT INTO defect(id,aircraft_tail,reported_at,ata_ref,fault_code,"
        "defect_text,rectification_text,source,sdr_year) VALUES(1,'N8942L',"
        "'2026-06-03','3411',NULL,?,?,'sdr',2026)",
        ("CAPT AIRSPEED UNRELIABLE ON TAKEOFF ROLL, AIRSPEED DISAGREE ANNUNCIATED "
         "AT APPROXIMATELY 60 KTS. CREW REJECTED TAKEOFF. BITE CHECK ACCOMPLISHED "
         "ON GROUND SHOWS NO CURRENT FAULTS.",
         "REPLACED CAPT PITOT PROBE IAW AMM 34-11-01-400-801. OPS CHECK GOOD."))
    # gold_queue is created by db.SCHEMA and is UNIQUE(defect_id, task_number),
    # so each queue row needs its own defect.
    con.execute("INSERT INTO gold_queue(defect_id,task_number,stratum,seq,done) "
                "VALUES(1,'34-11-01-200-801','ata34-diagnostic',1,0)")
    for i in range(2, 41):
        con.execute(
            "INSERT INTO defect(id,aircraft_tail,reported_at,ata_ref,defect_text,"
            "source,sdr_year) VALUES(?,?,?,'3411',?,'sdr',2026)",
            (i, f"N{8000 + i}L", "2026-05-01",
             "AIRSPEED DISAGREE REPORTED IN CRUISE."))
        con.execute("INSERT INTO gold_queue(defect_id,task_number,stratum,seq,done) "
                    "VALUES(?,'34-11-01-200-801',?,?,?)",
                    (i, "ata34-action" if i % 2 else "ata34-diagnostic", i,
                     1 if i <= 12 else 0))
    con.commit()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="aivionics-preview-"))
    app = QApplication.instance() or QApplication(sys.argv)

    try:
        empty_db = tmp / "empty.db"
        ctx = build_context(empty_db)
        app.setStyleSheet(fonts.qss(ctx.theme_name))
        print("rendering:")

        # (a) login
        login = LoginDialog(ctx.con, ctx.theme_name)
        shoot(app, login, "login")
        login.close()

        # (b) home  (c) manuals empty state — corpus absent
        ctx.user = auth.User(1, "s.asadli", "Sarvan Asadli", "engineer", False)
        window = MainWindow(ctx)
        window.resize(1440, 900)
        window.apply_context()
        window.navigate("home")
        shoot(app, window, "home")

        window.navigate("manuals")
        shoot(app, window, "manuals-empty")

        window.navigate("diagnose")
        shoot(app, window, "diagnose")

        window.navigate("compliance")
        shoot(app, window, "compliance")

        window.set_theme("dark")
        window.navigate("home")
        shoot(app, window, "home-dark")
        window.close()

        # (d) adjudicator, one synthetic pair
        full_db = tmp / "corpus.db"
        con = db.connect(full_db)
        auth.seed(con)
        seed_corpus(con)

        adj = AdjudicatorWindow(con, "light")
        adj.resize(1340, 880)
        shoot(app, adj, "adjudicator")
        adj.set_theme("dark")
        shoot(app, adj, "adjudicator-dark")
        adj.close()

        # manuals with a real corpus, to prove the tree and coverage column
        ctx2 = build_context(full_db)
        ctx2.user = auth.User(1, "s.asadli", "Sarvan Asadli", "engineer", False)
        w2 = MainWindow(ctx2)
        w2.resize(1440, 900)
        w2.apply_context()
        w2.navigate("manuals")
        settle(app)
        if w2.pages["manuals"].tree.topLevelItemCount():
            item = w2.pages["manuals"].tree.topLevelItem(0)
            item.setExpanded(True)
            settle(app)
            if item.childCount():
                w2.pages["manuals"].tree.setCurrentItem(item.child(0))
        shoot(app, w2, "manuals-loaded")

        # (e) the in-app PDF viewer, opened on a real AMM chapter page
        manuals = w2.pages["manuals"]
        target = None
        for i in range(manuals.tree.topLevelItemCount()):
            chapter = manuals.tree.topLevelItem(i)
            chapter.setExpanded(True)
            settle(app)
            for j in range(chapter.childCount()):
                child = chapter.child(j)
                meta = child.data(0, Qt.ItemDataRole.UserRole) or {}
                if meta.get("task", {}).get("task_number") == "34-00-00-440-801":
                    target = child
                    break
            if target:
                break
        if target is not None:
            manuals.tree.setCurrentItem(target)
            settle(app)
            manuals._open_in_manual()
            settle_for(app, 3.0)          # page rendering happens off-thread
            shoot(app, w2, "manuals-pdf")
            manuals.viewer.set_theme = getattr(manuals.viewer, "set_theme", None)
            print(f"     viewer page: {manuals.viewer.page_edit.text()} "
                  f"{manuals.viewer.page_total.text()}")
        else:
            print("  ! chapter 34 task not in the tree — PDF preview skipped")
        w2.close()
        con.close()
        ctx.con.close()
        ctx2.con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
