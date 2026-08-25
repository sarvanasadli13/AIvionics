"""Typed answers from the model, and the checks that throw a bad one away.

Phase 2 of the Nemotron work. `service.py` gave the application a model; this
gives it a *shape* for what a model is allowed to hand back, and the two rules
that shape exists to enforce:

* **Validation rejects, it never repairs.** A malformed answer that is quietly
  patched into a well-formed one reads exactly like an answer that was right
  the first time. Every helper in here raises `SchemaError` rather than
  substituting a default, dropping an unparseable field, or coercing `"3"` to
  `3`. Standing rule 4 is unenforceable otherwise: once a value has been
  silently rewritten nobody can say where it came from.
* **Evidence is a category, never a percentage.** The four levels in
  `EVIDENCE_LEVELS` are the whole vocabulary. A model that writes "87%
  confidence" is generating a statistic out of nothing, and the six-model
  review killed exactly that claim — the labels in this corpus record what
  engineers *did*, not what was correct, so no number derived from them means
  what a percentage would appear to mean.

The second half of the file is the grounding check. Schema validity says the
object is well formed; grounding says it is *admissible* — every task number
it cites was retrieved, every numeral it states appears in a tool result,
every cause has evidence behind it, and unresolved effectivity has been failed
closed (standing rule 8). `enforce_grounding` is the gate: it returns the
answer or it raises. There is deliberately no path that returns a rejected
answer with the offending parts stripped out.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Iterator, Mapping, Sequence

from .service import extract_json


class SchemaError(ValueError):
    """A payload did not match its schema.

    A `ValueError` subclass so a caller that only knows it is parsing
    untrusted text still catches it, and so `extract_json`'s own `ValueError`
    and this one can be handled in one place.
    """


# ── vocabularies ────────────────────────────────────────────────────────
#
# Every one of these is closed. An unrecognised member is rejected, because a
# value outside the vocabulary is a value nothing downstream knows how to
# render — and the failure mode of rendering it anyway is a screen that shows
# a state the engineer has never been taught to read.

STRONG = "strong"
LIMITED = "limited"
CONFLICTING = "conflicting"
INSUFFICIENT = "insufficient"
EVIDENCE_LEVELS = (STRONG, LIMITED, CONFLICTING, INSUFFICIENT)

# What each level is allowed to mean, in words. Kept here so the prompt that
# asks a model for one and the screen that renders it cannot drift apart, and
# so it stays visible that none of these is a number.
EVIDENCE_MEANING = {
    STRONG: "several independent prior cases agree and none contradict",
    LIMITED: "one or two prior cases point this way and no more",
    CONFLICTING: "the prior cases disagree with each other",
    INSUFFICIENT: "the case base does not answer this question",
}

# The four outcomes of `retrieval.search.resolve_applicability`, reused rather
# than restated: an effectivity vocabulary that disagreed with the retrieval
# engine's would be worse than none.
APPLICABLE = "applicable"
NOT_APPLICABLE = "not_applicable"
UNRESOLVED = "unresolved"
NOT_CHECKED = "not_checked"
EFFECTIVITY_STATES = (APPLICABLE, NOT_APPLICABLE, UNRESOLVED, NOT_CHECKED)

# Anything that is not positively `applicable` has to be failed closed.
EFFECTIVITY_NEEDS_CLOSING = (NOT_APPLICABLE, UNRESOLVED, NOT_CHECKED)

# The exact sentence the Diagnose, Manuals and print paths already use for an
# unresolved airframe (`ui/printing.py`). The grounding check looks for this
# string, so it has to be the same string.
UNRESOLVED_NOTICE = "applicability unresolved — verify in controlled data"

# Whether a document may lawfully be worked from. No authorization records
# exist in this database — see `tools.check_document_authorization` — so
# `unknown` is currently the only value any honest producer can emit, and the
# grounding check treats a document recommendation as unusable until that
# changes.
AUTHORIZED = "authorized"
NOT_AUTHORIZED = "not_authorized"
AUTHORIZATION_UNKNOWN = "unknown"
AUTHORIZATION_STATES = (AUTHORIZED, NOT_AUTHORIZED, AUTHORIZATION_UNKNOWN)

# `manual.manual_type` in `db.SCHEMA`.
MANUAL_TYPES = ("AMM", "FIM", "TSM", "IPC", "WDM", "SRM", "CMM", "MEL", "DDG")

# How a page relates to the task it was recommended for. A page that merely
# *mentions* a task is not a page an engineer should be sent to, so the
# relationship is carried explicitly rather than implied by proximity.
PAGE_RELATIONSHIPS = ("contains_task_start", "continues_task",
                      "contains_referenced_step", "contains_warning_or_caution")

# A hypothesis list longer than this is not a hypothesis list — it is a way of
# being right by covering everything, and it costs the engineer the one thing
# the tool is meant to save.
MIN_RANK = 1
MAX_RANK = 10

# Stable-ID namespaces. Tools emit these and answers cite them, so grounding
# is a set membership test rather than a string-similarity guess.
EVIDENCE_KINDS = ("task", "defect", "manual", "mmsg", "repeat", "aircraft")


def evidence_id(kind: str, value: Any) -> str:
    """`"defect:146266"` — the one spelling of an evidence reference."""
    if kind not in EVIDENCE_KINDS:
        raise SchemaError(f"unknown evidence kind {kind!r}; "
                          f"expected one of {', '.join(EVIDENCE_KINDS)}")
    return f"{kind}:{value}"


# ── primitive validators ────────────────────────────────────────────────
#
# Each one rejects on its own. There is no "clean up then validate" pass,
# because the cleaning is the part that would hide the problem.

def _payload(schema: str, payload: Any) -> Mapping:
    if not isinstance(payload, Mapping):
        raise SchemaError(
            f"{schema}: expected an object, got {type(payload).__name__}")
    return payload


def _present(schema: str, payload: Mapping, key: str) -> Any:
    if key not in payload:
        raise SchemaError(f"{schema}: required field {key!r} is missing")
    return payload[key]


def _text(schema: str, payload: Mapping, key: str, *,
          required: bool = True, default: str = "") -> str:
    """A string. `True` is not a string and neither is `3` — both are rejected."""
    if key not in payload or payload[key] is None:
        if required:
            raise SchemaError(f"{schema}: required field {key!r} is missing")
        return default
    value = payload[key]
    if not isinstance(value, str):
        raise SchemaError(f"{schema}: field {key!r} must be text, "
                          f"got {type(value).__name__}")
    if required and not value.strip():
        raise SchemaError(f"{schema}: field {key!r} is empty")
    return value.strip()


def _whole(schema: str, payload: Mapping, key: str, *,
           required: bool = True, default: int = 0) -> int:
    """An integer. `bool` is an `int` in Python and is rejected here anyway."""
    if key not in payload or payload[key] is None:
        if required:
            raise SchemaError(f"{schema}: required field {key!r} is missing")
        return default
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(f"{schema}: field {key!r} must be a whole number, "
                          f"got {type(value).__name__}")
    return value


def _flag(schema: str, payload: Mapping, key: str, *,
          default: bool = False) -> bool:
    if key not in payload or payload[key] is None:
        return default
    value = payload[key]
    if not isinstance(value, bool):
        raise SchemaError(f"{schema}: field {key!r} must be true or false, "
                          f"got {type(value).__name__}")
    return value


def _text_tuple(schema: str, payload: Mapping, key: str, *,
                required: bool = False) -> tuple[str, ...]:
    """A list of strings. `required` here means *non-empty*, not merely present."""
    raw = payload.get(key)
    if raw is None:
        if required:
            raise SchemaError(f"{schema}: required list {key!r} is missing")
        return ()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise SchemaError(f"{schema}: field {key!r} must be a list of text, "
                          f"got {type(raw).__name__}")
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, str):
            raise SchemaError(f"{schema}: {key}[{i}] must be text, "
                              f"got {type(item).__name__}")
        if item.strip():
            out.append(item.strip())
    if required and not out:
        raise SchemaError(f"{schema}: list {key!r} is empty, and this answer "
                          f"is not meaningful without at least one entry")
    return tuple(out)


def _whole_tuple(schema: str, payload: Mapping, key: str, *,
                 required: bool = False) -> tuple[int, ...]:
    raw = payload.get(key)
    if raw is None:
        if required:
            raise SchemaError(f"{schema}: required list {key!r} is missing")
        return ()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise SchemaError(f"{schema}: field {key!r} must be a list of whole "
                          f"numbers, got {type(raw).__name__}")
    out = []
    for i, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(item, int):
            raise SchemaError(f"{schema}: {key}[{i}] must be a whole number, "
                              f"got {type(item).__name__}")
        out.append(item)
    if required and not out:
        raise SchemaError(f"{schema}: list {key!r} is empty")
    return tuple(out)


def _one_of(schema: str, value: str, allowed: Sequence[str], key: str) -> str:
    if value not in allowed:
        raise SchemaError(f"{schema}: {key} {value!r} is not one of "
                          f"{', '.join(allowed)}")
    return value


def _mapping(schema: str, payload: Mapping, key: str, *,
             required: bool = True) -> dict:
    raw = payload.get(key)
    if raw is None:
        if required:
            raise SchemaError(f"{schema}: required field {key!r} is missing")
        return {}
    if not isinstance(raw, Mapping):
        raise SchemaError(f"{schema}: field {key!r} must be an object, "
                          f"got {type(raw).__name__}")
    if required and not raw:
        raise SchemaError(
            f"{schema}: {key!r} is empty — a recommendation with no retrieval "
            f"evidence behind it is a recommendation from nowhere")
    return dict(raw)


# ── the schemas ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InvestigationRequest:
    """What an engineer asked, before anything has been inferred from it.

    Kept separate from `ExtractedComplaint` on purpose: the raw complaint is
    the audit anchor, and once a model has normalised it there is no way back
    to the words the engineer actually wrote.
    """

    raw_complaint: str
    tail: str
    reported_at: str
    flight_number: str = ""
    departure_airport: str = ""
    arrival_airport: str = ""
    phase_of_flight: str = ""


def parse_investigation_request(payload: Any) -> InvestigationRequest:
    schema = "InvestigationRequest"
    data = _payload(schema, payload)
    return InvestigationRequest(
        raw_complaint=_text(schema, data, "raw_complaint"),
        tail=_text(schema, data, "tail"),
        reported_at=_text(schema, data, "reported_at"),
        flight_number=_text(schema, data, "flight_number", required=False),
        departure_airport=_text(schema, data, "departure_airport", required=False),
        arrival_airport=_text(schema, data, "arrival_airport", required=False),
        phase_of_flight=_text(schema, data, "phase_of_flight", required=False),
    )


@dataclass(frozen=True)
class ExtractedComplaint:
    """The complaint after normalisation — candidates, never conclusions.

    `ata_candidates` is plural and stays plural. Reporter-entered JASC/ATA
    codes are miscoded at the 22/24/27/31/34 boundaries, so committing to one
    chapter here would put the right task permanently out of reach with no
    recovery path (PLAN 2.5).
    """

    normalized_symptom: str
    ata_candidates: tuple[str, ...] = ()
    system_candidates: tuple[str, ...] = ()
    fault_codes: tuple[str, ...] = ()
    phase_of_flight: str = ""
    environmental: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    evidence_level: str = INSUFFICIENT


def parse_extracted_complaint(payload: Any) -> ExtractedComplaint:
    schema = "ExtractedComplaint"
    data = _payload(schema, payload)
    return ExtractedComplaint(
        normalized_symptom=_text(schema, data, "normalized_symptom"),
        ata_candidates=_text_tuple(schema, data, "ata_candidates"),
        system_candidates=_text_tuple(schema, data, "system_candidates"),
        fault_codes=_text_tuple(schema, data, "fault_codes"),
        phase_of_flight=_text(schema, data, "phase_of_flight", required=False),
        environmental=_text_tuple(schema, data, "environmental"),
        missing_information=_text_tuple(schema, data, "missing_information"),
        evidence_level=_one_of(
            schema, _text(schema, data, "evidence_level"),
            EVIDENCE_LEVELS, "evidence_level"),
    )


@dataclass(frozen=True)
class CauseCandidate:
    """One ranked hypothesis, and the case ids that put it there.

    `supporting_case_ids` is required and must be non-empty. A hypothesis with
    nothing behind it is not a hypothesis, it is a guess; where the case base
    is silent the correct output is an entry in `missing_information` and an
    abstention, not a ranked cause with an `insufficient` label attached to
    make it look accounted for.
    """

    cause_id: str
    cause: str
    rank: int
    evidence_level: str
    supporting_case_ids: tuple[int, ...]
    contradicting_case_ids: tuple[int, ...] = ()
    limitations: tuple[str, ...] = ()
    provenance: dict = field(default_factory=dict)


def parse_cause_candidate(payload: Any) -> CauseCandidate:
    schema = "CauseCandidate"
    data = _payload(schema, payload)
    rank = _whole(schema, data, "rank")
    if not MIN_RANK <= rank <= MAX_RANK:
        raise SchemaError(
            f"{schema}: rank {rank} is outside {MIN_RANK}..{MAX_RANK}")
    return CauseCandidate(
        cause_id=_text(schema, data, "cause_id"),
        cause=_text(schema, data, "cause"),
        rank=rank,
        evidence_level=_one_of(
            schema, _text(schema, data, "evidence_level"),
            EVIDENCE_LEVELS, "evidence_level"),
        supporting_case_ids=_whole_tuple(
            schema, data, "supporting_case_ids", required=True),
        contradicting_case_ids=_whole_tuple(
            schema, data, "contradicting_case_ids"),
        limitations=_text_tuple(schema, data, "limitations"),
        provenance=_mapping(schema, data, "provenance", required=False),
    )


@dataclass(frozen=True)
class DocumentRecommendation:
    """A manual task an engineer is being pointed at — a locator, never a body.

    Standing rule 1: the tool indexes into controlled data, it does not
    reproduce it. Everything here is what you would write on a slip of paper
    before walking to the approved source.
    """

    manual_id: int
    manual_type: str
    task_number: str
    title: str
    revision: str
    effectivity_result: str
    retrieval_evidence: dict
    authorization_state: str = AUTHORIZATION_UNKNOWN


def parse_document_recommendation(payload: Any) -> DocumentRecommendation:
    schema = "DocumentRecommendation"
    data = _payload(schema, payload)
    return DocumentRecommendation(
        manual_id=_whole(schema, data, "manual_id"),
        manual_type=_one_of(schema, _text(schema, data, "manual_type"),
                            MANUAL_TYPES, "manual_type"),
        task_number=_text(schema, data, "task_number"),
        title=_text(schema, data, "title"),
        revision=_text(schema, data, "revision"),
        effectivity_result=_one_of(
            schema, _text(schema, data, "effectivity_result"),
            EFFECTIVITY_STATES, "effectivity_result"),
        retrieval_evidence=_mapping(schema, data, "retrieval_evidence"),
        authorization_state=_one_of(
            schema, _text(schema, data, "authorization_state",
                          required=False, default=AUTHORIZATION_UNKNOWN),
            AUTHORIZATION_STATES, "authorization_state"),
    )


@dataclass(frozen=True)
class PageRecommendation:
    """A specific page of a specific revision, with what must be read with it.

    A page is never sufficient on its own. A task that starts on page 203 and
    carries its WARNING on 202 is a task an engineer must not be sent into at
    203, so the preceding and following pages and the warning/caution pages
    are part of the recommendation rather than a detail left to the reader.

    **Nothing can produce one of these today.** No page index has been built,
    so `tools.get_page_metadata` returns unavailable and the grounding check
    rejects any answer that cites a page. The schema exists so that the
    contract is fixed before the index is, not because the feature works.
    """

    manual_id: int
    revision: str
    source_page: int
    printed_page_label: str
    task_number: str
    task_relationship: str
    required_preceding_pages: tuple[int, ...] = ()
    required_following_pages: tuple[int, ...] = ()
    warning_pages: tuple[int, ...] = ()
    caution_pages: tuple[int, ...] = ()
    authorization_state: str = AUTHORIZATION_UNKNOWN
    effectivity_state: str = NOT_CHECKED


def parse_page_recommendation(payload: Any) -> PageRecommendation:
    schema = "PageRecommendation"
    data = _payload(schema, payload)
    page = _whole(schema, data, "source_page")
    if page < 1:
        raise SchemaError(f"{schema}: source_page {page} is not a page number")
    return PageRecommendation(
        manual_id=_whole(schema, data, "manual_id"),
        revision=_text(schema, data, "revision"),
        source_page=page,
        printed_page_label=_text(schema, data, "printed_page_label"),
        task_number=_text(schema, data, "task_number"),
        task_relationship=_one_of(
            schema, _text(schema, data, "task_relationship"),
            PAGE_RELATIONSHIPS, "task_relationship"),
        required_preceding_pages=_whole_tuple(
            schema, data, "required_preceding_pages"),
        required_following_pages=_whole_tuple(
            schema, data, "required_following_pages"),
        warning_pages=_whole_tuple(schema, data, "warning_pages"),
        caution_pages=_whole_tuple(schema, data, "caution_pages"),
        authorization_state=_one_of(
            schema, _text(schema, data, "authorization_state",
                          required=False, default=AUTHORIZATION_UNKNOWN),
            AUTHORIZATION_STATES, "authorization_state"),
        effectivity_state=_one_of(
            schema, _text(schema, data, "effectivity_state",
                          required=False, default=NOT_CHECKED),
            EFFECTIVITY_STATES, "effectivity_state"),
    )


@dataclass(frozen=True)
class AssistantAnswer:
    """Everything the model produced for one investigation.

    `narrative` is carried because the numeral check needs prose to check. An
    answer whose structured fields are clean and whose sentence says "in 7 of
    12 cases" is the failure standing rule 4 exists to stop, and the sentence
    is the only place that claim lives.

    `model_version` and `index_version` are required. An answer that cannot
    say which model wrote it and which index it read is an answer nobody can
    re-run, and standing rule 9 makes the index stamp load-bearing: changing
    the embedding model invalidates every stored vector and every measurement
    taken against them.
    """

    interpreted_complaint: ExtractedComplaint
    hypotheses: tuple[CauseCandidate, ...]
    recommended_documents: tuple[DocumentRecommendation, ...]
    recommended_pages: tuple[PageRecommendation, ...]
    supporting_evidence: tuple[str, ...]
    contradicting_evidence: tuple[str, ...]
    missing_information: tuple[str, ...]
    limitations: tuple[str, ...]
    abstained: bool
    abstention_reason: str
    model_version: str
    index_version: str
    evidence_ids: tuple[str, ...]
    narrative: str = ""

    @property
    def prose(self) -> tuple[str, ...]:
        """Every free-text field the model wrote, for the numeral check.

        All of them, uniformly. Exempting `limitations` because it describes
        the absence of data would leave one field a model could put "in 7 of
        12 cases" into and have it survive.
        """
        return (
            (self.narrative,)
            + tuple(h.cause for h in self.hypotheses)
            + tuple(line for h in self.hypotheses for line in h.limitations)
            + self.supporting_evidence + self.contradicting_evidence
            + self.missing_information + self.limitations
            + (self.abstention_reason,)
        )


def parse_assistant_answer(payload: Any) -> AssistantAnswer:
    schema = "AssistantAnswer"
    data = _payload(schema, payload)

    abstained = _flag(schema, data, "abstained")
    hypotheses = tuple(parse_cause_candidate(item)
                       for item in _list(schema, data, "hypotheses"))

    # Ranks are positions in *this* list, so they must be 1..N and unique. A
    # duplicated or skipped rank means the model was not actually ordering
    # anything, and a ranked list nobody ordered is worse than an unordered one
    # because the engineer reads the first entry as the best one.
    ranks = sorted(h.rank for h in hypotheses)
    if ranks and ranks != list(range(1, len(hypotheses) + 1)):
        raise SchemaError(
            f"{schema}: hypothesis ranks {ranks} are not 1..{len(hypotheses)} "
            f"without duplicates")

    answer = AssistantAnswer(
        interpreted_complaint=parse_extracted_complaint(
            _present(schema, data, "interpreted_complaint")),
        hypotheses=hypotheses,
        recommended_documents=tuple(
            parse_document_recommendation(item)
            for item in _list(schema, data, "recommended_documents")),
        recommended_pages=tuple(
            parse_page_recommendation(item)
            for item in _list(schema, data, "recommended_pages")),
        supporting_evidence=_text_tuple(schema, data, "supporting_evidence"),
        contradicting_evidence=_text_tuple(schema, data, "contradicting_evidence"),
        missing_information=_text_tuple(schema, data, "missing_information"),
        limitations=_text_tuple(schema, data, "limitations"),
        abstained=abstained,
        abstention_reason=_text(schema, data, "abstention_reason",
                                required=abstained),
        model_version=_text(schema, data, "model_version"),
        index_version=_text(schema, data, "index_version"),
        evidence_ids=_text_tuple(schema, data, "evidence_ids",
                                 required=not abstained),
        narrative=_text(schema, data, "narrative", required=False),
    )

    # The abstention invariant, both ways round. An answer that declines must
    # say why; an answer that does not decline must have produced something,
    # because "no hypotheses and no abstention" is silence dressed as a result.
    if not abstained and not answer.hypotheses:
        raise SchemaError(
            f"{schema}: no hypotheses and no abstention — an answer with "
            f"nothing in it must set abstained and say why")
    return answer


def _list(schema: str, payload: Mapping, key: str) -> Sequence:
    raw = payload.get(key)
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise SchemaError(f"{schema}: field {key!r} must be a list, "
                          f"got {type(raw).__name__}")
    return raw


def parse_answer_json(text: str) -> AssistantAnswer:
    """Parse a model's raw reply into an `AssistantAnswer`, or raise.

    `extract_json` does the tolerant part — it survives a reasoning preamble,
    a ```json fence and trailing prose, all of which Nemotron emits every time.
    What it does not do is tolerate broken JSON, and neither does this: a reply
    that cannot be parsed is rejected, never guessed at.
    """
    try:
        payload = extract_json(text)
    except ValueError as exc:
        raise SchemaError(
            f"the model's answer is not valid JSON — {exc}") from exc
    return parse_assistant_answer(payload)


# ── grounding ───────────────────────────────────────────────────────────
#
# Schema validity says the object is well formed. Grounding says it is
# admissible: that everything it asserts came from a tool result rather than
# from the model.

# Same shape as `search.TASK_NUMBER_TOKEN`, and the same job — but this one
# also has to be *removed* from prose before numerals are counted, because
# `34-11-01-400-801` is six numerals that would otherwise all look invented.
TASK_NUMBER = re.compile(
    r"\b[A-Z]?\d{2}-\d{2}-\d{2}(?:-\d{3})?(?:-(?:[A-Z]\d{2}|\d{3}[A-Z]?))?\b",
    re.I)
NUMERAL = re.compile(r"\d+(?:[.,]\d+)?")

# Small counts that are ordinary English rather than claims about the data
# ("one of the cases", "both"). The same allowance `summarise.validate` makes,
# and it is duplicated rather than imported on purpose: `summarise` imports the
# Ollama client, and dragging a module that opens sockets into a pure validator
# would make this file impossible to reason about offline.
ALLOWED_NUMERALS = frozenset({"0", "1", "2", "3", "4"})


@dataclass(frozen=True)
class Violation:
    """One reason an answer was refused, in a form a log can group on."""

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class GroundingReport:
    """Every violation found, or none. Never a partial pass."""

    violations: tuple[Violation, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.violations

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(v.code for v in self.violations)

    def describe(self) -> str:
        if self.accepted:
            return "grounded in the tool results"
        return "; ".join(str(v) for v in self.violations)


class GroundingError(ValueError):
    """An answer failed grounding and was refused.

    Carries the whole report rather than the first violation: an answer that
    cites a task nobody retrieved *and* states a number nobody measured has
    two problems, and fixing the one that happened to be checked first would
    leave the other in place.
    """

    def __init__(self, report: GroundingReport) -> None:
        super().__init__(report.describe())
        self.report = report


@dataclass(frozen=True)
class ToolEvidence:
    """Everything the tools actually returned, flattened for membership tests.

    Built by walking the tool results rather than by asking each tool to
    declare what it produced: a tool that forgot to declare something would
    silently narrow what the answer is allowed to say, and the failure would
    look like the model hallucinating.
    """

    task_numbers: frozenset[str] = frozenset()
    case_ids: frozenset[int] = frozenset()
    evidence_ids: frozenset[str] = frozenset()
    manual_ids: frozenset[int] = frozenset()
    pages: frozenset[tuple[int, int]] = frozenset()
    numerals: frozenset[str] = frozenset()

    @classmethod
    def collect(cls, results: Sequence[Any]) -> "ToolEvidence":
        tasks: set[str] = set()
        cases: set[int] = set()
        ids: set[str] = set()
        manuals: set[int] = set()
        pages: set[tuple[int, int]] = set()
        numerals: set[str] = set()

        for result in results or ():
            # A failed tool contributes no evidence. This is the whole reason
            # an unavailable tool must fail loudly rather than return an empty
            # success: an empty success would look like evidence of absence,
            # and nothing here could tell the difference.
            if not _result_ok(result):
                continue
            for key, value in _walk(_result_data(result)):
                if isinstance(value, str):
                    if key in ("task_number", "to_task_number"):
                        tasks.add(value.upper())
                    elif key == "evidence_id":
                        ids.add(value)
                    tasks.update(m.group(0).upper()
                                 for m in TASK_NUMBER.finditer(value))
                    numerals.update(_numerals(value))
                elif isinstance(value, bool):
                    continue                     # not a number anybody claimed
                elif isinstance(value, (int, float)):
                    if key in ("defect_id", "case_id", "repeat_defect_id"):
                        cases.add(int(value))
                    elif key == "manual_id":
                        manuals.add(int(value))
                    numerals.update(_numerals(str(value)))
            for page in _authorized_pages(_result_data(result)):
                pages.add(page)

        return cls(frozenset(tasks), frozenset(cases), frozenset(ids),
                   frozenset(manuals), frozenset(pages), frozenset(numerals))


def _result_ok(result: Any) -> bool:
    if isinstance(result, Mapping):
        return bool(result.get("ok", True))
    return bool(getattr(result, "ok", True))


def _result_data(result: Any) -> Any:
    if isinstance(result, Mapping):
        return result.get("data", result)
    return getattr(result, "data", result)


def _authorized_pages(data: Any) -> Iterator[tuple[int, int]]:
    """`(manual_id, source_page)` pairs from a page index, if one exists.

    Nothing produces this key today — see `PageRecommendation`. It is read
    here so that the day a page index is built, the grounding check starts
    accepting pages without anyone having to remember to change this file.
    """
    if not isinstance(data, Mapping):
        return
    for row in data.get("authorized_pages") or ():
        if isinstance(row, Mapping):
            manual, page = row.get("manual_id"), row.get("source_page")
            if isinstance(manual, int) and isinstance(page, int):
                yield manual, page


def _walk(node: Any, key: str = "") -> Iterator[tuple[str, Any]]:
    """Every leaf in a nested result, tagged with the key that held it."""
    if is_dataclass(node) and not isinstance(node, type):
        node = asdict(node)
    if isinstance(node, Mapping):
        for k, v in node.items():
            yield from _walk(v, str(k))
    elif isinstance(node, (list, tuple, set, frozenset)):
        for v in node:
            yield from _walk(v, key)
    else:
        yield key, node


def _numerals(text: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in NUMERAL.finditer(text or "")}


def check_grounding(answer: AssistantAnswer,
                    tool_results: Sequence[Any]) -> GroundingReport:
    """Check an answer against the tool results it was given.

    Returns every violation, not the first. The caller decides what to do with
    the report, but `enforce_grounding` is the only intended caller and it
    does exactly one thing with it.
    """
    evidence = ToolEvidence.collect(tool_results)
    found: list[Violation] = []

    # 1. Task numbers. A cited task must be one the retrieval step actually
    #    returned — including the ones cited only in prose, which is where a
    #    plausible-looking invention lands.
    cited = {doc.task_number.upper() for doc in answer.recommended_documents}
    cited |= {page.task_number.upper() for page in answer.recommended_pages}
    for text in answer.prose:
        cited |= {m.group(0).upper() for m in TASK_NUMBER.finditer(text or "")}
    for number in sorted(cited - evidence.task_numbers):
        found.append(Violation(
            "ungrounded_task_number",
            f"task {number} was not in any tool result — the answer cites a "
            f"task nothing retrieved"))

    # 2. Pages. There is no page index, so `evidence.pages` is empty and every
    #    page citation fails. That is the correct behaviour and not a
    #    placeholder: a page number nobody can verify against a revision is
    #    precisely the citation that sends an engineer to the wrong sheet.
    for page in answer.recommended_pages:
        if (page.manual_id, page.source_page) not in evidence.pages:
            found.append(Violation(
                "ungrounded_page",
                f"page {page.source_page} of manual {page.manual_id} is not in "
                f"the authorized page index"
                + ("" if evidence.pages else
                   " — no page index has been built, so no page may be cited")))

    # 3. Numerals (standing rule 4). Task numbers are stripped first: they are
    #    checked above and are six numerals each.
    claimed: set[str] = set()
    for text in answer.prose:
        claimed |= _numerals(TASK_NUMBER.sub(" ", text or ""))
    for value in sorted(claimed - evidence.numerals - ALLOWED_NUMERALS):
        found.append(Violation(
            "unsupported_numeral",
            f"the answer states {value!r}, which is in no tool result — every "
            f"numeral shown to an engineer comes from the database"))

    # 4. Causes. Schema parsing already refuses a cause with an empty list; this
    #    catches the subtler case of ids that exist but point at records the
    #    tools never returned.
    for cause in answer.hypotheses:
        if not cause.supporting_case_ids:
            found.append(Violation(
                "cause_without_evidence",
                f"cause {cause.cause_id!r} has no supporting case ids"))
        for case_id in cause.supporting_case_ids + cause.contradicting_case_ids:
            if case_id not in evidence.case_ids:
                found.append(Violation(
                    "unknown_case_id",
                    f"cause {cause.cause_id!r} cites case {case_id}, which no "
                    f"tool returned"))

    # 5. Evidence ids must name records the tools handed over.
    for ref in answer.evidence_ids:
        if ref not in evidence.evidence_ids:
            found.append(Violation(
                "unknown_evidence_id",
                f"evidence id {ref!r} was not issued by any tool"))

    # 6. Effectivity, failed closed (standing rule 8). Anything not positively
    #    applicable has to carry the notice the rest of the application already
    #    uses, in the answer's own limitations, where the engineer will read it.
    unresolved = [doc for doc in answer.recommended_documents
                  if doc.effectivity_result in EFFECTIVITY_NEEDS_CLOSING]
    unresolved += [page for page in answer.recommended_pages
                   if page.effectivity_state in EFFECTIVITY_NEEDS_CLOSING]
    if unresolved and not _states_unresolved(answer):
        found.append(Violation(
            "effectivity_not_failed_closed",
            f"{len(unresolved)} recommendation(s) have unresolved "
            f"applicability and the answer does not say so — it must carry "
            f"{UNRESOLVED_NOTICE!r} in its limitations"))

    return GroundingReport(tuple(found))


def _states_unresolved(answer: AssistantAnswer) -> bool:
    notice = UNRESOLVED_NOTICE.lower()
    return any(notice in (line or "").lower() for line in answer.limitations)


def enforce_grounding(answer: AssistantAnswer,
                      tool_results: Sequence[Any]) -> AssistantAnswer:
    """Return the answer, or raise `GroundingError`.

    There is no third outcome. A rejected answer is not returned with the
    ungrounded parts removed: an answer that cited one invented task among
    four real ones is an answer whose reasoning included the invention, and
    deleting the sentence leaves the reader trusting the rest of it.
    """
    report = check_grounding(answer, tool_results)
    if not report.accepted:
        raise GroundingError(report)
    return answer
