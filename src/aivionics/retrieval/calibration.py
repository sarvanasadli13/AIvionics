"""Per-ATA-chapter abstention thresholds (PLAN 2.7).

WHY THIS EXISTS

The Gate 2 run measured a confident-and-wrong rate of 0.94: almost every query
returned a top-1 that was wrong and scored above the abstention threshold. That
was not a badly chosen threshold, it was a structural defect. `search._max_norm`
divides every score by the maximum, so the best hit always scores exactly 1.0
and the merged top-1 is always at least the dense weight (0.85 on free text).
**No absolute threshold on that score can ever fire.** The system could not
decline to answer.

A confidence signal therefore has to be one that survives normalisation:

* `raw_dense_top` — the un-normalised cosine of the best dense hit. Absolute
  and comparable across queries: a query whose best match sits at 0.42 cosine
  is in genuinely different territory from one at 0.78.
* `margin` — the gap between the first and second merged score. A flat
  distribution means nothing stood out, however high the normalised top is.

A single global threshold is simultaneously too loose in one chapter and too
tight in another (PLAN 2.7), so thresholds are fitted per ATA chapter, on the
**train** split only. Fitting on test would tune against the numbers Gate 2
reports.

WHAT THIS DOES NOT DO

It does not make retrieval better. It converts a wrong confident answer into
an honest "no confident match", which in a maintenance tool is the difference
between a defect and a limitation. Coverage lost to abstention is reported
alongside, because an abstention rate of 1.0 would look excellent by every
other measure here and be worthless.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

# Below this support a chapter cannot be fitted and inherits the global value.
MIN_SUPPORT = 30
# Target precision among answered queries. Deliberately modest: the point is to
# suppress the confidently-wrong tail, not to promise accuracy the engine does
# not have.
TARGET_PRECISION = 0.50
# If even the strictest threshold cannot reach this, the chapter is marked
# always-abstain rather than pretending.
FLOOR_PRECISION = 0.25

SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration(
    index_version TEXT NOT NULL,
    ata_chapter   TEXT NOT NULL,     -- '' is the global fallback row
    feature       TEXT NOT NULL,     -- raw_dense_top | margin
    threshold     REAL NOT NULL,
    n             INTEGER NOT NULL,
    precision     REAL NOT NULL,
    coverage      REAL NOT NULL,
    always_abstain INTEGER NOT NULL DEFAULT 0,
    fitted_at     TEXT NOT NULL,
    PRIMARY KEY(index_version, ata_chapter, feature)
);
"""


@dataclass(frozen=True)
class Threshold:
    chapter: str
    feature: str
    threshold: float
    n: int
    precision: float
    coverage: float
    always_abstain: bool = False


def _best_threshold(samples: list[tuple[float, bool]],
                    target: float = TARGET_PRECISION,
                    floor: float = FLOOR_PRECISION) -> Threshold:
    """Lowest cut-off reaching `target` precision — i.e. the most coverage that
    still meets the bar. Falls back to the most precise cut-off available, and
    to always-abstain when even that is below `floor`."""
    n = len(samples)
    if not n:
        return Threshold("", "", 0.0, 0, 0.0, 0.0, always_abstain=True)

    ordered = sorted(samples, key=lambda s: s[0], reverse=True)
    best: Threshold | None = None
    correct = 0
    # Walk down the score order; at each point the answered set is the prefix.
    for i, (score, ok) in enumerate(ordered, start=1):
        correct += bool(ok)
        precision = correct / i
        coverage = i / n
        cand = Threshold("", "", score, n, precision, coverage)
        if precision >= target:
            best = cand                     # keep going: more coverage, same bar
        elif best is None and (cand.precision > (0.0 if best is None else best.precision)):
            pass
    if best is not None:
        return best

    # Target unreachable — take the most precise prefix with at least 10 answers
    # so a single lucky hit at the top cannot define the threshold.
    fallback: Threshold | None = None
    correct = 0
    for i, (score, ok) in enumerate(ordered, start=1):
        correct += bool(ok)
        if i < 10:
            continue
        cand = Threshold("", "", score, n, correct / i, i / n)
        if fallback is None or cand.precision > fallback.precision:
            fallback = cand
    if fallback is None or fallback.precision < floor:
        top = ordered[0][0]
        return Threshold("", "", top + 1.0, n, 0.0, 0.0, always_abstain=True)
    return fallback


