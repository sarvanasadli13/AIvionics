"""About AIvionics — identity, purpose, safety posture and system information.

Every dynamic value on this page is read at display time from the same source
the rest of the application uses: `admin.maintenance.app_version()` for the
version, `SCHEMA_VERSION` for the schema, the corpus reader for manual counts,
`assets/LICENSES.md` for attribution. Nothing here is a second copy of a fact
held elsewhere, because a second copy is a fact that can go stale silently.

Works entirely offline: the logo is a bundled asset and no text on the page is
fetched.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from ... import config
from ...admin import maintenance
from .. import theme as T
from ..widgets import (Placard, SectionHeader, StatusBadge, mono_font,
                       svg_pixmap, ui_font)
from .base import Page, scroll_host

PRODUCT = "AIvionics"
TAGLINE = "Intelligent Avionics Engineering Workstation"

CREATOR_NAME = "Sarvan Asadli"
CREATOR_TITLE = (
    "M.Sc. Electrical Engineering & Information Technology |\n"
    "Avionics Engineer (B.Sc. Honours) |\n"
    "AI & LLM Systems |\n"
    "RDMA, GPU-to-GPU & FPGA Research")
CREATOR_STATEMENT = ("AIvionics was conceived, created and directed by "
                     "Sarvan Asadli.")

ABOUT_TEXT = [
    "AIvionics is a local-first intelligent avionics engineering workstation "
    "designed to help engineers investigate aircraft defects, identify probable "
    "causes, locate applicable ATA documentation, review relevant maintenance "
    "procedures and prepare approved pages for technicians.",
    "The platform brings engineering diagnostics, maintenance documentation, "
    "historical defect information, fleet awareness, live aircraft tracking, "
    "airport information, weather and operational data into one unified "
    "environment.",
    "AIvionics is designed as an engineering decision-support system. It helps "
    "engineers retrieve, organize and evaluate evidence while keeping qualified "
    "personnel in control of every maintenance decision.",
]

PURPOSE_ITEMS = [
    "Aircraft defect reports",
    "Historical maintenance cases",
    "ATA documentation",
    "AMM, FIM/TSM, WDM, IPC, SRM, CMM and MEL documents",
    "Fleet and aircraft information",
    "Airport and flight information",
    "Weather and operational data",
    "Separate diagnostic and reporting tools",
]

UNIVERSAL_TEXT = [
    "AIvionics is not limited to one aircraft type. It is designed to support "
    "multiple manufacturers, aircraft families, models and variants through "
    "separate aircraft-specific knowledge packages.",
    "Each knowledge package can contain applicable manuals, revisions, "
    "effectivity information and historical cases. AIvionics filters its "
    "evidence according to the selected aircraft and must not use documentation "
    "from an incompatible aircraft type.",
]

# Listed only as systems used during development. No roles, no categories, no
# per-model descriptions — that is a deliberate constraint from the owner.
AI_SYSTEMS = ["Kimi K3", "Claude Fable 5", "Claude Opus 5", "DeepSeek V4 Pro",
              "OpenAI Codex", "NVIDIA Nemotron"]

INDEPENDENCE = ("AIvionics is an independently created product. The use of "
                "these AI systems does not imply sponsorship, certification or "
                "endorsement by their developers or associated companies.")

WORKFLOW = [
    "Aircraft and defect information",
    "Aircraft-effectivity filtering",
    "Historical-case retrieval",
    "Applicable ATA and manual retrieval",
    "AI-assisted analysis",
    "Probable causes and confidence",
    "Exact document, task and page references",
    "Engineer review and feedback",
]

WORKFLOW_NOTE = ("AIvionics does not replace maintenance manuals. It helps "
                 "find and organize relevant evidence from approved sources.")

SAFETY_HEADLINE = (
    "AIvionics is an engineering decision-support system. It does not replace "
    "approved aircraft maintenance documentation, organizational procedures, "
    "licensed personnel or regulatory requirements. All recommendations must be "
    "independently verified before maintenance action.")

SAFETY_POINTS = [
    "Probable causes are advisory recommendations, not confirmed diagnoses.",
    "AI-generated information must be supported by traceable evidence.",
    "Missing or insufficient evidence must be clearly disclosed.",
    "Manual applicability and aircraft effectivity must be verified.",
    "Only approved documents and pages may be released or printed for "
    "technicians.",
    "Final maintenance decisions remain the responsibility of authorized "
    "personnel.",
]

CONNECTIVITY_POINTS = [
    "Core manual access and previously indexed engineering information can work "
    "locally.",
    "Some operational features require an internet connection.",
    "Live aircraft tracking, current airport data, weather, imagery and other "
    "external information depend on their respective online data providers.",
    "Online status does not change the authority or applicability of approved "
    "maintenance documents.",
]

UNAVAILABLE = "Not available"
UNCONFIGURED = "Not configured"


class AboutPage(Page):
    title = "About"

    def __init__(self, ctx, parent: QWidget | None = None):
        super().__init__(ctx, parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QWidget()
        self.lay = QVBoxLayout(body)
        self.lay.setContentsMargins(26, 22, 26, 26)
        self.lay.setSpacing(16)

        self.lay.addWidget(self._header())
        self.lay.addWidget(self._prose("About AIvionics", ABOUT_TEXT))
        self.lay.addWidget(self._purpose())
        self.lay.addWidget(self._prose("Universal aircraft support",
                                       UNIVERSAL_TEXT))
        self.lay.addWidget(self._creator())
        self.lay.addWidget(self._development())
        self.lay.addWidget(self._workflow())
        self.lay.addWidget(self._safety())
        self.lay.addWidget(self._connectivity())
        self.lay.addWidget(self._system_information())
        self.lay.addWidget(self._licences())
        self.lay.addWidget(self._footer())
        self.lay.addStretch(1)

        # Long content must scroll rather than be clipped at small sizes.
        outer.addWidget(scroll_host(body), 1)

    # ── building blocks ───────────────────────────────────────────────
    def _card(self, title: str = "", right: str = "") -> tuple:
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 16, 20, 18)
        lay.setSpacing(9)
        if title:
            lay.addWidget(SectionHeader(title, right))
        return card, lay

    def _para(self, text: str, size: float = 10.5) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setFont(ui_font(size))
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setMaximumWidth(980)
        # A word-wrapped QLabel reports the height of a single line unless it
        # is told its height depends on its width. Without this the last line
        # of every multi-line paragraph was clipped by the card.
        policy = label.sizePolicy()
        policy.setHeightForWidth(True)
        policy.setVerticalPolicy(QSizePolicy.Policy.MinimumExpanding)
        label.setSizePolicy(policy)
        label.setMinimumHeight(label.heightForWidth(880))
        return label

    def _bullets(self, items: list[str]) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        for item in items:
            lay.addWidget(self._para(f"·   {item}", 10))
        return host

    # ── 1. header ─────────────────────────────────────────────────────
    def _header(self) -> QWidget:
        card, lay = self._card()
        row = QHBoxLayout()
        row.setSpacing(18)

        mark = QLabel()
        icon = config.ASSETS_DIR / "icons" / (
            f"mark-{'dark' if self.theme_name == 'dark' else 'light'}.svg")
        if icon.exists():
            mark.setPixmap(svg_pixmap(icon, 78))
        self.mark_label = mark
        row.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)

        titles = QVBoxLayout()
        titles.setSpacing(3)
        name = QLabel(PRODUCT)
        name.setFont(ui_font(27, QFont.Weight.Bold))
        titles.addWidget(name)
        tag = QLabel(TAGLINE)
        tag.setFont(ui_font(13))
        tag.setObjectName("Muted")
        titles.addWidget(tag)
        self.version_line = QLabel("")
        self.version_line.setFont(mono_font(9.5, QFont.Weight.Normal))
        self.version_line.setObjectName("Faint")
        titles.addWidget(self.version_line)
        row.addLayout(titles, 1)
        lay.addLayout(row)
        return card

    # ── 3. purpose ────────────────────────────────────────────────────
    def _purpose(self) -> QWidget:
        card, lay = self._card("Purpose", "why it exists")
        lay.addWidget(self._para(
            "AIvionics was created to reduce the time engineers spend moving "
            "between:"))
        lay.addWidget(self._bullets(PURPOSE_ITEMS))
        lay.addWidget(self._para(
            "The system is intended to make troubleshooting faster, more "
            "organized, traceable and evidence-based."))
        return card

    def _prose(self, title: str, paragraphs: list[str]) -> QWidget:
        card, lay = self._card(title)
        for text in paragraphs:
            lay.addWidget(self._para(text))
        return card

    # ── 5. creator ────────────────────────────────────────────────────
    def _creator(self) -> QWidget:
        card, lay = self._card("Creator")
        name = QLabel(CREATOR_NAME)
        name.setFont(ui_font(18, QFont.Weight.DemiBold))
        lay.addWidget(name)
        # Exact spelling, punctuation, capitalisation and line breaks as
        # supplied by the owner. Rendered as given, not reflowed.
        title = QLabel(CREATOR_TITLE)
        title.setFont(ui_font(10.5))
        title.setObjectName("Muted")
        title.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(title)
        lay.addSpacing(4)
        lay.addWidget(self._para(CREATOR_STATEMENT))
        return card

    # ── 6. development ────────────────────────────────────────────────
    def _development(self) -> QWidget:
        card, lay = self._card("Development")
        lay.addWidget(self._para(
            "The following AI models and systems were used during the "
            "development of AIvionics:"))
        # Names only. No roles, no categories, no per-model descriptions —
        # attributing a part of the product to a particular system would be a
        # claim the owner has not made.
        lay.addWidget(self._bullets(AI_SYSTEMS))
        note = self._para(INDEPENDENCE, 9.5)
        note.setObjectName("Faint")
        lay.addWidget(note)
        return card

    # ── 7. how it works ───────────────────────────────────────────────
    def _workflow(self) -> QWidget:
        card, lay = self._card("How AIvionics works")
        pal = T.THEMES[self.theme_name]
        for i, step in enumerate(WORKFLOW):
            row = QHBoxLayout()
            row.setSpacing(9)
            num = QLabel(f"{i + 1}")
            num.setFixedWidth(22)
            num.setFont(mono_font(9.5, QFont.Weight.DemiBold))
            num.setStyleSheet(f"color:{pal['cy']};")
            row.addWidget(num, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(self._para(step, 10.5), 1)
            host = QWidget()
            host.setLayout(row)
            lay.addWidget(host)
        lay.addSpacing(4)
        lay.addWidget(self._para(WORKFLOW_NOTE))
        return card

    # ── 8. safety ─────────────────────────────────────────────────────
    def _safety(self) -> QWidget:
        card, lay = self._card()
        pal = T.THEMES[self.theme_name]
        card.setStyleSheet(
            f"QFrame#Card{{border:1px solid {pal['amb']};"
            f"border-left:4px solid {pal['amb']};background:{pal['ambq']};}}")
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(StatusBadge("warn", "safety", self.theme_name))
        head.addStretch(1)
        lay.addLayout(head)
        headline = self._para(SAFETY_HEADLINE, 11.5)
        headline.setFont(ui_font(11.5, QFont.Weight.DemiBold))
        lay.addWidget(headline)
        lay.addWidget(self._bullets(SAFETY_POINTS))
        return card

    # ── 9. connectivity ───────────────────────────────────────────────
    def _connectivity(self) -> QWidget:
        card, lay = self._card("Local-first and online functionality")
        lay.addWidget(self._bullets(CONNECTIVITY_POINTS))
        return card

    # ── 10. system information ────────────────────────────────────────
    def _system_information(self) -> QWidget:
        card, lay = self._card("System information", "read live")
        self.sysinfo_grid = QGridLayout()
        self.sysinfo_grid.setHorizontalSpacing(28)
        self.sysinfo_grid.setVerticalSpacing(6)
        lay.addLayout(self.sysinfo_grid)

        row = QHBoxLayout()
        row.addStretch(1)
        copy = QPushButton("Copy system information")
        copy.setToolTip("Copy a diagnostic summary for support. Credentials, "
                        "tokens, personal data and filesystem paths are "
                        "excluded.")
        copy.clicked.connect(self.copy_system_information)
        row.addWidget(copy)
        lay.addLayout(row)
        return card

    def system_information(self) -> list[tuple[str, str]]:
        """Everything shown in the card, read live. Never invented.

        A value that cannot be determined is reported as such rather than
        guessed — a plausible wrong build identifier in a support report is
        worse than an honest gap.
        """
        ctx = self.ctx
        con = getattr(ctx, "con", None)
        corpus = getattr(ctx, "corpus", None)

        version = maintenance.app_version() or UNAVAILABLE
        build_id, build_date = self._build_identity()

        manuals = pages = packages = UNAVAILABLE
        if corpus is not None:
            try:
                rows = corpus.manuals()
                manuals = str(len(rows))
                packages = str(len({r.get("aircraft_type") for r in rows
                                    if r.get("aircraft_type")}))
            except Exception:                                    # noqa: BLE001
                pass
        if con is not None:
            try:
                pages = f"{con.execute('SELECT COUNT(*) FROM task').fetchone()[0]:,}"
            except Exception:                                    # noqa: BLE001
                pass

        model, ai_status = self._ai_status()
        online = getattr(ctx, "online_enabled", None)
        reach = getattr(getattr(ctx, "window", None), "reachability", None)
        reachable = getattr(reach, "reachable", None) if reach else None
        if online is None:
            connectivity = UNAVAILABLE
        elif not online:
            connectivity = "Offline — outbound features switched off"
        elif reachable is False:
            connectivity = "Online enabled — no network route"
        else:
            connectivity = "Online"

        return [
            ("Application version", version),
            ("Build identifier", build_id),
            ("Build date", build_date),
            ("Database schema version", maintenance.SCHEMA_VERSION or UNAVAILABLE),
            ("Active AI model", model),
            ("AI integration status", ai_status),
            ("Installed manuals", manuals),
            ("Indexed manual tasks", pages),
            ("Aircraft knowledge packages", packages),
            ("Connectivity", connectivity),
            ("Operating mode", "Local-first desktop workstation"),
        ]

    def _build_identity(self) -> tuple[str, str]:
        """Build id and date, from the packaged metadata when present."""
        try:
            from importlib.metadata import distribution
            dist = distribution("aivionics")
            built = dist.read_text("BUILD") or ""
            if built.strip():
                parts = built.strip().splitlines()
                return (parts[0].strip() or UNAVAILABLE,
                        parts[1].strip() if len(parts) > 1 else UNAVAILABLE)
        except Exception:                                        # noqa: BLE001
            pass
        # Running from source: there is no build, and saying so is the honest
        # answer rather than inventing a hash.
        return "source checkout", UNAVAILABLE

    def _ai_status(self) -> tuple[str, str]:
        """Read from the shared configuration seam.

        About must not hold its own opinion about the AI layer; building a
        fresh `LLMConfig()` here reported the Ollama default regardless of
        what was configured.
        """
        try:
            from ...llm import aiconfig
            current = aiconfig.status(getattr(self.ctx, "con", None))
            return current.settings.display, current.label
        except Exception:                                        # noqa: BLE001
            return UNCONFIGURED, UNAVAILABLE

    def refresh_system_information(self) -> None:
        while self.sysinfo_grid.count():
            item = self.sysinfo_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for row, (key, value) in enumerate(self.system_information()):
            self.sysinfo_grid.addWidget(Placard(key), row, 0)
            cell = QLabel(str(value))
            cell.setFont(mono_font(9.5, QFont.Weight.Normal))
            cell.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            cell.setWordWrap(True)
            self.sysinfo_grid.addWidget(cell, row, 1)
        self.sysinfo_grid.setColumnStretch(1, 1)

    def diagnostic_summary(self) -> str:
        """The text the copy button produces.

        Built from the same rows the card shows, so it can never disagree with
        what the user is looking at — and it carries no credentials, tokens,
        personal data or filesystem paths, because none of those rows exist.
        """
        lines = [f"{PRODUCT} — {TAGLINE}", ""]
        for key, value in self.system_information():
            lines.append(f"{key + ':':<30}{value}")
        return "\n".join(lines)

    def copy_system_information(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.diagnostic_summary())
        QMessageBox.information(
            self, "Copied",
            "A diagnostic summary has been copied to the clipboard.\n\n"
            "It contains no passwords, keys, tokens, personal data or "
            "filesystem paths.")

    # ── 11. licences ──────────────────────────────────────────────────
    def _licences(self) -> QWidget:
        card, lay = self._card("Data sources and licences",
                               "attribution registry")
        lay.addWidget(self._para(
            "External data providers, third-party software licences and data "
            "acknowledgements are maintained in the application's licence "
            "registry, which is the single source used elsewhere in the "
            "product."))
        self.licence_body = QLabel("")
        self.licence_body.setWordWrap(True)
        self.licence_body.setFont(mono_font(9, QFont.Weight.Normal))
        self.licence_body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.licence_body.setVisible(False)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.licence_btn = QPushButton("Show licences and attributions")
        self.licence_btn.clicked.connect(self.toggle_licences)
        row.addWidget(self.licence_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(self.licence_body)
        return card

    def licence_text(self) -> str:
        """Read from `assets/LICENSES.md` — the registry the rest of the
        application already uses. A second hand-written list here would drift
        out of step with it."""
        path = config.ASSETS_DIR / "LICENSES.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ("The licence registry (assets/LICENSES.md) could not be "
                    "read from this installation.")

    def toggle_licences(self) -> None:
        showing = not self.licence_body.isVisible()
        if showing:
            self.licence_body.setText(self.licence_text())
        self.licence_body.setVisible(showing)
        self.licence_btn.setText("Hide licences and attributions" if showing
                                 else "Show licences and attributions")

    # ── 12. footer ────────────────────────────────────────────────────
    def _footer(self) -> QWidget:
        card, lay = self._card()
        line = QLabel(f"{PRODUCT} — {TAGLINE}")
        line.setFont(ui_font(10.5, QFont.Weight.DemiBold))
        lay.addWidget(line)
        self.copyright_line = QLabel("")
        self.copyright_line.setFont(ui_font(9.5))
        self.copyright_line.setObjectName("Faint")
        lay.addWidget(self.copyright_line)
        return card

    # ── lifecycle ─────────────────────────────────────────────────────
    def on_shown(self) -> None:
        version = maintenance.app_version() or UNAVAILABLE
        self.version_line.setText(f"Version {version}")
        # The year is read at display time; a hard-coded one is wrong from
        # the first of January.
        self.copyright_line.setText(
            f"© {date.today().year} · Created by {CREATOR_NAME}")
        self.refresh_system_information()

    def refresh_theme(self, theme: str) -> None:
        super().refresh_theme(theme)
        icon = config.ASSETS_DIR / "icons" / (
            f"mark-{'dark' if theme == 'dark' else 'light'}.svg")
        if icon.exists():
            self.mark_label.setPixmap(svg_pixmap(icon, 78))
