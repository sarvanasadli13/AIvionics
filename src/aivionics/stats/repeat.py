"""Phase 3.2 — repeat-defect detection, normalised.

The naive rule (same tail, same ATA chapter, inside the window) was measured
on the full corpus and is wrong by an order of magnitude: **54.7% of eligible
defects** get a <=30 day repeat, and **87.6% of the pairs are ATA 53**. A
structural inspection logs a dozen findings against one airframe in one
chapter on one day, and every pair inside that clique counts as a repeat. A
54.7% fleet-wide repeat rate is not a finding, it is an artefact.

The fix is the symptom. Two write-ups are the same defect recurring only if
they describe the same complaint, so the pair test is:

    same tail  x  same ATA chapter  x  symptom overlap >= threshold
                                    x  elapsed <= window

Normalisation has to survive how engineers actually type: ``CAPT AIRSPEED
UNRELIABLE`` and ``CAPTAIN AIRSPEED UNRELIABLE.`` are one symptom, so the
pipeline upper-cases, expands a curated abbreviation map, strips punctuation,
drops stopwords and compares token sets.

**The clique bound is a measurement decision, not an optimisation.** Comparing
every pair inside a group is quadratic, and the ATA 53 groups are exactly the
ones that blow up. Forward comparisons per defect are capped, and the number
of defects that hit the cap is returned so a run can say how much it truncated
rather than quietly under-counting.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from . import schema

# ── normalisation ───────────────────────────────────────────────────────

# Curated, not generated. Every entry is an abbreviation seen in SDR
# narratives; a generic stemmer would collapse distinctions that matter
# (RESET vs RESEAT) while missing these entirely.
ABBREVIATIONS: dict[str, str] = {
    "CAPT": "CAPTAIN", "CPT": "CAPTAIN", "CA": "CAPTAIN",
    "FO": "FIRSTOFFICER", "F/O": "FIRSTOFFICER", "COPILOT": "FIRSTOFFICER",
    "LH": "LEFT", "L/H": "LEFT", "RH": "RIGHT", "R/H": "RIGHT",
    "FWD": "FORWARD", "INBD": "INBOARD", "OUTBD": "OUTBOARD",
    "ACFT": "AIRCRAFT", "A/C": "AIRCRAFT", "AC": "AIRCRAFT",
    "ENG": "ENGINE", "ENGS": "ENGINE", "APU": "AUXILIARYPOWERUNIT",
    "HYD": "HYDRAULIC", "ELEC": "ELECTRICAL", "PNEU": "PNEUMATIC",
    "PRESS": "PRESSURE", "TEMP": "TEMPERATURE", "QTY": "QUANTITY",
    "IND": "INDICATION", "INDIC": "INDICATION", "INDICATOR": "INDICATION",
    "INOP": "INOPERATIVE", "U/S": "UNSERVICEABLE", "UNSVC": "UNSERVICEABLE",
    "SYS": "SYSTEM", "GEN": "GENERATOR", "XFER": "TRANSFER",
    "XMTR": "TRANSMITTER", "RCVR": "RECEIVER", "ANT": "ANTENNA",
    "T/O": "TAKEOFF", "TO": "TAKEOFF", "LDG": "LANDING", "GND": "GROUND",
    "MSG": "MESSAGE", "MSGS": "MESSAGE", "LT": "LIGHT", "LTS": "LIGHT",
    "DISAG": "DISAGREE", "DISAGREED": "DISAGREE",
    "WARN": "WARNING", "CAUT": "CAUTION", "FLT": "FLIGHT",
    "ALT": "ALTITUDE", "SPD": "SPEED", "AIRSPD": "AIRSPEED",
    "CKPT": "COCKPIT", "FLTDK": "FLIGHTDECK", "PAX": "PASSENGER",
    "CB": "CIRCUITBREAKER", "C/B": "CIRCUITBREAKER",
    "MLG": "MAINLANDINGGEAR", "NLG": "NOSELANDINGGEAR", "LG": "LANDINGGEAR",
    "STA": "STATION", "STGR": "STRINGER", "FS": "STATION",
    "CORR": "CORROSION", "CORRODED": "CORROSION",
    "CRACKED": "CRACK", "CRACKS": "CRACK", "LEAKING": "LEAK", "LEAKS": "LEAK",
    "FAULTS": "FAULT", "FAULTED": "FAULT", "FAILED": "FAILURE",
    "FAILS": "FAILURE", "FAILING": "FAILURE",
}

# Negations are deliberately absent from this list: dropping NO / NOT turns
# "NO FAULT INDICATION" into "FAULT INDICATION".
STOPWORDS: frozenset[str] = frozenset("""
A AN THE AND OR OF ON IN AT TO FOR FROM WITH WITHOUT BY AS IS WAS WERE ARE BE
BEEN BEING HAS HAD HAVE THIS THAT THESE THOSE IT ITS THEY THEM THEIR WE OUR
DURING AFTER BEFORE WHILE WHEN THEN THAN THERE HERE ALSO ANY ALL SOME EACH
CREW PILOT REPORTED REPORTS REPORT WRITEUP WRITE UP NOTED NOTE PIREP PER IAW
REF REFERENCE USING AGAIN ONCE STILL ABOUT INTO OVER UNDER UPON VERY MORE MOST
DUE APPROX APPROXIMATELY ADVISED FOUND SHOWS SHOWED SHOWING INDICATES
""".split())

_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9/&'-]*")
_PURE_DIGITS = re.compile(r"^\d+$")
MIN_TOKENS = 2
DEFAULT_THRESHOLD = 0.5
DEFAULT_WINDOW_DAYS = 30
MAX_FORWARD_PAIRS = 50


def normalise_symptom(text: str | None) -> frozenset[str]:
    """Reduce a symptom narrative to the token set two write-ups are compared on.

    Upper-case, punctuation stripped, abbreviations expanded, stopwords and
    bare numbers dropped. Bare numbers go because station numbers, flight
    numbers and dates are the noisiest tokens in SDR text and none of them
    identify a symptom.
    """
    if not text:
        return frozenset()
    out: set[str] = set()
    for raw in _TOKEN.findall(text.upper()):
        token = ABBREVIATIONS.get(raw, raw).replace("/", "").replace("-", "")
        if not token or token in STOPWORDS or _PURE_DIGITS.match(token):
            continue
        token = ABBREVIATIONS.get(token, token)
        if len(token) < 2 or token in STOPWORDS:
            continue
        out.add(token)
    return frozenset(out)


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard overlap of two normalised symptoms, 0.0 when either is too thin.

    Jaccard rather than containment: containment scores a two-word write-up
    against a paragraph as a perfect match, which is how the ATA 53 clique
    would come straight back in through the similarity test.
    """
    if len(a) < MIN_TOKENS or len(b) < MIN_TOKENS:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


