"""Render the Diagnose screen against the real corpus.

Standalone rather than part of `ui_preview.py` because that script builds a
full MainWindow through `build_context`, which opens the database read-write
and runs DDL. This one only ever reads, so it is safe to run while an index
build still holds the write lock.

    python scripts/preview_diagnose_live.py "CAPT AIRSPEED UNRELIABLE ..."
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtCore import Qt                                   # noqa: E402
from PySide6.QtWidgets import QApplication                      # noqa: E402

from aivionics import config                                    # noqa: E402
from aivionics.ui import fonts, store                           # noqa: E402
from aivionics.ui import theme as T                             # noqa: E402
from aivionics.ui.pages.diagnose import DiagnosePage            # noqa: E402
from aivionics.ui.searchservice import SearchService            # noqa: E402

OUT = ROOT / "docs" / "status"
DEFAULT_QUERY = ("CAPT AIRSPEED UNRELIABLE ON TAKEOFF ROLL, BITE CHECK SHOWS "
                 "NO CURRENT FAULTS")


class _Ctx:
    """The slice of AppContext this page touches, over a read-only handle."""

    def __init__(self, theme: str = "light"):
        self.theme_name = theme
        self.db_path = config.DB_PATH
        self.corpus = store.CorpusReader(store.open_readonly(config.DB_PATH))
        self.con = self.corpus.con
        self.user = None
        self.window = None

    def print_locator(self, *a, **kw):                           # unused here
        return ""


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY
    app = QApplication.instance() or QApplication(sys.argv[:1])

    for theme in ("light", "dark"):
        ctx = _Ctx(theme)
        if ctx.con is None:
            print("no database — run the Phase 0/1 ingest first")
            return 1
        app.setStyleSheet(fonts.qss(theme))
        page = DiagnosePage(ctx)
        page.resize(1400, 850)
        page.query.setText(query)

        # Run the search on this thread and hand the page its own result, so
        # the render does not depend on a worker thread completing mid-grab.
        result = page.service.run(query)
        page._on_results(result)

        # Lay out and grab without ever mapping to the display. Rendering under
        # QT_QPA_PLATFORM=offscreen is not equivalent — the offscreen platform
        # reports an empty font database on this machine, so screenshots taken
        # that way show a substituted typeface the real app never uses.
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.show()
        for _ in range(8):
            app.processEvents()
        suffix = "" if theme == "light" else "-dark"
        path = OUT / f"ui-preview-diagnose-live{suffix}.png"
        page.grab().save(str(path))
        print(f"  {path.name}  ({len(result['tasks'].results)} locators, "
              f"{len(result['case_rows'])} cases)")
        page.close()
        page.service.close()
        ctx.corpus.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
