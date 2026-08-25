"""Can this machine reach a network right now? (BACKLOG item 5)

The title-bar badge used to report `online_enabled` — a *permission*, set once
in Admin. Read as a status, which is how everybody reads a word like OFFLINE,
it was simply wrong: an application that had never been unplugged still said
OFFLINE, and pulling the cable out of the wall changed nothing on screen.

Reachability here comes from `QNetworkInformation`, which on Windows is a thin
wrapper over the OS Network List Manager. That choice matters more than
convenience: it reports what the machine already knows and **sends nothing**.
A polling probe would have been an outbound call made on behalf of a status
light, on a product whose entire audit story is two allow-listed hosts and a
switch that turns them off (standing rule 12).

Everything degrades quietly. If the backend will not load, `supported` is
False and `is_reachable()` answers True — an unknown state is not evidence of
a disconnection, and badging OFFLINE on a guess is the failure this replaces.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class Reachability(QObject):
    """The OS's own view of whether there is a route off this machine.

    `changed` carries the new answer, so a cable pulled out and pushed back in
    moves the badge without anything being polled or fetched.
    """

    changed = Signal(bool)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._info = None
        self._reachable_states: tuple = ()
        try:
            from PySide6.QtNetwork import QNetworkInformation
            if QNetworkInformation.loadDefaultBackend():
                info = QNetworkInformation.instance()
                if info is not None and info.supports(
                        QNetworkInformation.Feature.Reachability):
                    # `Local` and `Site` mean a network was found but the
                    # internet was not, which is exactly the case this badge
                    # has to get right — neither counts as reachable.
                    self._reachable_states = (
                        QNetworkInformation.Reachability.Online,
                        QNetworkInformation.Reachability.Unknown,
                    )
                    self._info = info
                    info.reachabilityChanged.connect(self._on_reachability)
        except Exception:
            self._info = None

    @property
    def supported(self) -> bool:
        """False when the platform gave us no way to answer the question."""
        return self._info is not None

    def is_reachable(self) -> bool:
        if self._info is None:
            return True
        try:
            return self._info.reachability() in self._reachable_states
        except Exception:
            return True

    def backend_name(self) -> str:
        if self._info is None:
            return "none"
        try:
            return str(self._info.backendName())
        except Exception:
            return "unknown"

    def _on_reachability(self, _state=None) -> None:
        self.changed.emit(self.is_reachable())