# ── linkage ─────────────────────────────────────────────────────────────

@dataclass
class RepeatRun:
    """What one linkage pass did, including what it declined to do."""

    pairs: int = 0
    defects_with_repeat: int = 0
    eligible: int = 0
    groups: int = 0
    capped_defects: int = 0
    window_days: int = DEFAULT_WINDOW_DAYS
    threshold: float = DEFAULT_THRESHOLD
    per_chapter: dict[str, int] = field(default_factory=dict)

    @property
    def rate(self) -> float:
        """Fraction of eligible defects with a repeat. Diagnostics only.

        Not a reportable figure — anything shown to a user goes through
        ``metrics.Rate`` so it cannot be rendered without its n.
        """
        return self.defects_with_repeat / self.eligible if self.eligible else 0.0


_SELECT = """
SELECT d.id, d.aircraft_tail, d.ata_ref, d.reported_at, d.defect_text,
       a.action_type
  FROM defect d
  LEFT JOIN defect_action a ON a.defect_id = d.id
 WHERE d.aircraft_tail IS NOT NULL AND d.aircraft_tail <> ''
   AND d.ata_ref IS NOT NULL AND d.reported_at IS NOT NULL
 ORDER BY d.aircraft_tail, d.ata_ref, d.reported_at, d.id
"""
_INSERT = ("INSERT OR REPLACE INTO repeat_norm(defect_id,repeat_defect_id,"
           "days_apart,similarity,same_action,ata_chapter) VALUES(?,?,?,?,?,?)")


