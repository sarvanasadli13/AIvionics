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

from PySide6.QtWidgets import QApplication, QDialog          # noqa: E402

from aivionics import config, db, goldreview                     # noqa: E402
from aivionics.ui import auth, fonts                        # noqa: E402
from aivionics.ui import theme as T                  # noqa: E402
from aivionics.ui.adjudicator import AdjudicationQueue  # noqa: E402
from aivionics.ui.adjudicator_ui import AdjudicatorWindow  # noqa: E402
from aivionics.ui.login import LoginDialog  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="AIvionics gold-set adjudication")
    parser.add_argument("--db", type=Path, default=config.DB_PATH,
                        help="database to adjudicate against")
    parser.add_argument("--theme", choices=sorted(T.THEMES), default=T.DEFAULT_THEME)
    args = parser.parse_args()

    con = db.connect(args.db)
    auth.seed(con)
    # The standalone tool runs the same migration and holds the same
    # permissions as the application. It is a different window, not a
    # different set of rules.
    goldreview.migrate(con)
    if not AdjudicationQueue(con).exists:
        print("No `gold_queue` table in", args.db)
        print("Run scripts/build_gold_queue.py first to build the stratified queue.")
        # The window still opens and says the same thing, so the tool is not a
        # dead end when it is launched from a shortcut rather than a terminal.

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(fonts.qss(args.theme))

    # A real sign-in, exactly as the application does. There is deliberately
    # no `--user` flag: a command-line switch that names the reviewer would
    # let anyone with a shell attribute a gold answer to somebody else, and
    # every finalized answer here carries an authenticated user id.
    dialog = LoginDialog(con, args.theme)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return 0
    user = dialog.user
    if not goldreview.may_review(con, user):
        print(f"{user.username} does not hold "
              f"'{goldreview.GOLD_REVIEW_PERMISSION}'.")
        print("An administrator grants it on the role. Opening read-only.")

    window = AdjudicatorWindow(con, args.theme, user=user)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
