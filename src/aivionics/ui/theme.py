"""Design tokens and QSS generation (PLAN §4A.1).

One token dict per theme; both stylesheets are generated from it, so the
palette is defined exactly once. Light is the default.

The tokens are transcribed from ``docs/mockups/fault-lookup.html`` — that
mockup is the approved visual identity and this module is its Qt port.

Rules encoded here, not left to the caller:
  * the accent (``cy`` / ``cyf``) is never red, amber or green — those three
    are load-bearing *data* colours (compliance state, repeat-defect
    severity, coverage warnings) and nothing non-status may wear one;
  * every status colour is paired with an icon and a word by the widgets
    that use it — colour alone never carries meaning (~8% of male engineers
    are red-green colour deficient and this is a safety tool).

BreezeStyleSheets (MIT) was the surveyed choice in PLAN §4A. It is a theme
*generator* keyed on its own colour map, and porting this palette into it
buys nothing at v1 scale, so the stylesheet below is hand-rolled from the
token dict instead. The seam is the token dict either way.
"""
from __future__ import annotations

from string import Template

# ── palettes ────────────────────────────────────────────────────────────
# Keys mirror the CSS custom properties in the mockup one-for-one.

LIGHT: dict[str, str] = {
    "bg": "#EAF2F9",
    "s1": "#FFFFFF", "s2": "#F6FAFD", "s3": "#E6F1FA", "well": "#FFFFFF",
    "line": "#CBE0F0", "hair": "#E4EFF8",
    "txt": "#0F1F2C", "txt2": "#48607A", "txt3": "#7A93AB",
    "cy": "#0E74BC", "cyf": "#2E9BE0", "cyq": "#E2F1FC", "cyl": "#0A5A93",
    "amb": "#95600A", "ambq": "#FCF0DA", "ambl": "#764A00",
    "red": "#BE262C", "redq": "#FCE3E4", "redl": "#94181D",
    "grn": "#1B7642", "grnq": "#DDF0E5", "grnl": "#11552F",
    "railtop": "#FFFFFF", "railbot": "#EAF2F9",
    "closehover": "#C0272C",
}

DARK: dict[str, str] = {
    "bg": "#08131E",
    "s1": "#0F1C29", "s2": "#152435", "s3": "#1D3043", "well": "#061019",
    "line": "#22394E", "hair": "#182838",
    "txt": "#DCE9F5", "txt2": "#93AEC6", "txt3": "#63809A",
    "cy": "#5BB4F0", "cyf": "#3E9FE0", "cyq": "#0D2F4A", "cyl": "#95D2F8",
    "amb": "#F0A93B", "ambq": "#3A2A10", "ambl": "#F3C77E",
    "red": "#F2555A", "redq": "#3A1418", "redl": "#F79EA1",
    "grn": "#46B36A", "grnq": "#12301E", "grnl": "#8AD3A2",
    "railtop": "#0F1C29", "railbot": "#08131E",
    "closehover": "#C0272C",
}

THEMES: dict[str, dict[str, str]] = {"light": LIGHT, "dark": DARK}
DEFAULT_THEME = "light"

# Faint engineering-paper grid painted behind the shell, as (r, g, b, alpha).
GRID_RGBA: dict[str, tuple[int, int, int, int]] = {
    "light": (20, 80, 140, 13),
    "dark": (120, 180, 235, 9),
}
GRID_STEP = 34

# Status colours are always addressed through this map so no widget invents
# its own. `word` is mandatory — the badge renders icon + word + colour.
STATUS = {
    "ok":      {"token": "grn", "quiet": "grnq", "word": "OK",       "icon": "mdi6.check-circle-outline"},
    "warn":    {"token": "amb", "quiet": "ambq", "word": "CAUTION",  "icon": "mdi6.alert-outline"},
    "alert":   {"token": "red", "quiet": "redq", "word": "ALERT",    "icon": "mdi6.alert-circle-outline"},
    "info":    {"token": "cy",  "quiet": "cyq",  "word": "INFO",     "icon": "mdi6.information-outline"},
    "unknown": {"token": "txt3", "quiet": "s3",  "word": "NO DATA",  "icon": "mdi6.help-circle-outline"},
}

