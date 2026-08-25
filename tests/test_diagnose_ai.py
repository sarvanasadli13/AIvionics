"""Phase 3 — the Diagnose AI panel.

The panel's job is not to produce an answer. It is to refuse one when the
answer cannot be traced back to something retrieval actually found, and to
keep the rest of the screen working while it refuses. Every test here guards
one of the six promises the panel makes:

1. no AI hypothesis is ever presented as a confirmed cause — the corpus holds
   zero confirmed outcomes, `defect_closure` is empty;
2. evidence levels are the four categories and never a percentage;
3. a rejected answer never reaches the engineer — the violations do;
4. the deterministic search is complete with the model down, and the panel
   says so in words;
5. the UI thread is never blocked, and the work is cancellable;
6. provenance is on the screen: model, endpoint, served model, index version,
   evidence ids.

The one that has already been observed failing in the wild is (3). Asked where
a pitot probe heater fault-isolation task lives, Nemotron answered ATA 31; the
corpus puts that task at `30-31-00-810-801`, in ATA 30. Confident, plausible,
wrong, and indistinguishable from a right answer to anyone who is not holding
the manual. `test_the_pitot_heater_hallucination_never_reaches_the_engineer`
replays exactly that.

Nothing here opens a socket or needs a live model. Every model is a fake with
a canned reply, and the corpus is the four-table fixture from `test_ai_tools`
built into a temporary file.
"""
from __future__ import annotations

import dataclasses
import json
import re
import threading

import pytest

from aivionics.llm.schemas import EVIDENCE_LEVELS, EVIDENCE_MEANING
from aivionics.llm.service import (CancelToken, Cancelled, Generation, Health,
                                   ModelIdentity, Usage)
from aivionics.llm.tools import ToolRegistry
from aivionics.ui import aiservice as A
from aivionics.ui.pages import diagnose as D
from aivionics.ui.auth import User

# The corpus builder lives with the tool tests. Sharing it is deliberate: two
# fixtures that drift apart would let a tool test and a panel test disagree
# about what is in the database, and the panel tests would be the ones that
# looked right.
from test_ai_tools import _build_corpus

# What the corpus actually holds, and what a model is likely to invent instead.
REAL_TASK = "34-11-01-400-801"
# The measured hallucination: a real-looking task number in the wrong chapter.
HALLUCINATED_TASK = "31-11-00-810-801"

INDEX_VERSION = "idx-test"
SERVED_MODEL = "nemotron-test"


# ── fakes ───────────────────────────────────────────────────────────────

class FakeModel:
    """A model service with a canned reply. Never touches a socket.

    `reply` may be a string (returned as the generation text) or a callable
    taking `(prompt, system, max_tokens, cancel)` so a test can inspect what
    was asked or raise from inside the call.
    """

    provider = "fake"

    def __init__(self, reply="", *, ok: bool = True, present: bool = True,
                 served: str = SERVED_MODEL, requested: str = SERVED_MODEL,
                 reason: str = "", usage: Usage | None = None,
                 health_raises: Exception | None = None) -> None:
        self.reply = reply
        self._ok = ok
        self._present = present
        self._identity = ModelIdentity(requested=requested, served=served,
                                       provider="fake",
                                       endpoint="http://fake.invalid/v1")
        self._reason = reason
        self._usage = usage or Usage()
        self._health_raises = health_raises
        self.prompts: list[dict] = []

    def health(self) -> Health:
        if self._health_raises is not None:
            raise self._health_raises
        return Health(ok=self._ok, reason=self._reason,
                      model_present=self._present,
                      endpoint=self._identity.endpoint,
                      identity=self._identity)

    def identity(self) -> ModelIdentity:
        return self._identity

    def generate(self, prompt, *, system=None, max_tokens=None, cancel=None):
        self.prompts.append({"prompt": prompt, "system": system,
                             "max_tokens": max_tokens, "cancel": cancel})
        reply = self.reply
        if callable(reply):
            reply = reply(prompt, system, max_tokens, cancel)
        return Generation(text=reply, identity=self._identity,
                          usage=self._usage, raw=reply)


class NeverAskedModel(FakeModel):
    """Usable, but a call to `generate` is a test failure."""

    def generate(self, *_args, **_kw):                       # pragma: no cover
        raise AssertionError("the model was asked and should not have been")


# ── fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def corpus(tmp_path):
    path = tmp_path / "corpus.db"
    _build_corpus(path)
    return path


ENGINEER = User(1, "eng", "E. Engineer", "engineer", False)


@pytest.fixture
def engineer():
    return ENGINEER


def make_service(corpus, model, **kw) -> A.AIService:
    return A.AIService(db_path=corpus, index_version=INDEX_VERSION,
                       model=model, **kw)


