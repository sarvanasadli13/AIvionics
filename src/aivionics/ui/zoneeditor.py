"""Editing the world-clock strip (BACKLOG round 2, R7).

The strip used to be six hardcoded cities. Which six was a guess made once,
in a file, by somebody who is not the person reading the clock — so it is now
a list the operator owns.

Nothing here goes near the network. Zones come from two places that are
already on this machine: the IANA database `zoneinfo` reads, and the bundled
OurAirports positions resolved through `timezonefinder`. Searching "Baku"
finds the city through the airport index; searching "Asia/" finds the zone
directly. Both end in the same thing — an IANA name, never a fixed offset,
so the tz database keeps handling DST rather than us (PLAN 4.8).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout, QWidget)

from . import fonts
from . import theme as T
from .widgets import Placard, ui_font

MAX_ZONES = 10
MAX_MATCHES = 60


def default_label(zone: str) -> str:
    """`Europe/Berlin` -> `BERLIN`. A last resort — a code is better."""
    tail = zone.replace("_", " ").split("/")[-1]
    return tail.upper()[:8]


def search_zones(query: str, limit: int = MAX_MATCHES) -> list[tuple[str, str]]:
    """(zone, what matched) for a free-text query, offline.

    Airports are searched first because "Baku" and "Frankfurt" are how people
    think about this, and an airport carries a three-letter code that makes a
    far better strip label than the tail of a zone name.
    """
    text = (query or "").strip().lower()
    if len(text) < 2:
        return []

    seen: set[str] = set()
    found: list[tuple[str, str]] = []

    try:
        from ..ops import airports as apt
        index = apt.index()
        for airport in index.search(query, limit=40):
            zone = apt.timezone_for(airport)
            if not zone or zone in seen:
                continue
            seen.add(zone)
            label = airport.iata or airport.ident
            where = airport.where()
            found.append((zone, f"{label} · {airport.name}"
                                + (f" · {where}" if where else "")))
    except Exception:
        pass        # the index is optional here; zone names still work

    for zone in sorted(available_timezones()):
        if len(found) >= limit:
            break
        if text in zone.lower().replace("_", " ") and zone not in seen:
            seen.add(zone)
            found.append((zone, zone))
    return found[:limit]


class ZoneEditor(QDialog):
    """Add, remove and reorder the cities on the world-clock strip."""

    def __init__(self, zones: list[tuple[str, str]], theme: str = T.DEFAULT_THEME,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("World time — choose the cities")
        self.setStyleSheet(fonts.qss(theme))
        self.setMinimumSize(680, 460)
        self._theme = theme
        self._matches: list[tuple[str, str]] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(11)

        head = QLabel("World time")
        head.setFont(ui_font(12, QFont.Weight.DemiBold))
        lay.addWidget(head)
        note = QLabel(
            "Search a city, an airport or an IANA zone name. Everything here "
            "is resolved on this machine — bundled airport positions and "
            "the tz database. Nothing is fetched.")
        note.setObjectName("Muted")
        note.setFont(ui_font(9))
        note.setWordWrap(True)
        lay.addWidget(note)

        columns = QHBoxLayout()
        columns.setSpacing(14)

        # ── left: what is on the strip now ────────────────────────────
        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(Placard("On the strip"))
        self.chosen = QListWidget()
        self.chosen.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.chosen.currentRowChanged.connect(self._sync)
        left.addWidget(self.chosen, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.up = QPushButton("Move up")
        self.up.clicked.connect(lambda: self._move(-1))
        self.down = QPushButton("Move down")
        self.down.clicked.connect(lambda: self._move(1))
        self.remove = QPushButton("Remove")
        self.remove.clicked.connect(self._remove)
        for button in (self.up, self.down, self.remove):
            buttons.addWidget(button)
        buttons.addStretch(1)
        left.addLayout(buttons)
        columns.addLayout(left, 1)

        # ── right: find one to add ────────────────────────────────────
        right = QVBoxLayout()
        right.setSpacing(6)
        right.addWidget(Placard("Add a city"))
        self.query = QLineEdit()
        self.query.setPlaceholderText(
            "e.g. Baku, Frankfurt, EDDF, Asia/Tokyo, UTC")
        self.query.setMinimumHeight(30)
        self.query.textChanged.connect(self._search)
        right.addWidget(self.query)
        self.results = QListWidget()
        self.results.itemDoubleClicked.connect(lambda _: self._add())
        self.results.currentRowChanged.connect(self._sync)
        right.addWidget(self.results, 1)

        label_row = QHBoxLayout()
        label_row.setSpacing(6)
        label_row.addWidget(QLabel("Label"))
        self.label = QLineEdit()
        self.label.setMaxLength(8)
        self.label.setPlaceholderText("shown on the strip, e.g. GYD")
        label_row.addWidget(self.label, 1)
        self.add = QPushButton("Add")
        self.add.setObjectName("Primary")
        self.add.clicked.connect(self._add)
        label_row.addWidget(self.add)
        right.addLayout(label_row)
        columns.addLayout(right, 1)
        lay.addLayout(columns, 1)

        self.count = QLabel("")
        self.count.setObjectName("Muted")
        self.count.setFont(ui_font(9))
        lay.addWidget(self.count)

        footer = QHBoxLayout()
        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(self._reset)
        footer.addWidget(reset)
        footer.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        footer.addWidget(cancel)
        footer.addWidget(save)
        lay.addLayout(footer)

        self._load(zones)

    # ── state ─────────────────────────────────────────────────────────
    def zones(self) -> list[tuple[str, str]]:
        return [self.chosen.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.chosen.count())]

    def _load(self, zones: list[tuple[str, str]]) -> None:
        self.chosen.clear()
        for label, zone in zones:
            self._append(label, zone)
        self._sync()

    def _append(self, label: str, zone: str) -> None:
        try:
            now = datetime.now(ZoneInfo(zone)).strftime("%H:%M")
        except Exception:
            now = "--:--"
        item = QListWidgetItem(f"{label}   ·   {zone}   ·   {now}")
        item.setData(Qt.ItemDataRole.UserRole, (label, zone))
        self.chosen.addItem(item)

    def _reset(self) -> None:
        from .widgets import WORLD_CLOCK_ZONES
        self._load(list(WORLD_CLOCK_ZONES))

    def _search(self, text: str) -> None:
        self.results.clear()
        self._matches = search_zones(text)
        for zone, description in self._matches:
            item = QListWidgetItem(f"{description}\n{zone}")
            self.results.addItem(item)
        if self._matches and not self.label.text().strip():
            self._suggest_label(0)
        self._sync()

    def _suggest_label(self, row: int) -> None:
        if not 0 <= row < len(self._matches):
            return
        zone, description = self._matches[row]
        code = description.split(" · ")[0].strip()
        self.label.setText(code[:8].upper() if len(code) <= 8
                           else default_label(zone))

    def _add(self) -> None:
        row = self.results.currentRow()
        if row < 0 and self._matches:
            row = 0
        if not 0 <= row < len(self._matches):
            return
        if self.chosen.count() >= MAX_ZONES:
            return
        zone, _ = self._matches[row]
        label = (self.label.text().strip().upper()
                 or default_label(zone))[:8]
        if any(existing == zone for _, existing in self.zones()):
            return
        self._append(label, zone)
        self.label.clear()
        self.query.clear()
        self.results.clear()
        self._matches = []
        self._sync()

    def _remove(self) -> None:
        row = self.chosen.currentRow()
        if row >= 0:
            self.chosen.takeItem(row)
            self._sync()

    def _move(self, delta: int) -> None:
        row = self.chosen.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.chosen.count():
            return
        item = self.chosen.takeItem(row)
        self.chosen.insertItem(target, item)
        self.chosen.setCurrentRow(target)
        self._sync()

    def _sync(self, *_) -> None:
        row = self.chosen.currentRow()
        count = self.chosen.count()
        self.up.setEnabled(row > 0)
        self.down.setEnabled(0 <= row < count - 1)
        self.remove.setEnabled(row >= 0 and count > 1)
        self.add.setEnabled(bool(self._matches) and count < MAX_ZONES)
        if self.results.currentRow() >= 0:
            self._suggest_label(self.results.currentRow())
        self.count.setText(
            f"{count} of {MAX_ZONES} — at least one, and the strip stops "
            f"fitting much past that." if count < MAX_ZONES
            else f"{MAX_ZONES} of {MAX_ZONES} — remove one to add another.")
