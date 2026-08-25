"""Render the screenshots the README uses, against the demo database.

    python scripts/make_demo_db.py      # once
    python scripts/github_shots.py

Deliberately separate from `ui_preview.py`. That script shoots throwaway
fixtures to prove each screen *renders*; this one shoots the demo database to
show what the application *is*, which is a different job and wants different
screens.

Two constraints carried over from the preview script, both learned the hard
way:

* **Never run this with `QT_QPA_PLATFORM=offscreen`.** The offscreen plugin
  reports an empty font database, so every render comes back in a substituted
  face and shows a typography problem the real application does not have.
* **`WA_DontShowOnScreen`, not a hidden window.** It gives the full
  show/layout/polish cycle on the native platform without ever mapping a
  window to the display.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt                            # noqa: E402
from PySide6.QtWidgets import QApplication               # noqa: E402

from aivionics.ui import auth, fonts                     # noqa: E402
from aivionics.ui.app import AppContext, MainWindow, build_context  # noqa: E402
from aivionics.ui.login import LoginDialog               # noqa: E402

DEMO_DB = ROOT / "data" / "demo" / "aivionics-demo.db"
OUT = ROOT / "docs" / "screenshots"
SIZE = (1600, 950)


def settle(app: QApplication, seconds: float = 0.6) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def shoot(app: QApplication, widget, name: str, seconds: float = 0.6) -> None:
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    settle(app, seconds)
    path = OUT / f"{name}.png"
    widget.grab().save(str(path))
    print(f"  {path.relative_to(ROOT)}  {widget.width()}x{widget.height()}")


def main() -> int:
    if not DEMO_DB.exists():
        print(f"no demo database at {DEMO_DB.relative_to(ROOT)} — "
              f"run scripts/make_demo_db.py first")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv)
    ctx: AppContext = build_context(DEMO_DB)
    # `set_theme` persists to `settings`, so the dark shots at the end of the
    # last run left the database in dark and every "light" screenshot came out
    # dark. Start from a known theme rather than from whatever was left behind.
    ctx.theme_name = "light"
    app.setStyleSheet(fonts.qss(ctx.theme_name))
    ctx.user = auth.User(1, "s.asadli", "Sarvan Asadli", "engineer", False)
    print("rendering from the demo database:")

    login = LoginDialog(ctx.con, ctx.theme_name)
    shoot(app, login, "01-login", 0.4)
    login.close()

    window = MainWindow(ctx)
    window.resize(*SIZE)
    window.apply_context()

    for key, name, wait in (("home", "02-home", 0.8),
                            ("diagnose", "03-diagnose", 0.8),
                            ("manuals", "04-manuals", 1.2),
                            ("fleet", "05-fleet", 1.0),
                            ("reliability", "06-reliability", 1.4),
                            ("compliance", "07-compliance", 0.8),
                            ("ops", "08-ops-map", 1.6),
                            ("admin", "09-admin", 0.8)):
        window.navigate(key)
        shoot(app, window, name, wait)

    # the collapsed rail, because it is a feature people ask about
    window.navigate("diagnose")
    window.rail.set_expanded(False, animate=False)
    shoot(app, window, "10-rail-collapsed", 0.5)
    window.rail.set_expanded(True, animate=False)

    window.set_theme("dark")
    settle(app, 0.4)
    for key, name in (("home", "11-home-dark"), ("ops", "12-ops-dark")):
        window.navigate(key)
        shoot(app, window, name, 1.0)

    window.set_theme("light")           # leave the database as we found it
    window.close()
    print(f"\n{len(list(OUT.glob('*.png')))} screenshots in "
          f"{OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