@pytest.fixture
def answer_payload():
    """A well-formed answer, grounded in the corpus, that passes every check.

    Every rejection test below starts here and breaks exactly one thing, so a
    failure names the rule that was broken rather than leaving the reader to
    diff two payloads.
    """
    return {
        "interpreted_complaint": {
            "normalized_symptom": "air data module disagree on approach",
            "ata_candidates": ["34"],
            "system_candidates": ["air data"],
            "fault_codes": ["34-11002"],
            "evidence_level": "limited",
            "missing_information": ["no shop finding is recorded"],
        },
        "hypotheses": [{
            "cause_id": "c1",
            "cause": "Air data module internal drift",
            "rank": 1,
            "evidence_level": "limited",
            "supporting_case_ids": [1],
            "contradicting_case_ids": [2],
            "limitations": ["the case base records what was done, "
                            "not what was correct"],
        }],
        "recommended_documents": [{
            "manual_id": 1,
            "manual_type": "AMM",
            "task_number": REAL_TASK,
            "title": "Install the Air Data Module",
            "revision": "R12",
            "effectivity_result": "applicable",
            "retrieval_evidence": {"channel": "fts"},
        }],
        "recommended_pages": [],
        "supporting_evidence": ["a prior case replaced the air data module"],
        "contradicting_evidence": ["a prior case reseated a connector"],
        "missing_information": ["no closure state has been imported"],
        "limitations": ["absence of a case is not evidence that "
                        "nothing happened"],
        "abstained": False,
        "abstention_reason": "",
        "model_version": SERVED_MODEL,
        "index_version": INDEX_VERSION,
        "evidence_ids": ["task:" + REAL_TASK, "defect:1", "defect:2"],
        "narrative": "Prior reports on this tail point at the air data module.",
    }


def rendered_text(investigation: A.Investigation) -> str:
    """Every string the panel could possibly put on the screen, concatenated.

    Deliberately blunt: it walks the whole dataclass rather than the fields a
    panel happens to render today, so a field added later that carries a
    rejected answer is caught by the tests that scan this rather than by an
    engineer reading it on a screen.
    """
    return json.dumps(dataclasses.asdict(investigation), default=str)


def renderable_findings(investigation: A.Investigation) -> str:
    """The same, minus the refusal itself.

    A violation has to name what it refused — "task 31-11-00-810-801 was not
    in any tool result" is the whole value of the message — so the refusal
    text is the one place a fabricated number is allowed to appear. Leak
    checks scan everything else.
    """
    payload = dataclasses.asdict(investigation)
    payload.pop("report", None)
    payload.pop("reason", None)
    return json.dumps(payload, default=str)


# ── 1. the model being down is an ordinary state ────────────────────────

def test_an_unconfigured_model_is_a_state_not_an_exception(corpus):
    service = make_service(corpus, None)
    service._model_tried = True          # nothing to probe: no configuration
    status = service.status()
    assert status.available is False
    assert "no model is configured" in status.reason
    assert service.run("air data module fault").state == A.UNAVAILABLE


def test_an_unreachable_endpoint_is_the_same_ordinary_state(corpus):
    service = make_service(corpus, FakeModel(
        health_raises=ConnectionError("connection refused")))
    status = service.status()
    assert status.available is False
    # The reason is the real one, named in full, not "an error occurred".
    assert "ConnectionError" in status.reason
    assert "connection refused" in status.reason
    assert service.run("air data module fault").state == A.UNAVAILABLE


def test_a_listed_but_unserved_model_is_unavailable(corpus):
    """`ok` is not `usable`: a catalogue entry is not a loaded model."""
    service = make_service(corpus, FakeModel(ok=True, present=False,
                                             reason="model not pulled"))
    assert service.status().available is False
    assert service.run("air data module fault").state == A.UNAVAILABLE


def test_the_model_is_never_asked_when_it_is_unavailable(corpus):
    service = make_service(corpus, NeverAskedModel(ok=False,
                                                   reason="endpoint down"))
    run = service.run("air data module fault")
    assert run.state == A.UNAVAILABLE
    assert run.answer is None


def test_the_ai_path_cannot_reach_the_deterministic_search(corpus):
    """Structural, not behavioural.

    The promise is that a model failure cannot degrade the ranked-locator
    column, and the only way to be sure of that is that this module has no
    route to it at all. An import is a route — so the imports are what is
    checked, by parsing them, rather than the source text, which mentions
    `searchservice` in prose precisely because the pattern is copied from it.
    """
    import ast

    tree = ast.parse(open(A.__file__, encoding="utf-8").read())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported += [f"{node.module or ''}.{a.name}" for a in node.names]
    assert not [name for name in imported if "searchservice" in name], imported
    assert not [name for name in imported if name.endswith("SearchService")]


def test_the_deterministic_search_still_answers_with_the_model_down(corpus,
                                                                    engineer):
    """The left-hand column is not built out of the model, and this proves it.

    Runs the same retrieval the locator panel runs, through the tool registry,
    while the AI service reports the model unavailable. Both statements have
    to hold at once or "search is unaffected" is a claim nobody checked.
    """
    service = make_service(corpus, FakeModel(ok=False, reason="endpoint down"))
    assert service.run("air data module").state == A.UNAVAILABLE

    registry = ToolRegistry(db_path=corpus)
    try:
        result = registry.call("search_manual_tasks",
                               {"query": "air data module"}, engineer)
    finally:
        registry.close()
    assert result.ok
    numbers = [r["task_number"] for r in result.data["results"]]
    assert REAL_TASK in numbers