UI_FONT = "Segoe UI Variable Text, Segoe UI, system-ui, sans-serif"
MONO_FONT = "Cascadia Mono, Cascadia Code, Consolas, monospace"
MONO_FAMILIES = ["Cascadia Mono", "Cascadia Code", "Consolas", "Courier New"]
UI_FAMILIES = ["Segoe UI Variable Text", "Segoe UI", "Arial"]

# Dense data views: 28–32 px rows (PLAN §4A.1 rule 3). Air only in the Home hero.
ROW_HEIGHT = 30

# ── stylesheet ──────────────────────────────────────────────────────────
# `string.Template` rather than str.format: QSS is full of braces.

_QSS = Template("""
/* ── base ─────────────────────────────────────────────────────────── */
QWidget {
    background: transparent;
    color: $txt;
    font-family: $ui_font;
    font-size: 13px;
}
QMainWindow, QDialog { background: $bg; }
QToolTip {
    background: $s1; color: $txt; border: 1px solid $line;
    padding: 4px 7px; border-radius: 3px;
}

/* Focus is always visible, at >=3:1 against its surface. */
*:focus { outline: none; }
QPushButton:focus, QToolButton:focus, QLineEdit:focus,
QComboBox:focus, QTreeWidget:focus, QTableWidget:focus, QListWidget:focus {
    border: 2px solid $cy;
}

/* ── frameless shell ──────────────────────────────────────────────── */
#WindowFrame { background: $bg; border: 1px solid $line; }

#TitleBar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 $s2, stop:1 $s1);
    border-bottom: 1px solid $line;
}
#AppName { font-size: 25px; font-weight: 650; }
#TitleContext {
    color: $txt2; font-size: 12px;
    padding-left: 15px; border-left: 1px solid $line;
}
#TitleBadge {
    color: $txt3; font-size: 10px; font-weight: 600;
    border: 1px solid $line; border-radius: 3px; padding: 2px 8px;
}
#WinBtn, #WinBtnClose {
    background: transparent; border: none; border-radius: 0px; color: $txt2;
    font-size: 12px; width: 46px; padding: 0px;
}
#WinBtn:hover { background: $s3; color: $txt; }
#WinBtn:focus, #WinBtnClose:focus { border: 2px solid $cy; }
#WinBtnClose:hover { background: $closehover; color: #FFFFFF; }

#ThemeToggle {
    background: $well; border: 1px solid $line; border-radius: 5px;
}
#ThemeBtn {
    background: transparent; border: none; border-radius: 3px; color: $txt3;
}
#ThemeBtn:hover { color: $txt2; }
#ThemeBtn:checked { background: $s3; color: $cy; }

/* ── left rail ────────────────────────────────────────────────────── */
#Rail {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 $railtop, stop:1 $railbot);
    border-right: 1px solid $line;
}
#RailBtn {
    background: transparent; border: none; border-radius: 6px;
    color: $txt3; padding: 0px;
}
#RailBtn:hover { background: $s2; color: $txt2; }
#RailBtn:checked { background: $cyq; color: $cy; }
#RailBtn:disabled { color: $txt3; }
#RailSep { background: $line; }

/* ── page furniture ───────────────────────────────────────────────── */
#PageHeader {
    background: $s2; border-bottom: 1px solid $line;
}
#PageTitle {
    font-size: 10px; font-weight: 700; color: $txt2;
    letter-spacing: 1px;
}
#Placard { font-size: 10px; font-weight: 600; color: $txt3; letter-spacing: 1px; }
#Card {
    background: $s1; border: 1px solid $line; border-radius: 7px;
}
#Well {
    background: $well; border: 1px solid $line; border-radius: 6px;
}
#Band {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 $s2, stop:1 $s1);
    border-bottom: 1px solid $line;
}
#Hairline { background: $line; }
#Muted { color: $txt2; }
#Faint { color: $txt3; }
#Accent { color: $cy; }
#HeroValue { font-size: 30px; font-weight: 600; color: $txt; }
#HeroCaption { color: $txt2; font-size: 12px; }

/* Provenance line — standing rule 2. Never hidden, never a tooltip. */
#Provenance { color: $txt3; font-size: 10px; }

/* Decision-support disclaimer — PLAN §0.2. */
#Disclaimer {
    color: $txt3; font-size: 10px; font-weight: 600; letter-spacing: 1px;
}

/* ── inputs ───────────────────────────────────────────────────────── */
QLineEdit {
    background: $well; border: 1px solid $line; border-radius: 6px;
    padding: 7px 11px; color: $txt; selection-background-color: $cyq;
    selection-color: $txt;
}
QLineEdit:focus { border: 2px solid $cy; padding: 6px 10px; }
QLineEdit:disabled { color: $txt3; background: $s2; }

QComboBox {
    background: $well; border: 1px solid $line; border-radius: 6px;
    padding: 5px 26px 5px 9px; color: $txt; min-height: 20px;
}
QComboBox:hover { border-color: $cy; }
QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: center right;
    border: none; background: transparent; width: 24px;
}
QComboBox::down-arrow { image: url("$chevron"); width: 13px; height: 13px; }
QComboBox QAbstractItemView {
    background: $s1; border: 1px solid $line; color: $txt;
    selection-background-color: $cyq; selection-color: $cy; outline: none;
}

QPushButton {
    background: $s1; border: 1px solid $line; border-radius: 6px;
    padding: 7px 15px; color: $txt; font-weight: 600;
}
QPushButton:hover { background: $s3; border-color: $cy; }
QPushButton:pressed { background: $cyq; }
QPushButton:disabled { color: $txt3; background: $s2; border-color: $hair; }

/* Solid accent fill, always white text on it. Never a status colour. */
#Primary {
    background: $cyf; border: 1px solid $cyf; color: #FFFFFF; font-weight: 650;
    padding: 8px 20px;
}
#Primary:hover { background: $cy; border-color: $cy; }
#Primary:pressed { background: $cyl; border-color: $cyl; }
#Primary:disabled { background: $s3; border-color: $line; color: $txt3; }

#LinkBtn {
    background: transparent; border: none; color: $cy; font-weight: 600;
    padding: 2px 4px; text-decoration: underline;
}

/* ── dense data views (28–32 px rows, tabular numerals) ───────────── */
QHeaderView::section {
    background: $s2; color: $txt3; border: none;
    border-bottom: 1px solid $line; border-right: 1px solid $hair;
    padding: 6px 10px; font-size: 9px; font-weight: 700; letter-spacing: 1px;
    text-align: left;
}
QTableWidget, QTableView, QTreeWidget, QTreeView, QListWidget {
    background: $s1; border: none; gridline-color: $hair;
    alternate-background-color: $s2; color: $txt;
    selection-background-color: $cyq; selection-color: $txt;
    outline: none;
}
QTableWidget::item, QTableView::item {
    border-bottom: 1px solid $hair; padding: 4px 10px;
}
QTableWidget::item:selected, QTableView::item:selected { background: $cyq; color: $txt; }
QTreeWidget::item, QTreeView::item { padding: 4px 3px; border: none; }
QTreeWidget::item:hover, QTreeView::item:hover { background: $s2; }
QTreeWidget::item:selected, QTreeView::item:selected { background: $cyq; color: $cy; }
QTableCornerButton::section { background: $s2; border: none; }

/* ── tabs ─────────────────────────────────────────────────────────────
   Unstyled QTabBar draws grey-on-grey against these surfaces and the
   unselected tabs effectively disappear. Selection is carried by an accent
   underline and a weight change, so it survives in both themes and does not
   depend on colour alone. */
QTabWidget::pane { border: none; border-top: 1px solid $hair; background: $s1; }
QTabBar { background: transparent; qproperty-drawBase: 0; }
QTabBar::tab {
    background: transparent; color: $txt2; border: none;
    border-bottom: 2px solid transparent;
    padding: 7px 15px; margin-right: 2px;
    font-size: 10px; font-weight: 600;
}
QTabBar::tab:hover { color: $txt; background: $s2; }
QTabBar::tab:selected {
    color: $cy; border-bottom: 2px solid $cyf; background: transparent;
}
QTabBar::tab:focus { outline: none; }

/* ── scrollbars ───────────────────────────────────────────────────── */
QScrollBar:vertical { background: transparent; width: 11px; margin: 0; }
QScrollBar::handle:vertical {
    background: $line; border-radius: 4px; min-height: 26px; margin: 2px;
}
QScrollBar::handle:vertical:hover { background: $txt3; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 0; }
QScrollBar::handle:horizontal {
    background: $line; border-radius: 4px; min-width: 26px; margin: 2px;
}
QScrollBar::handle:horizontal:hover { background: $txt3; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QAbstractScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }

/* ── status bar ───────────────────────────────────────────────────── */
#StatusBar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 $s1, stop:1 $s2);
    border-top: 1px solid $line;
    color: $txt3;
}
#StatusBar QLabel { font-size: 10px; color: $txt3; }

/* ── read-only body pane (task text renders verbatim, rule 3) ─────── */
QPlainTextEdit, QTextEdit {
    background: $well; border: 1px solid $line; border-radius: 6px;
    color: $txt; padding: 8px; selection-background-color: $cyq;
    selection-color: $txt;
}

QProgressBar {
    background: $hair; border: none; border-radius: 3px;
    height: 6px; text-align: center; color: $txt2;
}
QProgressBar::chunk { background: $cyf; border-radius: 3px; }

QCheckBox { color: $txt; spacing: 7px; }
QCheckBox::indicator {
    width: 15px; height: 15px; border: 1px solid $line; border-radius: 3px;
    background: $well;
}
QCheckBox::indicator:checked { background: $cyf; border-color: $cyf; }

QSplitter::handle { background: $line; image: none; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }
QMenu {
    background: $s1; border: 1px solid $line; color: $txt; padding: 4px;
}
QMenu::item { padding: 5px 22px; border-radius: 4px; }
QMenu::item:selected { background: $cyq; color: $cy; }
""")


