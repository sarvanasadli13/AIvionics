"""Render the application mark to a Windows .ico (PLAN Phase 6).

The PyInstaller spec references `packaging/aivionics.ico` *if it exists*, which
is why the first builds shipped with the default interpreter icon and nobody
noticed: a missing icon was silently no icon.

Windows picks a different size for the taskbar, the title bar, Alt-Tab, the
desktop and Explorer's various view modes, so the file carries all of them.
The 16 px entry matters most and is rendered directly rather than downsampled
from 256 — scaling a detailed mark to 16 px turns it into mud.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtCore import Qt                               # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer                       # noqa: E402

SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
SOURCE = ROOT / "assets" / "icons" / "mark-light.svg"
TARGET = ROOT / "packaging" / "aivionics.ico"


def render(svg: Path, size: int) -> QImage:
    """One square frame, rendered at its final size rather than downsampled."""
    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise SystemExit(f"cannot read {svg}")
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter)
    painter.end()
    return image


def to_pillow(image: QImage, tmp: Path, size: int):
    """Via a PNG on disk rather than a QBuffer.

    `QBuffer(QByteArray())` segfaults: the temporary QByteArray is collected
    while the buffer still points at it. A file is slower by microseconds and
    cannot crash.
    """
    from PIL import Image
    path = tmp / f"icon-{size}.png"
    if not image.save(str(path), "PNG"):
        raise SystemExit(f"could not write {path}")
    with Image.open(path) as handle:
        return handle.convert("RGBA")


def main() -> int:
    import tempfile
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    _ = app
    tmp = Path(tempfile.mkdtemp(prefix="aivionics-icon-"))
    frames = [to_pillow(render(SOURCE, s), tmp, s) for s in SIZES]
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    # Pillow writes every supplied size into the .ico when `sizes` names them.
    frames[-1].save(TARGET, format="ICO",
                    sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])
    print(f"wrote {TARGET.relative_to(ROOT)} "
          f"({TARGET.stat().st_size / 1024:.1f} KB, {len(SIZES)} sizes: "
          f"{', '.join(str(s) for s in SIZES)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