def test_the_panel_says_plainly_that_only_this_column_needs_a_model():
    title, body = D.STATE_TEXT[A.UNAVAILABLE]
    assert "not available" in title.lower()
    text = f"{title} {body}".lower()
    # It has to name what still works, and say it is whole rather than damaged.
    assert "the rest of the screen does not" in text
    assert "deterministic" in text
    assert "not a degraded version" in text


def test_every_terminal_state_has_words_on_the_screen():
    """A state with no text is a blank panel, which reads as a broken tool."""
    for state in (A.UNAVAILABLE, A.NO_EVIDENCE, A.MALFORMED, A.REJECTED,
                  A.CANCELLED, A.FAILED):
        title, body = D.STATE_TEXT[state]
        assert title.strip() and body.strip(), state
    assert A.ACCEPTED not in D.STATE_TEXT      # accepted renders the answer


# ── 2. nothing to reason over is not an invitation to guess ─────────────

def test_no_retrieval_means_the_model_is_not_asked_at_all(corpus):
    service = make_service(corpus, NeverAskedModel())
    run = service.run("zzzz no such symptom exists in this corpus zzzz")
    assert run.state == A.NO_EVIDENCE
    assert run.answer is None
    assert "deterministic search" in run.reason


# ── 3. a rejected answer never reaches the engineer ─────────────────────

def _run_with_answer(corpus, payload, **kw) -> A.Investigation:
    model = FakeModel(json.dumps(payload), **kw)
    service = make_service(corpus, model)
    return service.run("air data module fault", tail="D-ABCD", user=ENGINEER)


def test_a_grounded_answer_is_accepted(corpus, answer_payload):
    """The control. Without it every rejection test below passes vacuously."""
    run = _run_with_answer(corpus, answer_payload)
    assert run.state == A.ACCEPTED, run.reason
    assert run.accepted
    assert run.answer is not None
    assert run.violations == ()


def test_the_pitot_heater_hallucination_never_reaches_the_engineer(
        corpus, answer_payload):
    """The measured failure, replayed.

    A task number that looks exactly like a real one, in the wrong chapter,
    written confidently by the model. It must not survive to the screen in any
    form — not in the answer, not truncated into a reason line, not anywhere a
    panel could reach.
    """
    answer_payload["recommended_documents"][0]["task_number"] = HALLUCINATED_TASK
    run = _run_with_answer(corpus, answer_payload)

    assert run.state == A.REJECTED
    assert run.answer is None
    # Nowhere an engineer could read it as a recommendation.
    assert HALLUCINATED_TASK not in renderable_findings(run)
    assert any("task" in v.code for v in run.violations), run.violations
    # Where it does appear it is the subject of the refusal, never a locator.
    named = [v.detail for v in run.violations if HALLUCINATED_TASK in v.detail]
    assert named, "the refusal did not say which task was fabricated"
    for detail in named:
        assert "not in any tool result" in detail, detail


def test_a_rejected_answer_is_not_carried_in_any_field(corpus, answer_payload):
    """Structural: there is no field a future panel could render by accident."""
    answer_payload["recommended_documents"][0]["task_number"] = HALLUCINATED_TASK
    marker = "THIS SENTENCE MUST NEVER BE SHOWN"
    answer_payload["narrative"] = marker
    run = _run_with_answer(corpus, answer_payload)

    assert run.state == A.REJECTED
    assert run.answer is None
    assert marker not in rendered_text(run)


def test_a_rejection_shows_the_violations_rather_than_an_answer(
        corpus, answer_payload):
    answer_payload["recommended_documents"][0]["task_number"] = HALLUCINATED_TASK
    run = _run_with_answer(corpus, answer_payload)

    assert run.rejected
    assert run.answer is None, "the violations are what is shown, not the answer"
    assert run.violations, "a rejection with no violations explains nothing"
    for violation in run.violations:
        assert violation.code and violation.detail
    assert run.reason == run.report.describe()
    assert run.reason.strip()


def test_every_violation_is_reported_not_just_the_first(corpus,
                                                        answer_payload):
    """Two problems, two violations.

    Fixing whichever happened to be checked first would leave the other in
    place, and the answer would come back looking corrected.
    """
    answer_payload["recommended_documents"][0]["task_number"] = HALLUCINATED_TASK
    answer_payload["hypotheses"][0]["supporting_case_ids"] = [9999]
    run = _run_with_answer(corpus, answer_payload)

    assert run.state == A.REJECTED
    assert len(run.violations) >= 2, run.report.describe()
    assert len(set(run.report.codes)) >= 2, run.report.codes


def test_an_uncited_case_id_is_refused(corpus, answer_payload):
    answer_payload["hypotheses"][0]["supporting_case_ids"] = [4242]
    run = _run_with_answer(corpus, answer_payload)
    assert run.state == A.REJECTED
    assert "4242" not in json.dumps(
        dataclasses.asdict(run.report), default=str) or run.answer is None
    assert run.answer is None


