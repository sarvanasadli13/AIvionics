# PyInstaller spec — AIvionics (PLAN Phase 6)
#
#     pyinstaller packaging/aivionics.spec --noconfirm
#
# One-folder, not one-file. A one-file build unpacks itself to a temp directory
# on every launch, which costs seconds with PySide6 and — more to the point —
# is the pattern corporate EDR flags hardest. One-folder also lets IT see what
# actually ships.
#
# The database is deliberately NOT bundled. It is built on site by the ingest
# scripts and lives in the per-user data directory; shipping a copy would put a
# stale corpus inside Program Files where nobody would think to look at it.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
BLOCK_CIPHER = None

# assets/ carries the OurAirports CSVs (19.5 MB, public domain) and the icon
# set. assets/data is matched by the repository .gitignore but must ship.
datas = [
    (str(ROOT / "assets" / "icons"), "assets/icons"),
    (str(ROOT / "assets" / "aircraft"), "assets/aircraft"),
    (str(ROOT / "assets" / "flags"), "assets/flags"),
    (str(ROOT / "assets" / "data"), "assets/data"),
    (str(ROOT / "assets" / "LICENSES.md"), "assets"),
]

hiddenimports = [
    "aivionics.parsers.boeing",      # registered by import side effect
    "qtpy", "qtawesome",            # icon fonts, resolved through qtpy
    *collect_submodules("aivionics"),
]

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Two classes of exclusion, both load-bearing.
    #
    # 1. Other Qt bindings. PyInstaller refuses to build at all when it sees
    #    two ("attempt to collect multiple Qt bindings packages"), and PyQt5
    #    gets pulled in here purely because `qtpy` is installed in the
    #    development environment. The application imports neither — verified,
    #    not assumed.
    #    `qtpy` itself must SHIP: qtawesome imports it to find the bindings.
    #    Excluding it produced a frozen build that died with
    #    "No module named 'qtpy'". With PyQt5/6 excluded it can only resolve
    #    to PySide6, and launch.py pins QT_API to say so explicitly.
    # 2. torch and transformers. Nothing in this application imports them:
    #    embedding is fastembed over onnxruntime and reranking is FlashRank,
    #    also onnxruntime. PyInstaller's hooks collect torch speculatively
    #    because transformers happens to be installed, and torch alone is
    #    roughly 2 GB — it would dominate the installer for a dependency that
    #    is never loaded (PLAN §4: onnxruntime, not torch).
    excludes=[
        "PyQt5", "PyQt6", "PySide2",
        "torch", "torchvision", "torchaudio", "transformers", "tensorboard",
        # 3. Data and cloud libraries reached transitively and never imported:
        #    pyarrow 79 MB, scipy 58 MB, duckdb 31 MB, babel 31 MB,
        #    botocore 23 MB. Checked the same way — none appears in
        #    sys.modules after importing every module this app actually uses.
        "pyarrow", "duckdb", "botocore", "boto3", "babel", "scipy",
        "pandas", "PIL", "IPython", "jedi", "notebook", "pytest",
        "tkinter", "matplotlib", "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtCharts",
        "PySide6.QtDataVisualization", "PySide6.QtMultimedia",
        "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtBluetooth",
        "PySide6.QtNetworkAuth", "PySide6.QtPositioning", "PySide6.QtSensors",
        "PySide6.QtSerialPort", "PySide6.QtTest", "PySide6.QtWebSockets",
    ],
    noarchive=False,
    block_cipher=BLOCK_CIPHER,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=BLOCK_CIPHER)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AIvionics",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX compression is itself an AV heuristic trigger
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "packaging" / "aivionics.ico")
    if (ROOT / "packaging" / "aivionics.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AIvionics",
)
