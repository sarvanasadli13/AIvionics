"""Phase 5 — probable-cause candidates, built from evidence rather than recall.

**`defect_closure` has 0 rows.** No maintenance outcome is recorded anywhere in
this database and there is no path to one without an operator customer, so the
one thing this module may never do is the obvious thing: ask a model what
usually causes a symptom. That answer would come from pretrained memory, it
would read exactly like a measurement, and nothing here could tell the
difference. Every candidate below is assembled from rows that exist — a
finding, an action, a repeat pair, a tail's own history — and carries the ids
it was assembled from.

What comes out is named in `CLAIM` and the name is load-bearing: these are
**evidence-supported hypotheses**, never learned root-cause probability. There
is no calibrated probability available because there is no outcome to
calibrate against, so none is emitted. `ScoredCandidate.score` is deliberately
*unbounded* — a heuristic sum of weighted counts that cannot be mistaken for a
0..1 confidence, because the moment a ranking scalar lands in 0..1 somebody
reads it as one.

Three stages, kept apart on purpose:

``generate_candidates``  evidence rows -> grouped hypotheses. No score.
``score_candidates``     hypotheses -> ranked, with every feature's own
                         contribution stored so a rank can be explained.
``explain_candidate``    a ranked hypothesis -> the sentences a model would
                         later phrase. Generated deterministically here so the
                         model's job is wording, never invention.

The separation is what lets the ranker change without touching the assistant
interface, and it is why each stage takes the previous stage's output as a
plain argument rather than reaching back into the database.

**Contradicting cases are first-class.** A prior case where the same component
was swapped and the write-up then said *"ops check good"*, or where the swap
was followed by a repeat inside the window, is evidence *against* the
hypothesis, and it is collected with the same care as evidence for it. A list
that only counts agreement is a list that agrees with itself.

Prose here is kept free of numerals on purpose. Standing rule 4 says every
numeral shown to an engineer comes from the database, and `AssistantAnswer`
runs a numeral check over `cause` and `limitations`; counts therefore live in
the structured features where the UI can render them from their source, and
the sentences stay qualitative.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field

from ..db import stats_guard
from ..llm.schemas import (CONFLICTING, INSUFFICIENT, LIMITED, MAX_RANK,
                           STRONG, CauseCandidate, evidence_id)
from . import metrics, repeat
from .casebase import CONFIRMED_FAULT, NO_FAULT_FOUND, REMOVAL_ACTIONS

# The sentence every caller must be able to quote. Kept as one constant so the
# API, the tests and any screen that renders a candidate cannot drift into
# claiming something stronger than the data supports.
CLAIM = ("evidence-supported hypotheses drawn from prior cases — not a learned "
         "root-cause probability")

# Why the score is not a probability, in the object rather than in a comment
# somebody has to find.
SCORE_MEANING = ("an unbounded heuristic ranking total; the weights are a "
                 "judgement, not a fit to outcomes, because no outcome data "
                 "exists to fit them to")

# Which source put a case in front of us. Carried per case so a candidate can
# say not just *that* evidence exists but which table it came from.
FROM_ACTION = "defect_action"
FROM_FINDING = "defect_finding"
FROM_REPEAT = "repeat_norm"
FROM_TAIL_HISTORY = "tail_history"
FROM_SIMILAR_CASE = "similar_case"
FROM_TASK = "task_link"
FROM_FAULT_CODE = "mmsg"

SOURCE = "FAA SDR mined case base (defect, defect_action, defect_finding, repeat_norm)"

# Bounds. The scan cap follows `repeat.MAX_FORWARD_PAIRS` in spirit: a limit
# that is *reported* rather than silently applied, because a caller that got
# the top slice of ATA 53 without being told would reason about it as the
# whole chapter. 666,261 defects sit in that one chapter.
MAX_SCAN = 2_000
MAX_CASE_IDS = 500
DEFAULT_LIMIT = 5

# A component named in only one prior case is `limited` evidence, which the
# vocabulary already has a word for, so it is kept rather than dropped. What
# is dropped is a candidate with nothing at all behind it — `CauseCandidate`
# refuses an empty `supporting_case_ids` and that refusal is correct.
STRONG_MIN_CASES = 3
STRONG_MIN_TAILS = 2

# Position words are stripped when grouping a component. A left and a right
# pitot probe are the same component class for the purpose of "what should I
# look at", and keeping them apart splits thin evidence thinner. The position
# itself survives in `provenance`, so nothing is lost.
POSITIONS = frozenset({
    "LEFT", "RIGHT", "FORWARD", "AFT", "UPPER", "LOWER", "INBOARD",
    "OUTBOARD", "CENTER", "CENTRE", "NUMBER", "NO", "SIDE",
})
# Words that name no component. "UNKNOWN" is in the corpus as a literal part
# name and is not a hypothesis about anything.
EMPTY_PART_NAMES = frozenset({"UNKNOWN", "UNK", "NONE", "N/A", "NA", "OTHER"})

# What a component is called when the reporter gave a part number and no
# usable name. Numeral-free by necessity — see `normalise_cause`.
UNNAMED_COMPONENT = "component identified by part number"


# ── evidence ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CaseEvidence:
    """One prior case, and which side of a hypothesis its rows put it on.

    Support and contradiction are decided here, in generation, because both
    are properties of rows rather than of the ranking: the write-up said no
    fault was found, or the swap was followed by a repeat. The ranker weighs
    the two counts; it does not get to re-decide which is which.
    """

    defect_id: int
    tail: str
    reported_at: str
    ata_chapter: str
    action_type: str
    part_name: str
    part_number: str
    task_number: str
    finding_type: str
    symptom_similarity: float
    repeated_within_window: bool
    generators: tuple[str, ...] = ()

    @property
    def evidence_id(self) -> str:
        return evidence_id("defect", self.defect_id)

    @property
    def is_removal(self) -> bool:
        return self.action_type in REMOVAL_ACTIONS

    @property
    def contradiction_reason(self) -> str:
        """Why this case argues against the hypothesis, or an empty string.

        Two reasons, and the second is the stronger one. A recorded
        `no_fault_found` is the write-up saying the component was serviceable.
        A removal followed by a repeat of the same defect is the maintenance
        record itself demonstrating that swapping the component did not hold —
        which is a harder fact than anything the narrative asserts.
        """
        if self.is_removal and self.repeated_within_window:
            return ("the component was replaced and the same defect recurred "
                    "inside the repeat window")
        if self.finding_type == NO_FAULT_FOUND:
            return ("the write-up recorded no fault found on this component "
                    "— narrative language, not a shop teardown verdict")
        return ""

    @property
    def contradicts(self) -> bool:
        return bool(self.contradiction_reason)


@dataclass(frozen=True)
class CauseRequest:
    """What is being asked, before any evidence has been gathered.

    `case_ids` lets a caller hand in cases retrieved elsewhere — Phase 4's
    search, say — instead of having this module choose them. That is the
    decoupled path and the preferred one: candidate generation should not own
    retrieval, and a module that reached into the retrieval engine would tie
    the ranker's fate to the index.
    """

    symptom: str
    ata_chapter: str = ""
    tail: str = ""
    fault_codes: tuple[str, ...] = ()
    case_ids: tuple[int, ...] = ()
    window_days: int = metrics.DEFAULT_WINDOW_DAYS
    limit: int = DEFAULT_LIMIT
    max_scan: int = MAX_SCAN


# ── stage 1 · candidate generation ──────────────────────────────────────

@dataclass(frozen=True)
class RawCandidate:
    """One grouped hypothesis, with no score attached to it yet.

    A component, the cases that implicate it, and the cases that argue against
    it. `cause` carries no numerals — see the module docstring.
    """

    cause_id: str
    cause: str
    normalized_key: str
    part_numbers: tuple[str, ...]
    generators: tuple[str, ...]
    supporting: tuple[CaseEvidence, ...]
    contradicting: tuple[CaseEvidence, ...]
    provenance: dict = field(default_factory=dict)

    @property
    def supporting_case_ids(self) -> tuple[int, ...]:
        return tuple(sorted({c.defect_id for c in self.supporting}))

    @property
    def contradicting_case_ids(self) -> tuple[int, ...]:
        return tuple(sorted({c.defect_id for c in self.contradicting}))

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(sorted({c.evidence_id
                             for c in self.supporting + self.contradicting}))

    @property
    def distinct_tails(self) -> int:
        """Airframes, not cases. Five write-ups on one tail are one story."""
        return len({c.tail for c in self.supporting if c.tail})

    @property
    def confirmed_findings(self) -> int:
        return sum(1 for c in self.supporting
                   if c.finding_type == CONFIRMED_FAULT)

    @property
    def records_any_finding(self) -> bool:
        return any(c.finding_type for c in self.supporting + self.contradicting)


def normalise_cause(part_name: str | None,
                    part_number: str | None) -> tuple[str, str]:
    """Group key and display phrase for a component. `("", "")` when neither.

    **The name groups, not the number, and that is a measurement rather than a
    preference.** `defect_action.part_name` is populated on 1,336,979 of
    1,336,997 rows; `part_number` on 710,202 — barely half. Grouping on the
    sparser column splits one component into a numbered group and an unnumbered
    one whenever a reporter left the field blank, which shows an engineer two
    identical rows and halves the evidence under each. Every part number seen
    in a group is still carried, in `provenance`, where it can be rendered from
    its own column.

    This is not the precedence `casebase.extract_action` applies, and the
    difference is deliberate: that function chooses which value to record for
    *one* case, where a field the reporter filled in genuinely beats a regex.
    This one chooses what to group *many* cases on, which is a different
    question with a different answer.

    The name path reuses `repeat.normalise_symptom` rather than growing a
    second normaliser. Phase 3.2's abbreviation map and stopword list were
    curated against these same narratives, and a Phase 5 that disagreed with
    Phase 3.2 about whether two strings are the same thing would be worse than
    one that is merely imperfect.
    """
    name = " ".join((part_name or "").split()).upper()
    tokens = sorted(t for t in repeat.normalise_symptom(name)
                    if t not in POSITIONS and t not in EMPTY_PART_NAMES)
    if tokens:
        return f"nm:{' '.join(tokens)}", " ".join(tokens)
    number = (part_number or "").strip().upper()
    if number and number not in EMPTY_PART_NAMES:
        # A number can still group a component the reporter did not name, but
        # it never becomes the display phrase: `cause` is prose the numeral
        # check reads, and a part number is numerals.
        return f"pn:{number}", UNNAMED_COMPONENT
    return "", ""


def cause_id_for(key: str) -> str:
    """A stable id for a normalised cause.

    Hashed rather than spelled out: part phrases in this corpus run to sixty
    characters of prose and an id has to be short, bounded and identical
    across runs and across databases for the same component.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"cause:{digest}"