def build(con: sqlite3.Connection, *, window_days: int = DEFAULT_WINDOW_DAYS,
          threshold: float = DEFAULT_THRESHOLD,
          max_forward: int = MAX_FORWARD_PAIRS,
          rebuild: bool = True, progress=None) -> RepeatRun:
    """Detect normalised repeats and write them to ``repeat_norm``.

    One ordered pass over the defect table, grouped by (tail, ATA chapter).
    The full 1.75 M-row table is streamed rather than loaded — the naive
    ``fetchall`` form is several GB.
    """
    schema.ensure(con)
    if rebuild:
        con.execute("DELETE FROM repeat_norm")
        con.commit()

    run = RepeatRun(window_days=window_days, threshold=threshold)
    cursor = con.execute(_SELECT)
    group: list[tuple] = []
    key: tuple | None = None
    seen: set[int] = set()
    pending: list[tuple] = []

    while True:
        chunk = cursor.fetchmany(20_000)
        if not chunk:
            break
        for did, tail, chapter, at, text, action in chunk:
            if did in seen:            # a defect with two action rows
                continue
            seen.add(did)
            row_key = (tail, chapter)
            if key is not None and row_key != key:
                _flush(group, run, pending, window_days, threshold, max_forward)
                group = []
            key = row_key
            group.append((did, chapter, _ordinal(at), text, action))
            if len(pending) >= 20_000:
                _write(con, pending, run, progress)
    _flush(group, run, pending, window_days, threshold, max_forward)
    _write(con, pending, run, progress)
    con.commit()
    run.defects_with_repeat = con.execute(
        "SELECT COUNT(DISTINCT defect_id) FROM repeat_norm").fetchone()[0]
    return run


def _write(con: sqlite3.Connection, pending: list[tuple], run: RepeatRun,
           progress) -> None:
    if not pending:
        return
    con.executemany(_INSERT, pending)
    con.commit()
    run.pairs += len(pending)
    pending.clear()
    if progress is not None:
        progress(run)


def _flush(group: list[tuple], run: RepeatRun, pending: list[tuple],
           window_days: int, threshold: float, max_forward: int) -> None:
    """Compare one (tail, chapter) group and queue the pairs that survive."""
    if not group:
        return
    run.groups += 1
    run.eligible += len(group)
    symptoms = [normalise_symptom(text) for _, _, _, text, _ in group]
    for i, (did, chapter, day, _, action) in enumerate(group):
        if day is None:
            continue
        compared = 0
        for j in range(i + 1, len(group)):
            other_id, _, other_day, _, other_action = group[j]
            if other_day is None:
                continue
            gap = other_day - day
            if gap > window_days:
                break                      # group is date-ordered
            if compared >= max_forward:
                run.capped_defects += 1
                break
            compared += 1
            score = similarity(symptoms[i], symptoms[j])
            if score < threshold:
                continue
            pending.append((did, other_id, gap, round(score, 4),
                            int(bool(action) and action == other_action), chapter))
            run.per_chapter[chapter] = run.per_chapter.get(chapter, 0) + 1


def _ordinal(value: str | None) -> int | None:
    """ISO date -> day number. Anything unparseable is dropped, not guessed."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).toordinal()
    except ValueError:
        return None


# ── reporting ───────────────────────────────────────────────────────────

def chapter_counts(con: sqlite3.Connection, *,
                   window_days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    """Per-chapter eligible / repeated counts, ordered by repeat count.

    Counts only. The rate is built by ``metrics`` so that it cannot be
    rendered without its supporting n.
    """
    eligible = dict(con.execute("""
        SELECT ata_ref, COUNT(*) FROM defect
         WHERE aircraft_tail IS NOT NULL AND aircraft_tail <> ''
           AND ata_ref IS NOT NULL AND reported_at IS NOT NULL
         GROUP BY ata_ref"""))
    repeated = dict(con.execute("""
        SELECT ata_chapter, COUNT(DISTINCT defect_id) FROM repeat_norm
         WHERE days_apart <= ? GROUP BY ata_chapter""", (window_days,)))
    rows = [{"chapter": ch, "eligible": n, "repeated": repeated.get(ch, 0)}
            for ch, n in eligible.items()]
    rows.sort(key=lambda r: (-r["repeated"], r["chapter"]))
    return rows
