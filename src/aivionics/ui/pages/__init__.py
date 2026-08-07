"""Page widgets for the shell's QStackedWidget."""
from .diagnose import DiagnosePage
from .home import HomePage
from .manuals import ManualsPage
from .stubs import (AdminPage, CompliancePage, FleetPage, OpsPage,
                    ReliabilityPage)

__all__ = ["HomePage", "DiagnosePage", "ManualsPage", "FleetPage",
           "ReliabilityPage", "CompliancePage", "OpsPage", "AdminPage"]
