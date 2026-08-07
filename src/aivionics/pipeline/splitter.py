"""Phase 0.3 — narrative splitter: defect text vs rectification text.

The SDR Discrepancy field contains the defect AND the rectification (with the
cited task number) in one field — Fable's label-leakage finding. A query built
from the full narrative contains its own answer. The split point is the first
action-verb boundary; text before it is the leak-free query.

Measured on SDR-2025 (2026-08-06): 76.3% of citing narratives have a boundary;
citation-before-boundary = unsplittable -> leak_free=0.
"""
from __future__ import annotations

import re

# action verbs that begin the rectification clause in SDR narratives
BOUNDARY = re.compile(
    r"\b(REMOVED\s+AND\s+REPLACED|REPLACED|REMOVED|R&R'?D?|SWAPPED|"
    r"ACCOMPLISHED|PERFORMED|COMPLIED\s+WITH|C/W|CARRIED\s+OUT|"
    r"ADJUSTED|REPAIRED|RESET|CLEANED|RE-?RACKED|RESEATED|RE-?SEATED|"
    r"TIGHTENED|SECURED|INSTALLED|CORRECTED|RECTIFIED|TROUBLESHOT|"
    r"MAINTENANCE\s+(?:REPLACED|PERFORMED|ACCOMPLISHED|TIGHTENED))\b",
    re.I,
)
# a manual citation (task number or 'PER AMM/FIM/IAW')
CITATION = re.compile(
    r"\b(?:PER|IAW|REF(?:ERENCE)?|USING)\s+(?:AMM|FIM|IFIM|TSM|SRM|CMM|MM)\b"
    r"|\b[A-Z]?\d{2}-\d{2}-\d{2}-\d{3}-[A-Z]?\d{2}[0-9A-Z]\b",
    re.I,
)

MIN_QUERY_CHARS = 25


def split(narrative: str) -> tuple[str, str | None, bool]:
    """Return (defect_text, rectification_text, leak_free)."""
    if not narrative:
        return "", None, False
    m = BOUNDARY.search(narrative)
    if not m:
        return narrative.strip(), None, False        # nothing to split
    defect = narrative[: m.start()].strip(" .;,-")
    rect = narrative[m.start():].strip()
    if CITATION.search(defect):
        # citation before any action verb -> query still contains the answer
        return defect, rect, False
    if len(defect) < MIN_QUERY_CHARS:
        return defect, rect, False                    # too short to be a query
    return defect, rect, True
