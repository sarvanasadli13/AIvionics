"""The LLM layer, which is optional and must never be trusted (PLAN Phase 5).

No network and no model download here: the client is injected. What is tested
is the validator, because it is the only thing standing between a plausible
sentence and a fabricated maintenance claim.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aivionics.llm import summarise
from aivionics.llm.client import LLMConfig, LLMUnavailable

CASES = [
    {"tail": "N101AV", "reported_at": "2025-03-04",
     "replaced": "REPLACED PITOT PROBE PER AMM 34-11-01-400-801",
     "found": "not recorded"},
    {"tail": "N202AV", "reported_at": "2025-04-11",
     "replaced": "PERFORMED PITOT STATIC LEAK CHECK. NO FAULT",
     "found": "FOUND CHAFED WIRE AT CONNECTOR"},
]
ALLOWED = {"34-11-01-400-801"}


class FakeClient:
    """Returns whatever it was handed. `config` mirrors the real client."""

    def __init__(self, reply="", raises=None):
        self.reply = reply
        self.raises = raises
        self.config = LLMConfig()
        self.prompts: list[str] = []

    def generate(self, prompt, *, system=None):
        self.prompts.append(prompt)
        if self.raises:
            raise self.raises
        return self.reply


# ── the validator ────────────────────────────────────────────────────────
def test_a_grounded_summary_is_accepted():
    out = summarise.validate(
        "The pitot probe was replaced on one aircraft and a leak check found a "
        "chafed wire at the connector on another.",
        sources=[c["replaced"] + " " + c["found"] for c in CASES],
        allowed_tasks=ALLOWED)
    assert out.accepted and not out.violations


def test_a_numeral_absent_from_the_records_is_rejected():
    """Standing rule 4: every numeral in the UI comes from the database. A
    model writing '7 of 12 removals' is generating a statistic."""
    out = summarise.validate(
        "In 7 of 12 cases the probe was replaced.",
        sources=["REPLACED PITOT PROBE"], allowed_tasks=ALLOWED)
    assert not out.accepted
    assert any("7" in v for v in out.violations)


def test_small_counts_that_are_ordinary_english_are_allowed():
    out = summarise.validate("Both cases were resolved; 2 aircraft involved.",
                             sources=["REPLACED PROBE"], allowed_tasks=set())
    assert out.accepted


def test_a_task_number_that_was_never_retrieved_is_rejected():
    out = summarise.validate(
        "Refer to AMM 27-51-00-810-999 for the procedure.",
        sources=["REPLACED PITOT PROBE PER AMM 34-11-01-400-801"],
        allowed_tasks=ALLOWED)
    assert not out.accepted
    assert any("27-51-00-810-999" in v for v in out.violations)


def test_a_task_number_present_in_the_sources_is_allowed():
    out = summarise.validate(
        "One case cites 34-11-01-400-801.",
        sources=["REPLACED PITOT PROBE PER AMM 34-11-01-400-801"],
        allowed_tasks=set())
    assert out.accepted


def test_an_invented_safety_callout_is_rejected():
    """Safety text comes from the manual, never from a model."""
    out = summarise.validate(
        "WARNING: depressurise the system before removal.",
        sources=["REPLACED PITOT PROBE"], allowed_tasks=ALLOWED)
    assert not out.accepted
    assert any("WARNING" in v for v in out.violations)


def test_declining_to_answer_is_a_valid_answer():
    out = summarise.validate(summarise.INSUFFICIENT, sources=["x"],
                             allowed_tasks=set())
    assert out.accepted and out.insufficient


def test_an_empty_reply_is_not_accepted():
    assert not summarise.validate("", sources=["x"]).accepted


def test_a_violating_summary_is_dropped_not_repaired():
    """Editing the output would leave a sentence that reads as grounded when
    part of it was silently removed."""
    bad = "In 9 of 40 cases the probe failed."
    out = summarise.validate(bad, sources=["REPLACED PROBE"])
    assert not out.accepted
    assert out.text == bad            # unchanged, and unused


# ── the prompt ───────────────────────────────────────────────────────────
def test_the_prompt_carries_only_case_records():
    """Standing rule 3: procedural text never reaches the model."""
    prompt, sources = summarise.build_prompt("AIRSPEED UNRELIABLE", CASES)
    assert "AIRSPEED UNRELIABLE" in prompt
    assert len(sources) == 2
    for case in CASES:
        assert case["tail"] in prompt
    assert "REMOVE THE PROBE" not in prompt.upper()


def test_missing_fields_render_as_not_recorded():
    prompt, _ = summarise.build_prompt("X", [{"tail": "N1"}])
    assert prompt.count("not recorded") >= 2


# ── the optional path ────────────────────────────────────────────────────
def test_no_model_configured_is_an_ordinary_outcome():
    out = summarise.summarise_cases(None, "q", CASES)
    assert not out.accepted
    assert "no language model" in out.reason


def test_an_unreachable_model_does_not_raise():
    """The application is required to work without one."""
    out = summarise.summarise_cases(
        FakeClient(raises=LLMUnavailable("connection refused")), "q", CASES)
    assert not out.accepted
    assert "unavailable" in out.reason


def test_an_unexpected_client_error_is_contained():
    out = summarise.summarise_cases(
        FakeClient(raises=RuntimeError("boom")), "q", CASES)
    assert not out.accepted
    assert "boom" in out.reason


def test_no_cases_means_no_summary():
    out = summarise.summarise_cases(FakeClient("anything"), "q", [])
    assert not out.accepted


def test_end_to_end_accepts_a_grounded_model_reply():
    client = FakeClient(
        "A pitot probe was replaced on one aircraft. On another a leak check "
        "found a chafed wire at the connector.")
    out = summarise.summarise_cases(client, "AIRSPEED UNRELIABLE", CASES,
                                    allowed_tasks=ALLOWED)
    assert out.accepted
    assert out.model == LLMConfig().model


def test_end_to_end_rejects_a_fabricating_model_reply():
    client = FakeClient(
        "In 47 of 91 cases the ADIRU was at fault; see 22-11-00-810-777.")
    out = summarise.summarise_cases(client, "AIRSPEED UNRELIABLE", CASES,
                                    allowed_tasks=ALLOWED)
    assert not out.accepted
    assert len(out.violations) >= 2