def fit(samples_by_chapter: dict[str, list[tuple[float, bool]]],
        feature: str = "raw_dense_top",
        target: float = TARGET_PRECISION) -> list[Threshold]:
    """Fit one threshold per chapter plus a global fallback row (chapter '')."""
    out: list[Threshold] = []
    pooled: list[tuple[float, bool]] = []
    for chapter, samples in samples_by_chapter.items():
        pooled.extend(samples)
        if len(samples) < MIN_SUPPORT:
            continue                      # too thin to fit; inherits the global
        t = _best_threshold(samples, target)
        out.append(Threshold(chapter, feature, t.threshold, t.n, t.precision,
                             t.coverage, t.always_abstain))
    g = _best_threshold(pooled, target)
    out.append(Threshold("", feature, g.threshold, g.n, g.precision,
                         g.coverage, g.always_abstain))
    return out


def save(con: sqlite3.Connection, index_version: str,
         thresholds: list[Threshold]) -> None:
    con.executescript(SCHEMA)
    now = datetime.now(timezone.utc).isoformat()
    con.execute("DELETE FROM calibration WHERE index_version=?", (index_version,))
    con.executemany(
        "INSERT INTO calibration(index_version,ata_chapter,feature,threshold,"
        "n,precision,coverage,always_abstain,fitted_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        [(index_version, t.chapter, t.feature, t.threshold, t.n, t.precision,
          t.coverage, int(t.always_abstain), now) for t in thresholds])
    con.commit()


class Calibrator:
    """Applies fitted thresholds. Absent calibration means never abstain —
    an uncalibrated install must not silently suppress every answer."""

    def __init__(self, thresholds: dict[str, Threshold] | None = None,
                 feature: str = "raw_dense_top"):
        self.thresholds = thresholds or {}
        self.feature = feature

    @classmethod
    def load(cls, con: sqlite3.Connection, index_version: str,
             feature: str = "raw_dense_top") -> "Calibrator":
        try:
            rows = con.execute(
                "SELECT ata_chapter,feature,threshold,n,precision,coverage,"
                "always_abstain FROM calibration"
                " WHERE index_version=? AND feature=?",
                (index_version, feature)).fetchall()
        except sqlite3.Error:
            return cls({}, feature)
        return cls({r[0]: Threshold(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]))
                    for r in rows}, feature)

    @property
    def fitted(self) -> bool:
        return bool(self.thresholds)

    def for_chapter(self, chapter: str | None) -> Threshold | None:
        key = (chapter or "").strip()[:2]
        return self.thresholds.get(key) or self.thresholds.get("")

    def should_abstain(self, chapter: str | None, confidence: dict) -> bool:
        """True when the run does not clear its chapter's bar."""
        t = self.for_chapter(chapter)
        if t is None:
            return False                  # uncalibrated: answer, do not suppress
        if t.always_abstain:
            return True
        return float(confidence.get(self.feature, 0.0)) < t.threshold

    def explain(self, chapter: str | None) -> str:
        t = self.for_chapter(chapter)
        if t is None:
            return "not calibrated — every result is shown"
        if t.always_abstain:
            return (f"ATA {t.chapter or 'all'}: no threshold reaches usable "
                    f"precision on {t.n} fitted queries — results suppressed")
        return (f"ATA {t.chapter or 'all'}: answers above {t.threshold:.3f} "
                f"{t.feature} · fitted precision {t.precision:.0%} at "
                f"{t.coverage:.0%} coverage on n={t.n}")