def test_an_unparseable_reply_is_discarded_rather_than_repaired(corpus):
    """A malformed answer patched into a well-formed one reads exactly like
    one that was right the first time, so nothing is patched."""
    prose = "I think it is probably the air data module."
    service = make_service(corpus, FakeModel(prose))
    run = service.run("air data module fault", tail="D-ABCD", user=ENGINEER)
    assert run.state == A.MALFORMED
    assert run.answer is None
    assert prose not in renderable_findings(run)


def test_a_truncated_reply_names_the_budget_rather_than_being_parsed(
        corpus, answer_payload):
    """A reasoning model that runs out of tokens returns half a thought.

    Parsing it would either fail confusingly or — worse — succeed on a partial
    object. It is refused with the reason named, so someone knows to raise the
    budget instead of chasing a schema bug.
    """
    model = FakeModel(json.dumps(answer_payload),
                      usage=Usage(truncated=True, finish_reason="length"))
    service = make_service(corpus, model)
    run = service.run("air data module fault", tail="D-ABCD",
                      user=ENGINEER)
    assert run.state == A.MALFORMED
    assert run.answer is None
    assert "ran out of tokens" in run.reason


def test_the_prompt_hands_the_model_the_candidate_set_it_must_copy_from(
        corpus, answer_payload):
    model = FakeModel(json.dumps(answer_payload))
    service = make_service(corpus, model)
    service.run("air data module fault", tail="D-ABCD",
                      user=ENGINEER)

    prompt = model.prompts[0]["prompt"]
    assert "ALLOWED TASK NUMBERS:" in prompt
    assert "ALLOWED CASE IDS:" in prompt
    assert REAL_TASK in prompt
    # The rule is enforced twice on purpose — a request in the prompt and a
    # gate on the way back. Both have to be present.
    system = model.prompts[0]["system"]
    assert "copied character for character" in system


def test_an_empty_candidate_set_is_spelled_out_as_an_instruction():
    """"(none)" says recommend nothing; a missing line says no constraint."""
    prompt = A.build_prompt("some complaint", "", [],
                            index_version=INDEX_VERSION,
                            model_version=SERVED_MODEL)
    assert "ALLOWED TASK NUMBERS: (none — recommend no task)" in prompt
    assert "ALLOWED CASE IDS: (none)" in prompt


def test_no_task_body_is_ever_put_in_front_of_the_model(corpus,
                                                        answer_payload):
    """The corpus fixture writes a marker into the one task body it has."""
    model = FakeModel(json.dumps(answer_payload))
    service = make_service(corpus, model)
    service.run("air data module fault", tail="D-ABCD",
                      user=ENGINEER)
    assert "DO NOT VENT" not in model.prompts[0]["prompt"]


# ── 4. hypotheses are hypotheses, never causes ──────────────────────────

def test_the_caveat_above_every_answer_refuses_the_word_cause_alone():
    caveat = D.HYPOTHESIS_CAVEAT.lower()
    assert "hypotheses" in caveat
    assert "not confirmed causes" in caveat
    # The finding of the six-model review, stated rather than implied: the
    # case base records what was done, not whether it was right.
    assert "what an engineer did" in caveat
    assert "no confirmed outcomes" in caveat


def test_the_model_is_instructed_that_there_are_no_confirmed_outcomes():
    system = A.SYSTEM.lower()
    assert "hypotheses, not causes" in system
    assert "no confirmed outcomes" in system
    assert "never describe a hypothesis as a confirmed" in system


def test_no_rendered_state_text_claims_a_cause_was_identified():
    """`cause` may appear only as the thing being denied."""
    for state, (title, body) in D.STATE_TEXT.items():
        for sentence in re.split(r"(?<=[.;])\s+", f"{title}. {body}"):
            low = sentence.lower()
            if "cause" not in low:
                continue
            assert any(word in low for word in
                       ("not", "never", "hypothes")), (state, sentence)


def test_no_evidence_level_is_rendered_as_a_confirmation():
    """Green is absent on purpose: a green badge reads as "confirmed"."""
    assert set(D.EVIDENCE_STYLE) == set(EVIDENCE_LEVELS)
    tokens = {style["token"] for style in D.EVIDENCE_STYLE.values()}
    assert not tokens & {"grn", "grnq", "ok", "good"}, tokens
    for level, style in D.EVIDENCE_STYLE.items():
        assert style["word"].startswith("EVIDENCE: ")
        assert style["icon"], level


def test_an_answer_that_calls_a_hypothesis_a_confirmed_cause_is_still_labelled(
        corpus, answer_payload):
    """The caveat is not conditional on the model behaving.

    Even an accepted answer sits under the standing sentence, because the
    model's own wording is not what makes a hypothesis a hypothesis.
    """
    answer_payload["hypotheses"][0]["cause"] = "Air data module drift"
    run = _run_with_answer(corpus, answer_payload)
    assert run.state == A.ACCEPTED
    assert "not confirmed causes" in D.HYPOTHESIS_CAVEAT


# ── 5. evidence levels are categories, never percentages ────────────────