def _run(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list:
    """Every query here goes through the guard, as in `metrics`.

    Standing rule 6 is the reason. Notes are evidence a human reads and never
    an input to an aggregate, and a Phase 5 query that joined the note table
    would turn a hypothesis list into per-engineer attribution.
    """
    stats_guard(sql)
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _fault_code_chapters(con: sqlite3.Connection,
                         codes: tuple[str, ...]) -> dict[str, dict]:
    """ATA chapter and task routing for maintenance message codes.

    **No defect in this database carries a fault code** — `defect.fault_code`
    is populated 0 times out of 1,754,410, and `fault_code_source` is NULL
    throughout. So a code cannot be matched against prior cases at all. What
    it can still do is name a chapter and the tasks `mmsg_task` routes it to,
    which is a real row rather than an inference, and that is all this returns.
    """
    out: dict[str, dict] = {}
    for code in codes:
        code = (code or "").strip()
        if not code:
            continue
        rows = _run(con, "SELECT ata_chapter FROM mmsg WHERE code = ?", (code,))
        if not rows:
            continue
        tasks = [r[0] for r in _run(
            con, "SELECT task_number FROM mmsg_task WHERE code = ? ORDER BY seq",
            (code,))]
        out[code] = {"ata_chapter": rows[0][0], "tasks": tasks}
    return out


def _scan_cases(con: sqlite3.Connection, request: CauseRequest,
                chapters: set[str]) -> tuple[list[tuple], bool]:
    """Prior cases worth examining, and whether the scan hit its cap.

    The prefilter is the ATA chapter, which is populated on every defect and
    indexed by `ix_defect_tail_ata`. Ordering is newest first: a 2010 case is
    weaker evidence about a current airframe than a 2024 one, so when the cap
    bites it should bite on the oldest rows.
    """
    if request.case_ids:
        ids = list(dict.fromkeys(request.case_ids))[:MAX_CASE_IDS]
        placeholders = ",".join("?" * len(ids))
        rows = _run(con, f"SELECT d.id, d.aircraft_tail, d.reported_at,"
                         f" d.ata_ref, d.defect_text FROM defect d"
                         f" WHERE d.id IN ({placeholders})", tuple(ids))
        return rows, len(request.case_ids) > len(ids)

    clauses, args = [], []
    if chapters:
        clauses.append(f"d.ata_ref IN ({','.join('?' * len(chapters))})")
        args.extend(sorted(chapters))
    if request.tail:
        clauses.append("d.aircraft_tail = ?")
        args.append(request.tail)
    if not clauses:
        # Neither a chapter nor a tail is a query over 1.75 M rows with no
        # handle on it. Refusing is better than returning the newest two
        # thousand defects and letting them look like an answer.
        return [], False
    cap = max(1, min(request.max_scan, MAX_SCAN))
    rows = _run(con, f"SELECT d.id, d.aircraft_tail, d.reported_at, d.ata_ref,"
                     f" d.defect_text FROM defect d"
                     f" WHERE {' AND '.join(clauses)}"
                     f" ORDER BY d.reported_at DESC LIMIT ?",
                tuple([*args, cap + 1]))
    return rows[:cap], len(rows) > cap


def generate_candidates(con: sqlite3.Connection,
                        request: CauseRequest) -> tuple[tuple[RawCandidate, ...],
                                                        dict]:
    """**Stage 1.** Build hypotheses from rows. Returns candidates and a census.

    The census is how a caller learns what the scan actually saw — cases
    examined, how many recorded a finding, whether the scan truncated. A
    candidate list with no census behind it cannot be argued with.
    """
    chapters = {c for c in (request.ata_chapter,) if c}
    codes = _fault_code_chapters(con, request.fault_codes)
    for detail in codes.values():
        if detail["ata_chapter"]:
            chapters.add(detail["ata_chapter"])

    rows, truncated = _scan_cases(con, request, chapters)
    census = {"cases_examined": len(rows), "cases_matched": 0,
              "cases_with_finding": 0, "truncated": truncated,
              "fault_codes_resolved": sorted(codes),
              "fault_codes_unmatched": sorted(
                  set(request.fault_codes) - set(codes))}
    if not rows:
        return (), census

    wanted = repeat.normalise_symptom(request.symptom)
    ids = [r[0] for r in rows]
    placeholders = ",".join("?" * len(ids))
    actions: dict[int, list[tuple]] = {}
    for did, atype, pname, pnum, task in _run(
            con, f"SELECT defect_id, action_type, part_name, part_number,"
                 f" task_number FROM defect_action"
                 f" WHERE defect_id IN ({placeholders})", tuple(ids)):
        actions.setdefault(did, []).append((atype, pname, pnum, task))
    findings = {did: kind for did, kind in _run(
        con, f"SELECT defect_id, finding_type FROM defect_finding"
             f" WHERE defect_id IN ({placeholders})", tuple(ids))}
    repeated = {did for (did,) in _run(
        con, f"SELECT DISTINCT defect_id FROM repeat_norm"
             f" WHERE defect_id IN ({placeholders}) AND days_apart <= ?",
        tuple([*ids, request.window_days]))}

    groups: dict[str, list[CaseEvidence]] = {}
    display: dict[str, str] = {}
    numbers: dict[str, set[str]] = {}
    for did, tail, at, chapter, text in rows:
        # Symptom similarity reuses Phase 3.2 exactly. When the caller handed
        # in its own case ids the similarity is informational only — those
        # cases were already chosen by something better than a token overlap.
        score = repeat.similarity(wanted, repeat.normalise_symptom(text))
        if not request.case_ids and wanted and score <= 0.0:
            continue
        census["cases_matched"] += 1
        census["cases_with_finding"] += int(did in findings)
        for atype, pname, pnum, task in actions.get(did, ()):
            key, phrase = normalise_cause(pname, pnum)
            if not key:
                continue
            display.setdefault(key, phrase)
            if pnum and pnum.strip():
                numbers.setdefault(key, set()).add(pnum.strip().upper())
            marks = [FROM_ACTION, FROM_SIMILAR_CASE]
            if did in findings:
                marks.append(FROM_FINDING)
            if did in repeated:
                marks.append(FROM_REPEAT)
            if request.tail and tail == request.tail:
                marks.append(FROM_TAIL_HISTORY)
            if task:
                marks.append(FROM_TASK)
            if codes:
                marks.append(FROM_FAULT_CODE)
            groups.setdefault(key, []).append(CaseEvidence(
                defect_id=did, tail=tail or "", reported_at=at or "",
                ata_chapter=chapter or "", action_type=atype or "",
                part_name=pname or "", part_number=pnum or "",
                task_number=task or "", finding_type=findings.get(did, ""),
                symptom_similarity=round(score, 4),
                repeated_within_window=did in repeated,
                generators=tuple(dict.fromkeys(marks))))

    candidates = []
    for key, cases in groups.items():
        supporting = tuple(c for c in cases if not c.contradicts)
        contradicting = tuple(c for c in cases if c.contradicts)
        if not supporting:
            # Every case on this component argues against it. That is a real
            # finding but it is not a hypothesis, and `CauseCandidate` refuses
            # an empty support list rather than let one through with a label.
            continue
        part_numbers = tuple(sorted(numbers.get(key, ())))
        candidates.append(RawCandidate(
            cause_id=cause_id_for(key), cause=display.get(key, ""),
            normalized_key=key, part_numbers=part_numbers,
            generators=tuple(dict.fromkeys(
                g for c in cases for g in c.generators)),
            supporting=supporting, contradicting=contradicting,
            provenance={"normalized_key": key,
                        "part_numbers": list(part_numbers),
                        "source": SOURCE,
                        "fault_code_routing": codes,
                        "generators": sorted({g for c in cases
                                              for g in c.generators})}))
    return tuple(candidates), census


# ── stage 2 · scoring ───────────────────────────────────────────────────

@dataclass(frozen=True)
class FeatureContribution:
    """One term of a score, with the measured quantity that produced it.

    `observed` is a count from the database and `contribution` is
    `observed * weight`. Both are kept so the arithmetic can be checked rather
    than trusted — a score whose parts cannot be recomputed is a score nobody
    can argue with, which is the shape "AI confidence" takes when it is
    written down honestly.
    """

    name: str
    observed: float
    weight: float
    detail: str

    @property
    def contribution(self) -> float:
        return round(self.observed * self.weight, 6)


# The weights, and why each one is where it is. None of these is fitted;
# there is no outcome column in this database to fit them against, and saying
# so is more useful than a number that implies otherwise.
WEIGHTS: dict[str, tuple[float, str]] = {
    "confirmed_findings": (
        2.0, "a case whose write-up recorded a condition actually found is the "
             "strongest evidence this corpus contains"),
    "independent_tails": (
        1.5, "cases spread across airframes are independent; several write-ups "
             "on one tail are one airframe's story told repeatedly"),
    "supporting_cases": (
        1.0, "each prior case where this component was acted on for a similar "
             "symptom"),
    "symptom_agreement": (
        1.0, "how closely the prior symptoms match, summed — a near-identical "
             "complaint is worth more than a chapter-level coincidence"),
    "tail_history": (
        1.0, "this airframe's own history with the component"),
    "task_linkage": (
        0.5, "a case carrying a manual task number is traceable to a procedure"),
    "ata_agreement": (
        0.5, "the case sits in the ATA chapter the complaint was routed to"),
    "no_fault_contradiction": (
        -2.0, "a case where the write-up recorded no fault found on this "
              "component argues against it"),
    "repeat_contradiction": (
        -2.5, "the component was replaced and the defect came back; the "
              "maintenance record itself shows the swap did not hold"),
}


@dataclass(frozen=True)
class ScoredCandidate:
    """A ranked hypothesis whose rank can be taken apart feature by feature."""

    candidate: RawCandidate
    features: tuple[FeatureContribution, ...]
    rank: int
    evidence_level: str
    limitations: tuple[str, ...]

    @property
    def score(self) -> float:
        """The sum of the contributions, and nothing else.

        Recomputed from the features rather than stored beside them, so the
        two cannot disagree.
        """
        return round(sum(f.contribution for f in self.features), 6)

    @property
    def score_meaning(self) -> str:
        return SCORE_MEANING


def _features(raw: RawCandidate, request: CauseRequest
              ) -> tuple[FeatureContribution, ...]:
    observed = {
        "confirmed_findings": float(raw.confirmed_findings),
        "independent_tails": float(max(0, raw.distinct_tails - 1)),
        "supporting_cases": float(len(raw.supporting_case_ids)),
        "symptom_agreement": round(
            sum(c.symptom_similarity for c in raw.supporting), 4),
        "tail_history": float(sum(
            1 for c in raw.supporting
            if request.tail and c.tail == request.tail)),
        "task_linkage": float(sum(1 for c in raw.supporting if c.task_number)),
        "ata_agreement": float(sum(
            1 for c in raw.supporting
            if request.ata_chapter and c.ata_chapter == request.ata_chapter)),
        # Both terms read the same branch `contradiction_reason` took, so the
        # feature a case is charged under is always the reason the engineer is
        # shown for it. Keying the repeat term on `repeated_within_window`
        # alone charged a case that merely recurred — nothing removed — under
        # a sentence stating the component had been replaced, which is the one
        # thing a feature-by-feature explanation may not do.
        "no_fault_contradiction": float(sum(
            1 for c in raw.contradicting
            if not (c.is_removal and c.repeated_within_window))),
        "repeat_contradiction": float(sum(
            1 for c in raw.contradicting
            if c.is_removal and c.repeated_within_window)),
    }
    return tuple(
        FeatureContribution(name, observed[name], WEIGHTS[name][0],
                            WEIGHTS[name][1])
        for name in WEIGHTS)


def _evidence_level(raw: RawCandidate) -> str:
    """One of the four Phase 2 levels. Never a fifth, never a percentage.

    The mapping is the vocabulary's own wording read literally:
    `strong` is "several independent prior cases agree and none contradict",
    so it needs both a case count and a spread of airframes *and* a clean
    contradiction list. Any contradiction at all makes the level
    `conflicting` — the prior cases genuinely do disagree, and averaging that
    away into a slightly lower "strong" is the failure this level exists for.
    """
    if raw.contradicting:
        return CONFLICTING
    if (len(raw.supporting_case_ids) >= STRONG_MIN_CASES
            and raw.distinct_tails >= STRONG_MIN_TAILS):
        return STRONG
    return LIMITED


def _limitations(raw: RawCandidate) -> tuple[str, ...]:
    """What this candidate cannot support, in numeral-free sentences."""
    out = ["no maintenance outcome is recorded in this database, so this "
           "candidate has not been confirmed or ruled out by any closure"]
    if not raw.records_any_finding:
        out.append("the prior cases record what was replaced, not what was "
                   "found — a swap is not a diagnosis")
    if raw.distinct_tails <= 1:
        out.append("every supporting case comes from a single airframe, so "
                   "they are not independent observations")
    if raw.contradicting:
        out.append("prior cases disagree; see the contradicting case ids")
    if any(c.finding_type == NO_FAULT_FOUND for c in raw.contradicting):
        out.append("no-fault-found here is narrative language from a "
                   "write-up, not a shop teardown verdict")
    return tuple(out)


def score_candidates(raw_candidates: tuple[RawCandidate, ...],
                     request: CauseRequest) -> tuple[ScoredCandidate, ...]:
    """**Stage 2.** Rank hypotheses, keeping every term of every score.

    Takes stage 1's output as a plain argument and touches no database. That
    is what makes the ranker replaceable: a different weighting is a different
    function with the same signature, and nothing upstream or downstream has
    to know.
    """
    scored = []
    for raw in raw_candidates:
        features = _features(raw, request)
        scored.append(ScoredCandidate(
            candidate=raw, features=features, rank=0,
            evidence_level=_evidence_level(raw), limitations=_limitations(raw)))
    # Ties break on the id so the same evidence always produces the same order.
    scored.sort(key=lambda s: (-s.score, s.candidate.cause_id))
    cap = max(1, min(request.limit, MAX_RANK))
    return tuple(
        ScoredCandidate(candidate=s.candidate, features=s.features,
                        rank=i + 1, evidence_level=s.evidence_level,
                        limitations=s.limitations)
        for i, s in enumerate(scored[:cap]))


# ── stage 3 · explanation ───────────────────────────────────────────────

@dataclass(frozen=True)
class Explanation:
    """The shape a model would narrate, generated deterministically.

    Every sentence is derived from a row that was read. A model handed one of
    these is being asked to phrase it, never to supply the content — which is
    the only arrangement in which a language model can be let near this at all.
    """

    cause_id: str
    cause: str
    headline: str
    support: tuple[str, ...]
    contradiction: tuple[str, ...]
    limitations: tuple[str, ...]
    feature_lines: tuple[str, ...]

    def as_text(self) -> str:
        blocks = [self.headline]
        for title, lines in (("Supporting", self.support),
                             ("Contradicting", self.contradiction),
                             ("Limitations", self.limitations),
                             ("How this was ranked", self.feature_lines)):
            if lines:
                blocks.append(title + ":\n"
                              + "\n".join("  - " + line for line in lines))
        return "\n\n".join(blocks)


def explain_candidate(scored: ScoredCandidate) -> Explanation:
    """**Stage 3.** Turn a ranked hypothesis into sentences, deterministically.

    Case ids are printed rather than counted. An id is a pointer an engineer
    can open; a count is a claim about a population, and the population here
    is a reportable-occurrence sample that systematically excludes routine
    removals (PLAN §1.3), so a count would overstate what was checked.
    """
    raw = scored.candidate
    support = [
        f"prior case {c.evidence_id} on tail {c.tail or 'unknown'} recorded "
        f"{c.action_type or 'an action'} on this component"
        + (f", and the write-up recorded a condition found"
           if c.finding_type == CONFIRMED_FAULT else "")
        for c in raw.supporting]
    contradiction = [
        f"prior case {c.evidence_id}: {c.contradiction_reason}"
        for c in raw.contradicting]
    feature_lines = [
        f"{f.name} contributed {f.contribution:+g} ({f.detail})"
        for f in scored.features if f.observed]
    return Explanation(
        cause_id=raw.cause_id, cause=raw.cause,
        headline=(f"{raw.cause or 'unnamed component'} — ranked "
                  f"{scored.rank}, evidence {scored.evidence_level}. {CLAIM}."),
        support=tuple(support), contradiction=tuple(contradiction),
        limitations=tuple(scored.limitations),
        feature_lines=tuple(feature_lines) or (
            "no feature registered a non-zero observation",))


# ── the assembled report ────────────────────────────────────────────────

@dataclass(frozen=True)
class CauseReport:
    """Everything Phase 5 produced for one request, with its own caveats.

    `evidence_level` is the report's own, not the top candidate's alone: with
    no candidates at all it is `insufficient`, which is the honest answer and
    the one `missing_information` then has to justify.
    """

    request: CauseRequest
    candidates: tuple[CauseCandidate, ...]
    scored: tuple[ScoredCandidate, ...]
    explanations: tuple[Explanation, ...]
    evidence_level: str
    missing_information: tuple[str, ...]
    limitations: tuple[str, ...]
    census: dict
    source: str = SOURCE
    freshness: str = ""

    @property
    def claim(self) -> str:
        return CLAIM

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(sorted({e for s in self.scored
                             for e in s.candidate.evidence_ids}))


def _missing_information(con: sqlite3.Connection, request: CauseRequest,
                         census: dict, candidates: int) -> tuple[str, ...]:
    """What is absent, named. Always populated — absence is never implied.

    Written from measured state rather than assumed: the closure table is
    checked, not remembered. It has been empty in every database built so far
    and the sentence would still be wrong to hard-code.
    """
    out = []
    if not _run(con, "SELECT 1 FROM defect_closure LIMIT 1"):
        out.append("no confirmed maintenance outcome exists in this database "
                   "— nothing here has been checked against what actually "
                   "fixed the aircraft")
    if not candidates:
        out.append("no prior case in the population searched implicates any "
                   "component for this complaint")
    if census.get("truncated"):
        out.append("the search hit its scan cap, so older cases in this "
                   "chapter were not examined")
    if census.get("fault_codes_unmatched"):
        out.append("a supplied fault code is not in the maintenance message "
                   "catalogue")
    if request.fault_codes:
        out.append("no defect in this case base carries a fault code, so the "
                   "codes supplied could route to a chapter but could not be "
                   "matched against prior cases")
    if request.tail and not _run(
            con, "SELECT 1 FROM aircraft WHERE tail = ?", (request.tail,)):
        out.append("this tail is not in the aircraft register, so nothing is "
                   "known about its build standard or its hours")
    if census.get("cases_matched") and not census.get("cases_with_finding"):
        out.append("no examined case recorded what was found, only what was "
                   "done")
    return tuple(out)


REPORT_CAVEAT = (
    "the case base is the FAA SDR reportable-occurrence sample, which "
    "excludes routine removals — absence of a candidate is not evidence that "
    "nothing causes this")


def propose_causes(con: sqlite3.Connection,
                   request: CauseRequest) -> CauseReport:
    """Run all three stages and assemble the report.

    Convenience only. The stages remain independently callable and are tested
    that way; nothing below decides anything the three functions did not
    already decide.
    """
    raw, census = generate_candidates(con, request)
    scored = score_candidates(raw, request)
    explanations = tuple(explain_candidate(s) for s in scored)
    candidates = tuple(
        CauseCandidate(
            cause_id=s.candidate.cause_id, cause=s.candidate.cause,
            rank=s.rank, evidence_level=s.evidence_level,
            supporting_case_ids=s.candidate.supporting_case_ids,
            contradicting_case_ids=s.candidate.contradicting_case_ids,
            limitations=s.limitations,
            provenance=dict(s.candidate.provenance,
                            claim=CLAIM,
                            score=s.score,
                            score_meaning=SCORE_MEANING,
                            features={f.name: f.contribution
                                      for f in s.features}))
        for s in scored)
    level = scored[0].evidence_level if scored else INSUFFICIENT
    return CauseReport(
        request=request, candidates=candidates, scored=scored,
        explanations=explanations, evidence_level=level,
        missing_information=_missing_information(
            con, request, census, len(candidates)),
        limitations=(REPORT_CAVEAT,), census=census,
        freshness=metrics.latest_report_date(con) or "unknown")
