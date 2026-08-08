"""Page widgets for the shell's QStackedWidget."""
from .diagnose import DiagnosePage
from .fleet import FleetPage
from .home import HomePage
from .manuals import ManualsPage
from .reliability import ReliabilityPage
from .compliance import CompliancePage
from .stubs import AdminPage, OpsPage

__all__ = ["HomePage", "DiagnosePage", "ManualsPage", "FleetPage",
           "ReliabilityPage", "CompliancePage", "OpsPage", "AdminPage"]