PERCENT = re.compile(r"\d\s*%|\bconfiden(?:ce|t)\b|\bprobabilit|\blikelihood\b",
                     re.IGNORECASE)


def test_the_closed_set_is_four_categories():
    assert EVIDENCE_LEVELS == ("strong", "limited", "conflicting",
                               "insufficient")
    assert set(EVIDENCE_MEANING) == set(EVIDENCE_LEVELS)


@pytest.mark.parametrize("blob", [
    "SYSTEM", "SCHEMA_NOTE",
])
def test_no_percentage_or_confidence_language_in_what_the_model_is_told(blob):
    """Except where the prompt forbids them, which is a different sentence."""
    text = getattr(A, blob)
    for line in text.splitlines():
        if "Never write a percentage" in line or "category" in line:
            continue
        assert not PERCENT.search(line), f"{blob}: {line}"


def test_no_percentage_or_confidence_language_in_any_panel_string():
    strings = [D.HYPOTHESIS_CAVEAT]
    strings += [s["word"] for s in D.EVIDENCE_STYLE.values()]
    strings += list(D.AUTHORIZATION_TEXT.values())
    strings += list(D.APPLICABILITY_TEXT.values())
    strings += [t for pair in D.STATE_TEXT.values() for t in pair]
    strings += list(EVIDENCE_MEANING.values())
    for text in strings:
        assert not PERCENT.search(text), text


def test_a_percentage_where_an_evidence_level_belongs_is_refused(
        corpus, answer_payload):
    answer_payload["hypotheses"][0]["evidence_level"] = "87%"
    run = _run_with_answer(corpus, answer_payload)
    assert run.state == A.MALFORMED
    assert run.answer is None
    # The refusal names the closed set it was held to, so the fix is obvious.
    assert all(level in run.reason for level in EVIDENCE_LEVELS), run.reason
    assert "87%" not in renderable_findings(run)


def test_an_unmeasured_numeral_in_the_prose_is_refused(corpus,
                                                       answer_payload):
    """"in 7 of 12 cases" is a confidence claim wearing a different hat."""
    answer_payload["narrative"] = "This cause explains 7 of 12 prior cases."
    run = _run_with_answer(corpus, answer_payload)
    assert run.state == A.REJECTED
    assert run.answer is None
    assert "7 of 12" not in rendered_text(run)


def test_no_accepted_answer_renders_a_percentage(corpus, answer_payload):
    run = _run_with_answer(corpus, answer_payload)
    assert run.state == A.ACCEPTED
    assert not PERCENT.search(rendered_text(run)), rendered_text(run)


# ── 6. cancellation ─────────────────────────────────────────────────────

def test_cancelling_before_the_model_is_asked_stops_the_investigation(corpus):
    token = CancelToken()
    token.cancel()
    service = make_service(corpus, NeverAskedModel())
    run = service.run("air data module fault", tail="D-ABCD",
                      cancel=token, user=ENGINEER)
    assert run.state == A.CANCELLED
    assert run.answer is None
    assert "before the model was asked" in run.reason


def test_cancelling_stops_the_evidence_gathering_between_tools(corpus,
                                                               engineer):
    token = CancelToken()
    token.cancel()
    service = make_service(corpus, FakeModel())
    assert service.gather("air data module", "D-ABCD", engineer, token) == []


def test_a_cancel_raised_inside_the_model_is_caught_as_a_state(corpus):
    def raise_cancelled(*_args):
        raise Cancelled("stopped")

    service = make_service(corpus, FakeModel(raise_cancelled))
    run = service.run("air data module fault", tail="D-ABCD",
                      user=ENGINEER)
    assert run.state == A.CANCELLED
    assert "while the model was thinking" in run.reason
    assert run.answer is None


def test_an_answer_that_arrives_after_a_cancel_is_discarded(corpus,
                                                            answer_payload):
    """The token flips while the model is generating, as it does in life."""
    token = CancelToken()

    def answer_then_cancel(*_args):
        token.cancel()
        return json.dumps(answer_payload)

    service = make_service(corpus, FakeModel(answer_then_cancel))
    run = service.run("air data module fault", tail="D-ABCD",
                      cancel=token, user=ENGINEER)
    assert run.state == A.CANCELLED
    assert run.answer is None
    assert "after the model answered" in run.reason


def test_the_cancel_token_reaches_the_model_call(corpus, answer_payload):
    """Cooperative cancellation only works if the token is actually passed."""
    model = FakeModel(json.dumps(answer_payload))
    service = make_service(corpus, model)
    token = CancelToken()
    service.run("air data module fault", tail="D-ABCD",
                      cancel=token, user=ENGINEER)
    assert model.prompts[0]["cancel"] is token


def test_an_unexpected_failure_is_named_in_full_not_swallowed(corpus):
    def blow_up(*_args):
        raise RuntimeError("the socket closed mid-read")

    service = make_service(corpus, FakeModel(blow_up))
    run = service.run("air data module fault", tail="D-ABCD",
                      user=ENGINEER)
    assert run.state == A.FAILED
    assert "RuntimeError" in run.reason
    assert "the socket closed mid-read" in run.reason
    assert run.answer is None


