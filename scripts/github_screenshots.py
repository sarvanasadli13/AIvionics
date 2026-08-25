"""Screenshots fit to publish.

Deliberately different from `github_shots.py`, which shoots whatever is on
screen including the manual viewer. **No shot here renders a page of the
AMM.** The corpus is Boeing maintenance data stamped "UNRELEASED DATA — USE
FOR REFERENCE ONLY"; a screenshot of it in a public repository distributes
controlled OEM content under someone else's trademark, whatever the
surrounding application looks like.

So the Manuals screen is shown as its ATA tree and its document classifier —
which is the part worth showing anyway, because it is ours — and never as a
rendered procedure.

    python scripts/github_screenshots.py

Never run with QT_QPA_PLATFORM=offscreen: the offscreen plugin reports an
empty font database and every render comes back in a substituted face.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
DEMO = ROOT / "data" / "demo" / "aivionics-demo.db"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication                      # noqa: E402

_APP = QApplication.instance() or QApplication(sys.argv[:1])

from PySide6.QtCore import Qt                                   # noqa: E402

from aivionics.ui import auth, fonts                            # noqa: E402
from aivionics.ui.app import MainWindow, build_context          # noqa: E402

SIZE = (1600, 950)

# key, filename, theme, and what to do before the shot.
SHOTS = [
    ("home", "01-home", "light", None),
    ("diagnose", "02-diagnose", "light", None),
    ("manuals", "03-manuals-ata-tree", "light", "manuals_737"),
    ("manuals", "04-manuals-training-document", "light", "manuals_a320"),
    ("fleet", "05-fleet", "light", None),
    ("reliability", "06-reliability", "light", None),
    ("compliance", "07-compliance", "light", None),
    ("ops", "08-ops", "light", None),
    ("validation", "09-ai-validation-dashboard", "light", None),
    ("validation", "10-ai-validation-case", "light", "resume_case"),
    ("about", "11-about", "light", None),
    ("admin", "12-admin-ai-assistant", "light", "admin_ai"),
    ("home", "13-home-dark", "dark", None),
    ("diagnose", "14-diagnose-dark", "dark", None),
]


def settle(seconds: float = 0.7) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        _APP.processEvents()
        time.sleep(0.02)


def prepare(window, action: str | None) -> None:
    if action == "manuals_737":
        page = window.pages["manuals"]
        index = page.type_combo.findText("737-8")
        if index >= 0:
            page.type_combo.setCurrentIndex(index)
        settle(0.5)
        # Expand one chapter so the tree shows task locators, which are our
        # index — not manual text.
        if page.tree.topLevelItemCount():
            page.tree.topLevelItem(0).setExpanded(True)
    elif action == "manuals_a320":
        page = window.pages["manuals"]
        index = page.type_combo.findText("A320 family")
        if index >= 0:
            page.type_combo.setCurrentIndex(index)
    elif action == "resume_case":
        panel = window.pages["validation"].panel
        panel.resume()
    elif action == "admin_ai":
        page = window.pages["admin"]
        tabs = [page.tabs.tabText(i) for i in range(page.tabs.count())]
        if "AI assistant" in tabs:
            page.tabs.setCurrentIndex(tabs.index("AI assistant"))


def main() -> int:
    if not DEMO.exists():
        print(f"no demo database at {DEMO} — run scripts/make_demo_db.py "
              f"then scripts/make_demo_complete.py")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    ctx = build_context(DEMO)
    ctx.theme_name = "light"
    _APP.setStyleSheet(fonts.qss("light"))
    ctx.user = auth.User(1, "admin", "S. Asadli", "admin", False)

    window = MainWindow(ctx)
    window.resize(*SIZE)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    settle(1.2)

    current_theme = "light"
    print(f"writing to {OUT.relative_to(ROOT)}")
    for key, name, theme, action in SHOTS:
        if theme != current_theme:
            window.set_theme(theme)
            _APP.setStyleSheet(fonts.qss(theme))
            current_theme = theme
            settle(0.8)
        window.navigate(key)
        settle(0.6)
        prepare(window, action)
        settle(0.8)
        path = OUT / f"{name}.png"
        window.grab().save(str(path))
        print(f"  {path.name}")

    # A publishable set must not contain a rendered manual page.
    viewer = window.pages["manuals"].viewer
    if window.pages["manuals"].stack.currentWidget() is viewer:
        print("\nREFUSED: the manuals page ended on the PDF viewer")
        return 1
    print(f"\n{len(SHOTS)} screenshots — none renders a manual page")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
