"""Gold-set adjudication tool (PLAN 0.7).

    python scripts/adjudicate.py [--db PATH] [--theme light|dark]

One defect/task pair per screen. Y / N / P / U set the verdict, Enter
commits and advances, Backspace steps back. Progress is written after every
commit, so the session can be closed at any point and resumed here.

Requires the stratified queue: run `scripts/build_gold_queue.py` first.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication          # noqa: E402

from aivionics import config, db                     # noqa: E402
from aivionics.ui import fonts                        # noqa: E402
from aivionics.ui import theme as T                  # noqa: E402
from aivionics.ui.adjudicator import AdjudicationQueue  # noqa: E402
from aivionics.ui.adjudicator_ui import AdjudicatorWindow  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="AIvionics gold-set adjudication")
    parser.add_argument("--db", type=Path, default=config.DB_PATH,
                        help="database to adjudicate against")
    parser.add_argument("--theme", choices=sorted(T.THEMES), default=T.DEFAULT_THEME)
    args = parser.parse_args()

    con = db.connect(args.db)
    if not AdjudicationQueue(con).exists:
        print("No `gold_queue` table in", args.db)
        print("Run scripts/build_gold_queue.py first to build the stratified queue.")
        # The window still opens and says the same thing, so the tool is not a
        # dead end when it is launched from a shortcut rather than a terminal.

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(fonts.qss(args.theme))
    window = AdjudicatorWindow(con, args.theme)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