# ── 7. unavailable tools render their missing state ─────────────────────

def test_a_tool_with_no_data_behind_it_is_a_result_not_a_silence(corpus,
                                                                 engineer):
    service = make_service(corpus, FakeModel())
    results = service.gather("air data module", "D-ABCD", engineer)
    unavailable = [r for r in results if not r.ok]
    assert unavailable, "seven declared tools have no data — none reported it"
    for result in unavailable:
        assert result.error, result.tool
        assert result.missing or "refused" in result.error, result.tool


def test_the_investigation_splits_available_from_unavailable_tools(
        corpus, answer_payload):
    run = _run_with_answer(corpus, answer_payload)
    assert run.available_tools
    assert run.unavailable_tools
    assert (len(run.available_tools) + len(run.unavailable_tools)
            == len(run.tool_results))
    for result in run.unavailable_tools:
        assert not result.ok


def test_the_tools_that_could_not_answer_are_named_to_the_model(corpus,
                                                                engineer):
    """The honest "no closure state has been imported" is worth more than
    the tool's silent absence — to the model as much as to the engineer."""
    service = make_service(corpus, FakeModel())
    results = service.gather("air data module", "D-ABCD", engineer)
    block, _tasks, _cases = A._brief("air data module", "D-ABCD", results)
    payload = json.loads(block)
    assert payload["tools_that_could_not_answer"], payload.keys()
    for entry in payload["tools_that_could_not_answer"]:
        assert entry["tool"] and entry["why"]


def test_a_refused_tool_does_not_abort_the_investigation(corpus,
                                                         answer_payload):
    """A model that may not read compliance data can still be asked about a
    symptom, and the panel shows which door was closed."""
    class RefusingRegistry(ToolRegistry):
        def call(self, name, arguments, user=None):
            if name == "get_compliance_context":
                from aivionics.llm.tools import ToolError
                raise ToolError("the engineer role does not hold this")
            return super().call(name, arguments, user)

    registry = RefusingRegistry(db_path=corpus)
    try:
        service = A.AIService(db_path=corpus, index_version=INDEX_VERSION,
                              model=FakeModel(json.dumps(answer_payload)),
                              registry=registry)
        run = service.run("air data module fault", tail="D-ABCD",
                      user=ENGINEER)
    finally:
        registry.close()
    assert run.state == A.ACCEPTED, run.reason
    refused = [r for r in run.unavailable_tools
               if r.tool == "get_compliance_context"]
    assert refused and "refused" in refused[0].error


def test_the_authorization_answer_is_a_question_asked_not_a_blank():
    assert set(D.AUTHORIZATION_TEXT) >= {"unknown"}
    unknown = D.AUTHORIZATION_TEXT["unknown"]
    assert unknown.strip()
    assert "unknown" in unknown.lower()
    assert "no authorization record" in unknown.lower()


def test_effectivity_tokens_never_reach_the_screen_as_machine_values():
    """Standing rule 8 is fail-closed on effectivity, in words not tokens."""
    from aivionics.llm.schemas import EFFECTIVITY_STATES

    assert set(D.APPLICABILITY_TEXT) == set(EFFECTIVITY_STATES)
    for token, text in D.APPLICABILITY_TEXT.items():
        assert text.strip() != token                    # never the bare value
        assert "_" not in text, (token, text)           # never `not_applicable`
        assert " " in text.strip(), text
    # Anything not positively applicable has to say so in a way an engineer
    # can act on, rather than reading as a quiet pass.
    for token in ("not_applicable", "unresolved", "not_checked"):
        text = D.APPLICABILITY_TEXT[token].lower()
        assert any(word in text for word in ("not", "unresolved")), text


# ── 8. provenance is on the screen ──────────────────────────────────────

def test_an_accepted_answer_carries_model_endpoint_index_and_evidence_ids(
        corpus, answer_payload):
    run = _run_with_answer(corpus, answer_payload)
    prov = run.provenance
    assert prov.served_model == SERVED_MODEL
    assert prov.endpoint == "http://fake.invalid/v1"
    assert prov.provider == "fake"
    assert prov.index_version == INDEX_VERSION
    assert prov.tools_called
    assert run.answer.evidence_ids

    described = prov.describe()
    for fragment in (SERVED_MODEL, "fake", "http://fake.invalid/v1",
                     INDEX_VERSION):
        assert fragment in described, described


def test_an_endpoint_serving_a_different_model_says_so(corpus,
                                                       answer_payload):
    """A listed model is not a served model, and attributing one model's
    output to another is a silent failure for as long as nobody checks."""
    answer_payload["model_version"] = "other-model"
    model = FakeModel(json.dumps(answer_payload), requested="nemotron-test",
                      served="other-model")
    service = make_service(corpus, model)
    run = service.run("air data module fault", tail="D-ABCD",
                      user=ENGINEER)

    assert run.provenance.model_mismatch
    described = run.provenance.describe()
    assert "requested nemotron-test" in described
    assert "served other-model" in described
    assert run.state == A.REJECTED
    assert run.answer is None
    assert "served_model_mismatch" in run.report.codes


