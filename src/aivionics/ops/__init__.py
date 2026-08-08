"""Fleet operations — compliance, import, airports, weather, live traffic.

PLAN Phase 4B. Everything in this package is either **offline data the
application already ships** (the OurAirports CSVs) or **online data behind a
single `online_enabled` setting** (standing rule 12). The manuals core,
retrieval, case base and statistics import nothing from here, so the offline
half of the product is provably untouched by any of it.

Submodules are imported lazily by their callers rather than re-exported
here: `airports` builds a 12.7 MB index and `weather`/`adsb` reach for the
network client, and neither cost should be paid by `import aivionics.ops`.
"""
from __future__ import annotations

__all__ = ["adsb", "airports", "compliance", "importer", "net", "weather"]
