"""Phase 3 — case base and statistics.

Three modules, in the order the data flows through them:

``casebase``  what was *done* (``defect_action``) and what was *found*
              (``defect_finding``), extracted from the rectification half of
              the split narrative.
``repeat``    normalised repeat-defect detection — same tail, same ATA
              chapter, *similar symptom*, inside an elapsed window. The naive
              tail x chapter product is measurably wrong (54.7% fleet-wide,
              87.6% of it ATA 53 structural inspections) and is not used.
``metrics``   the rate layer: Wilson intervals, small-n suppression, the
              provenance split, and the cross-standard hook for PLAN 3.7.

Two rules run through all three and are enforced in code rather than left to
reviewers:

* **Every rate carries its supporting n.** ``metrics.Rate`` is the only way to
  obtain one, and it holds ``n`` in the same object. Below the support
  threshold ``Rate.value`` is ``None`` — a bare percentage cannot be extracted.
* **`sdr_mined` and `operator_confirmed` are never pooled.** They answer
  different questions from different evidence, and averaging them would
  launder a proxy into a measurement.
"""
from __future__ import annotations

from . import casebase, metrics, repeat
from .metrics import (MIN_SUPPORT, OPERATOR_CONFIRMED, SDR_MINED, Rate,
                      wilson_interval)

__all__ = [
    "casebase", "repeat", "metrics",
    "Rate", "wilson_interval", "MIN_SUPPORT",
    "SDR_MINED", "OPERATOR_CONFIRMED",
]