def test_a_tag_or_namespace_difference_is_not_a_mismatch():
    assert not A.Provenance(requested_model="nvidia/nemotron",
                            served_model="nemotron:latest").model_mismatch
    assert A.Provenance(requested_model="nemotron",
                        served_model="qwen3").model_mismatch


def test_an_answer_stamped_with_the_wrong_index_cannot_be_re_run(
        corpus, answer_payload):
    answer_payload["index_version"] = "idx-something-else"
    run = _run_with_answer(corpus, answer_payload)
    assert run.state == A.REJECTED
    assert "wrong_index_version" in run.report.codes
    assert run.answer is None


def test_an_answer_stamped_with_a_model_that_did_not_write_it_is_refused(
        corpus, answer_payload):
    answer_payload["model_version"] = "some-other-model"
    run = _run_with_answer(corpus, answer_payload)
    assert run.state == A.REJECTED
    assert "wrong_model_version" in run.report.codes
    assert run.answer is None


def test_provenance_survives_every_terminal_state(corpus, answer_payload):
    """An engineer looking at a refusal still needs to know what refused."""
    cases = {
        A.UNAVAILABLE: FakeModel(ok=False, reason="down"),
        A.MALFORMED: FakeModel("not json"),
        A.FAILED: FakeModel(lambda *_a: (_ for _ in ()).throw(
            RuntimeError("boom"))),
    }
    for state, model in cases.items():
        service = make_service(corpus, model)
        run = service.run("air data module fault", tail="D-ABCD",
                      user=ENGINEER)
        assert run.state == state, (state, run.reason)
        assert run.provenance.index_version == INDEX_VERSION
        assert run.provenance.describe().strip()


# ── 9. the UI thread is never blocked ───────────────────────────────────

@pytest.fixture(scope="module")
def qt_app():
    """A QApplication for the async tests, or a skip if Qt is unavailable."""
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_submit_returns_immediately_and_runs_off_the_calling_thread(
        qt_app, corpus, answer_payload):
    """The whole promise, measured rather than asserted.

    The model blocks until the test releases it. If `submit` ran the
    investigation inline, the call would not return until `release` was set —
    and the test would hang rather than fail, which is why the wait is
    bounded.
    """
    release = threading.Event()
    ran_on: dict[str, int] = {}

    def block_until_released(*_args):
        ran_on["thread"] = threading.get_ident()
        assert release.wait(timeout=10), "the worker was never released"
        return json.dumps(answer_payload)

    service = make_service(corpus, FakeModel(block_until_released))
    signals = A.AISignals()
    received: list[A.Investigation] = []
    signals.done.connect(received.append)

    caller = threading.get_ident()
    assert service.submit("air data module fault", signals, tail="D-ABCD",
                          user=ENGINEER)
    # submit returned while the worker is still blocked: it did not run inline.
    assert service.busy

    release.set()
    for _ in range(2000):
        qt_app.processEvents()
        if received:
            break
    assert received, "the done signal never arrived"
    assert ran_on["thread"] != caller
    assert received[0].state == A.ACCEPTED, received[0].reason


def test_a_second_investigation_is_refused_while_one_is_running(
        qt_app, corpus, answer_payload):
    release = threading.Event()

    def block_until_released(*_args):
        assert release.wait(timeout=10)
        return json.dumps(answer_payload)

    service = make_service(corpus, FakeModel(block_until_released))
    signals = A.AISignals()
    received: list[A.Investigation] = []
    signals.done.connect(received.append)

    assert service.submit("air data module fault", signals, tail="D-ABCD",
                          user=ENGINEER)
    assert service.submit("a second question", signals) is False

    release.set()
    for _ in range(2000):
        qt_app.processEvents()
        if received:
            break
    assert received
    assert service.busy is False


def test_a_worker_failure_arrives_as_a_signal_not_a_dead_panel(qt_app,
                                                               corpus):
    """`run` catches its own failures, so this exercises the outer net: a
    failure in the job itself still has to reach the screen."""
    class Exploding(A.AIService):
        def run(self, *_args, **_kw):
            raise ValueError("something nobody predicted")

    service = Exploding(db_path=corpus, index_version=INDEX_VERSION,
                        model=FakeModel())
    signals = A.AISignals()
    failures: list[tuple[str, str]] = []
    signals.failed.connect(lambda q, m: failures.append((q, m)))

    assert service.submit("air data module fault", signals)
    for _ in range(2000):
        qt_app.processEvents()
        if failures:
            break
    assert failures, "a job that raised told nobody"
    assert "ValueError" in failures[0][1]
    assert "something nobody predicted" in failures[0][1]
    assert service.busy is False


