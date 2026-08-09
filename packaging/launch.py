"""Entry point for the frozen application.

`aivionics/ui/__main__.py` does `from .app import main`, which is right for
`python -m aivionics.ui` — the module is imported as part of its package and
the relative import resolves. PyInstaller freezes an entry script and runs it
as `__main__` with no package context, so that same line raises

    ImportError: attempted relative import with no known parent package

and the window never appears. This module is the frozen entry instead: an
absolute import, which works in both worlds.

It is kept in `packaging/` rather than in `src/` because it exists only for the
build. Nothing imports it, and the source tree keeps its own `__main__`.
"""
from __future__ import annotations

import os
import sys

# qtpy picks its bindings at import time, and qtawesome imports qtpy. Saying
# which one we mean removes the guess — PyQt5 is not bundled, but relying on
# "whatever it finds first" is how the wrong binding gets chosen on a machine
# that happens to have one.
os.environ.setdefault("QT_API", "pyside6")


def _excepthook(exc_type, exc, tb) -> None:
    """Show a startup failure instead of dying behind a hidden console.

    The application is built with `console=False`, so a traceback goes to a
    stream nobody is reading and the process simply disappears — or worse,
    lingers with no window, which is exactly how the first frozen build looked
    like it was working when it was not.
    """
    import traceback
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    sys.stderr.write(text)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv[:1])
        _ = app
        QMessageBox.critical(
            None, "AIvionics could not start",
            "The application failed during startup.\n\n"
            f"{exc_type.__name__}: {exc}\n\n"
            "The manuals and the CAMO remain the source of truth; nothing "
            "depends on this application being available.")
    except Exception:                                            # noqa: BLE001
        pass


def main() -> int:
    sys.excepthook = _excepthook
    from aivionics.ui.app import main as run
    return run()


if __name__ == "__main__":
    sys.exit(main())