def build_qss(theme: str = DEFAULT_THEME, ui_family: str | None = None,
              mono_family: str | None = None) -> str:
    """Compile the stylesheet for `theme` from its token dict.

    `ui_family` / `mono_family` take a single resolved family name — see
    `fonts.qss`, which picks one against the installed font database. The
    comma-separated defaults are only a fallback for callers without a Qt
    application (the tests).

    Raises KeyError for an unknown theme name, and — via
    ``Template.substitute`` — for any placeholder the token dict is missing,
    so a typo fails loudly at startup rather than rendering an unstyled app.
    """
    tokens = dict(THEMES[theme])
    tokens["ui_font"] = ui_family or UI_FONT
    tokens["mono_font"] = mono_family or MONO_FONT
    tokens["chevron"] = chevron_path(theme)
    return _QSS.substitute(tokens)


def chevron_path(theme: str) -> str:
    """Combo-box disclosure arrow. Qt draws no native arrow on a styled
    QComboBox, so the asset is referenced explicitly. Forward slashes: QSS
    `url()` does not accept Windows backslashes."""
    from .. import config
    return (config.ASSETS_DIR / "icons" / f"chevron-down-{theme}.svg").as_posix()


def token(name: str, theme: str = DEFAULT_THEME) -> str:
    return THEMES[theme][name]


def status_style(kind: str, theme: str = DEFAULT_THEME) -> dict[str, str]:
    """Colour + word + icon for a status. Callers must render all three."""
    spec = STATUS[kind]
    pal = THEMES[theme]
    return {
        "color": pal[spec["token"]],
        "quiet": pal[spec["quiet"]],
        "word": spec["word"],
        "icon": spec["icon"],
    }


def accent_is_status_free() -> bool:
    """Guard for the §4A.1 rule that the accent may never be R/A/G.

    Asserted by the test suite so a future palette edit cannot quietly put a
    status colour in the accent slot.
    """
    for pal in THEMES.values():
        reserved = {pal["red"].upper(), pal["amb"].upper(), pal["grn"].upper()}
        if {pal["cy"].upper(), pal["cyf"].upper()} & reserved:
            return False
    return True