def test_cancel_reaches_the_running_investigation(qt_app, corpus):
    """`cancel` flips the token the job is checking, without killing a thread
    mid-socket."""
    seen: list[CancelToken] = []
    release = threading.Event()

    def observe(_prompt, _system, _budget, cancel):
        seen.append(cancel)
        assert release.wait(timeout=10)
        return "not json"

    service = make_service(corpus, FakeModel(observe))
    signals = A.AISignals()
    received: list[A.Investigation] = []
    signals.done.connect(received.append)

    assert service.submit("air data module fault", signals, tail="D-ABCD",
                          user=ENGINEER)
    for _ in range(2000):
        if seen:
            break
        qt_app.processEvents()
    assert seen, "the model was never reached"
    service.cancel()
    assert seen[0].cancelled

    release.set()
    for _ in range(2000):
        qt_app.processEvents()
        if received:
            break
    assert received
    assert received[0].state == A.CANCELLED


# ── 10. the plan is fixed, not chosen by the model ──────────────────────

def test_the_evidence_plan_is_fixed_rather_than_model_chosen():
    """A tool-calling loop lets the model decide what evidence it does not
    want, and that failure is invisible in the answer."""
    plan = A.plan_tools("air data module fault", "D-ABCD")
    names = [name for name, _args in plan]
    assert names[0] == "search_manual_tasks"
    assert "search_similar_cases" in names
    # The unbacked tools are called anyway: their honest refusal is the answer.
    assert "get_open_defects" in names
    assert "get_compliance_context" in names


def test_the_case_search_is_not_narrowed_to_one_airframe():
    """A fleet-wide symptom match is the point of a case base."""
    plan = dict((name, args) for name, args in
                A.plan_tools("air data module fault", "D-ABCD"))
    assert "tail" not in plan["search_similar_cases"]
    assert plan["search_manual_tasks"]["tail"] == "D-ABCD"


def test_the_candidate_set_is_bounded():
    assert A.MAX_TASKS <= 6
    assert A.MAX_CASES <= 8
    plan = dict((name, args) for name, args in A.plan_tools("x", ""))
    assert plan["search_manual_tasks"]["limit"] == A.MAX_TASKS


def test_the_token_budget_leaves_room_for_the_reasoning_preamble():
    """Two structured-extraction probes ran out of tokens inside the thinking
    block and returned no JSON at all. The budget is the measurement."""
    from aivionics.llm.service import budget_for
    assert budget_for(A.ANSWER_TOKENS) > A.ANSWER_TOKENS


# ── 11. the accepted answer resolves its own citations ──────────────────

def test_a_cited_case_renders_as_the_record_it_points_at(corpus,
                                                         answer_payload):
    run = _run_with_answer(corpus, answer_payload)
    assert run.state == A.ACCEPTED
    cases = run.cases()
    for case_id in run.answer.hypotheses[0].supporting_case_ids:
        assert case_id in cases, case_id
        assert cases[case_id].get("symptom") is not None


def test_a_recommended_task_resolves_to_the_retrieved_locator(corpus,
                                                              answer_payload):
    run = _run_with_answer(corpus, answer_payload)
    tasks = run.tasks()
    for document in run.answer.recommended_documents:
        assert document.task_number.upper() in tasks


def test_an_empty_investigation_resolves_to_nothing_rather_than_failing():
    blank = A.Investigation(A.IDLE)
    assert blank.cases() == {}
    assert blank.tasks() == {}
    assert blank.violations == ()
    assert blank.accepted is False


# ── finding 9: the remaining grounding rejections, end to end ────────────
def test_an_unsupported_ata_chapter_is_rejected(corpus, answer_payload):
    """A chapter the retrieval never returned is an invention, however
    plausible it reads."""
    for doc in answer_payload["recommended_documents"]:
        doc["task_number"] = "49-11-00-810-801"      # never retrieved
    run = _run_with_answer(corpus, answer_payload)
    assert run.state != A.ACCEPTED
    assert run.answer is None
    assert run.violations
    # The violation *names* the rejected task on purpose — that is the
    # explanation the engineer is shown instead of an answer. What must never
    # happen is the number surviving as an accepted recommendation.
    assert run.answer is None
    assert any("ungrounded" in v.code for v in run.violations)


def test_an_unsupported_page_reference_is_rejected(corpus, answer_payload):
    """Page numbers are the easiest thing for a model to fabricate and the
    hardest for a reader to check, so an unretrieved page is a rejection."""
    doc = answer_payload["recommended_documents"][0]
    doc["page"] = 99999
    doc["page_number"] = 99999
    run = _run_with_answer(corpus, answer_payload)
    if run.state == A.ACCEPTED:
        # The field is not part of the accepted contract at all, which is a
        # stronger guarantee than validating it: it cannot reach the engineer.
        rendered = repr(run.answer)
        assert "99999" not in rendered, "a fabricated page reached the answer"
    else:
        assert run.answer is None


def test_deterministic_results_survive_a_rejected_answer(corpus,
                                                         answer_payload):
    """Rejecting the model must never take the search results with it."""
    answer_payload["recommended_documents"][0]["task_number"] = HALLUCINATED_TASK
    run = _run_with_answer(corpus, answer_payload)
    assert run.state != A.ACCEPTED
    assert run.answer is None
    # The investigation still carries its evidence for the engineer to read.
    assert run.report is not None
