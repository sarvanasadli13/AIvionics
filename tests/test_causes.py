"""Phase 5 — probable-cause candidates.

All logic, no Qt and no socket: every test runs against a fixture database
built by ``db.connect(tmp_path/...)``. The real 2.55 GB corpus is never opened
and nothing here writes to it.

The tests that matter most are the ones guarding a claim the product makes
about itself: that a candidate is assembled from rows rather than recalled by
a model, that a case arguing *against* a hypothesis is surfaced as loudly as
one arguing for it, that a rank can be taken apart feature by feature, and
that nothing in the API ever offers a learned root-cause probability — because
``defect_closure`` has no rows and there is nothing to learn one from.
"""
import inspect
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aivionics import db
from aivionics.llm.schemas import (CONFLICTING, EVIDENCE_LEVELS, INSUFFICIENT,
                                   LIMITED, MAX_RANK, STRONG, CauseCandidate,
                                   parse_cause_candidate)
from aivionics.stats import causes, schema
from aivionics.stats.casebase import CONFIRMED_FAULT, NO_FAULT_FOUND


# ── fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture()
def con(tmp_path):
    c = db.connect(tmp_path / "causes.db")
    schema.ensure(c)
    yield c
    c.close()


SYMPTOM = "CAPT AIRSPEED UNRELIABLE"


def add_defect(con, did, *, tail="N101AA", ata="34", at="2025-03-01",
               defect_text=SYMPTOM, source="sdr"):
    con.execute(
        "INSERT INTO defect(id,aircraft_tail,reported_at,ata_ref,defect_text,"
        "source,sdr_year) VALUES(?,?,?,?,?,?,2025)",
        (did, tail, at, ata, defect_text, source))
    return did


def add_action(con, did, *, action_type="replaced", part_name="PITOT PROBE",
               part_number="PN-1", task_number=None):
    con.execute("INSERT INTO defect_action(defect_id,action_type,part_name,"
                "part_number,task_number) VALUES(?,?,?,?,?)",
                (did, action_type, part_name, part_number, task_number))


def add_finding(con, did, finding_type=CONFIRMED_FAULT, text="FOUND CORRODED"):
    con.execute("INSERT INTO defect_finding(defect_id,finding_type,"
                "finding_text,found_at,source) VALUES(?,?,?,?,'sdr_mined')",
                (did, finding_type, text, "2025-03-01"))


# Fifteen real avionics LRUs, every one a single token. Single tokens on
# purpose: `normalise_cause` sorts the tokens of a name before joining them,
# so a two-word part comes back alphabetised ("STATIC PORT" displays as
# "PORT STATIC"). That is correct for *grouping* and surprising in a fixture,
# so these names sidestep it. Verified: all fifteen normalise distinctly.
DISTINCT_PARTS = ("ADIRU", "ALTIMETER", "TRANSPONDER", "RADALT", "DME",
                  "VOR", "ILS", "TCAS", "EGPWS", "FMC", "IRU", "AHRS",
                  "ACARS", "ELT", "CVR")


def add_repeat(con, first, second, days=5, similarity=0.9, ata="34"):
    """Link two defects as a recurrence, creating the second if it is absent.

    `repeat_norm.repeat_defect_id` is a foreign key, and rightly so: a repeat
    IS another defect, not a bare identifier. A fixture that inserts the link
    without the defect is asserting something the schema does not allow to
    exist, so the helper creates the far end rather than the test pretending
    it is there.
    """
    if con.execute("SELECT 1 FROM defect WHERE id=?", (second,)).fetchone() is None:
        add_defect(con, second, tail="N999ZZ", ata=ata)
    con.execute("INSERT INTO repeat_norm(defect_id,repeat_defect_id,days_apart,"
                "similarity,same_action,ata_chapter) VALUES(?,?,?,?,0,?)",
                (first, second, days, similarity, ata))


def case(con, did, *, tail="N101AA", part_name="PITOT PROBE",
         part_number="PN-1", action_type="replaced", finding=None,
         task_number=None, ata="34", defect_text=SYMPTOM):
    """One complete prior case: a defect, an action, optionally a finding."""
    add_defect(con, did, tail=tail, ata=ata, defect_text=defect_text)
    add_action(con, did, action_type=action_type, part_name=part_name,
               part_number=part_number, task_number=task_number)
    if finding:
        add_finding(con, did, finding)
    return did


def request(**kwargs):
    kwargs.setdefault("symptom", SYMPTOM)
    kwargs.setdefault("ata_chapter", "34")
    return causes.CauseRequest(**kwargs)


# ── the central claim ────────────────────────────────────────────────────

def test_the_api_calls_its_output_evidence_supported_hypotheses(con):
    """The wording is the product's honesty, so it is pinned by a test."""
    case(con, 1)
    report = causes.propose_causes(con, request())
    assert "evidence-supported hypotheses" in report.claim
    assert "not a learned root-cause probability" in report.claim


def test_no_candidate_offers_a_probability_or_a_confidence(con):
    """No percentage, no 0..1 confidence, nowhere in the emitted object.

    A ranking scalar that lands in 0..1 gets read as a probability whatever
    the docstring says, so the score is an unbounded weighted count and the
    words 'probability' and 'confidence' may not appear as a field anywhere.
    """
    for i, tail in enumerate(("N1", "N2", "N3"), start=1):
        case(con, i, tail=tail, finding=CONFIRMED_FAULT)
    report = causes.propose_causes(con, request())
    assert report.candidates
    for candidate in report.candidates:
        keys = set(candidate.provenance)
        assert not {"probability", "confidence", "likelihood"} & keys
        assert candidate.evidence_level in EVIDENCE_LEVELS
    # The score is a sum of weighted counts and is free to exceed 1, which is
    # what stops it reading as a probability.
    assert report.scored[0].score > 1.0
    assert "not a fit to outcomes" in report.scored[0].score_meaning


def test_the_module_never_claims_a_learned_or_predicted_cause():
    """The vocabulary of learning has no place in a module with no outcomes."""
    source = Path(causes.__file__).read_text(encoding="utf-8")
    rendered = " ".join(
        [causes.CLAIM, causes.SCORE_MEANING, causes.REPORT_CAVEAT]
        + [detail for _, detail in causes.WEIGHTS.values()])
    for word in ("predicted cause", "root cause is", "trained", "learned from"):
        assert word not in rendered.lower()
    # And no training table was invented on the way past — Phase 6 is blocked.
    assert "CREATE TABLE" not in source.upper()


# ── stage 1 · candidates come from rows, and only from rows ─────────────

def test_a_candidate_exists_only_where_an_action_row_exists(con):
    """A defect with no recorded action implicates no component at all."""
    add_defect(con, 1)                       # narrative only, no action row
    raw, census = causes.generate_candidates(con, request())
    assert raw == ()
    assert census["cases_matched"] == 1      # the case was seen and rejected


def test_a_candidate_carries_the_ids_of_the_rows_that_built_it(con):
    case(con, 11, tail="N1")
    case(con, 12, tail="N2")
    raw, _ = causes.generate_candidates(con, request())
    assert len(raw) == 1
    assert raw[0].supporting_case_ids == (11, 12)
    assert raw[0].evidence_ids == ("defect:11", "defect:12")


def test_no_candidate_survives_without_supporting_evidence(con):
    """Every case argues against the component, so there is no hypothesis.

    Not a candidate marked `insufficient` to look accounted for — the Phase 2
    schema refuses an empty support list and that refusal is the right one.
    """
    case(con, 21, finding=NO_FAULT_FOUND)
    case(con, 22, finding=NO_FAULT_FOUND)
    raw, _ = causes.generate_candidates(con, request())
    assert raw == ()
    report = causes.propose_causes(con, request())
    assert report.candidates == ()
    assert report.evidence_level == INSUFFICIENT


def test_every_emitted_candidate_satisfies_the_phase_two_schema(con):
    """Round-tripped through `parse_cause_candidate`, which rejects rather
    than repairs — so this fails if anything here drifts from Phase 2."""
    for i, tail in enumerate(("N1", "N2", "N3"), start=1):
        case(con, i, tail=tail, finding=CONFIRMED_FAULT)
    case(con, 9, part_name="STATIC PORT", part_number="PN-9")
    report = causes.propose_causes(con, request())
    assert report.candidates
    for candidate in report.candidates:
        payload = {
            "cause_id": candidate.cause_id, "cause": candidate.cause,
            "rank": candidate.rank, "evidence_level": candidate.evidence_level,
            "supporting_case_ids": list(candidate.supporting_case_ids),
            "contradicting_case_ids": list(candidate.contradicting_case_ids),
            "limitations": list(candidate.limitations),
            "provenance": candidate.provenance,
        }
        assert isinstance(parse_cause_candidate(payload), CauseCandidate)


def test_an_unrelated_symptom_in_the_same_chapter_is_not_evidence(con):
    """The chapter is a prefilter, never the match. Phase 3.2's similarity is."""
    case(con, 1, defect_text="CARGO DOOR SEAL TORN", part_name="DOOR SEAL")
    raw, census = causes.generate_candidates(con, request())
    assert raw == ()
    assert census["cases_examined"] == 1 and census["cases_matched"] == 0


def test_a_caller_may_hand_in_its_own_case_ids(con):
    """The decoupled path: retrieval chooses the cases, this module does not."""
    case(con, 5, ata="21", defect_text="PACK TRIP")   # another chapter entirely
    raw, _ = causes.generate_candidates(
        con, causes.CauseRequest(symptom=SYMPTOM, case_ids=(5,)))
    assert [c.supporting_case_ids for c in raw] == [(5,)]


def test_a_query_with_neither_chapter_nor_tail_refuses_to_scan(con):
    """1.75 M rows with no handle on them is not a search."""
    case(con, 1)
    raw, census = causes.generate_candidates(
        con, causes.CauseRequest(symptom=SYMPTOM))
    assert raw == () and census["cases_examined"] == 0


def test_the_scan_cap_is_reported_rather_than_hidden(con):
    """`repeat.py`'s discipline: say how much was truncated, never under-count."""
    for i in range(1, 7):
        case(con, i, tail=f"N{i}")
    raw, census = causes.generate_candidates(con, request(max_scan=3))
    assert census["truncated"] is True
    assert census["cases_examined"] == 3
    assert len(raw[0].supporting_case_ids) == 3


# ── stage 1 · contradiction is first-class ──────────────────────────────

def test_a_no_fault_found_case_contradicts_the_component(con):
    case(con, 1, tail="N1", finding=CONFIRMED_FAULT)
    case(con, 2, tail="N2", finding=NO_FAULT_FOUND)
    raw, _ = causes.generate_candidates(con, request())
    assert raw[0].supporting_case_ids == (1,)
    assert raw[0].contradicting_case_ids == (2,)


def test_a_replacement_followed_by_a_repeat_contradicts_the_component(con):
    """The harder contradiction: the record shows the swap did not hold."""
    case(con, 1, tail="N1")
    case(con, 2, tail="N2", action_type="removed_replaced")
    add_repeat(con, 2, 99, days=5)
    raw, _ = causes.generate_candidates(con, request())
    assert raw[0].contradicting_case_ids == (2,)
    reason = raw[0].contradicting[0].contradiction_reason
    assert "recurred" in reason


def test_a_repeat_outside_the_window_is_not_a_contradiction(con):
    case(con, 1, tail="N1", action_type="removed_replaced")
    add_repeat(con, 1, 99, days=400)
    raw, _ = causes.generate_candidates(con, request(window_days=30))
    assert raw[0].supporting_case_ids == (1,)
    assert raw[0].contradicting_case_ids == ()


def test_a_repeat_after_an_inspection_is_not_a_contradiction(con):
    """Nothing was swapped, so nothing failed to hold."""
    case(con, 1, action_type="inspected")
    add_repeat(con, 1, 99, days=5)
    raw, _ = causes.generate_candidates(con, request())
    assert raw[0].contradicting_case_ids == ()


def test_contradicting_cases_reach_the_emitted_candidate(con):
    """The point of collecting them is that an engineer sees them."""
    case(con, 1, tail="N1")
    case(con, 2, tail="N2", finding=NO_FAULT_FOUND)
    report = causes.propose_causes(con, request())
    assert report.candidates[0].contradicting_case_ids == (2,)
    assert report.explanations[0].contradiction
    assert "narrative language" in " ".join(report.candidates[0].limitations)


# ── stage 1 · grouping ──────────────────────────────────────────────────

def test_the_same_component_named_two_ways_is_one_candidate(con):
    """`LH PITOT PROBE` and `LEFT PITOT PROBE` are one component class."""
    case(con, 1, tail="N1", part_name="LH PITOT PROBE", part_number=None)
    case(con, 2, tail="N2", part_name="LEFT PITOT PROBE", part_number=None)
    raw, _ = causes.generate_candidates(con, request())
    assert len(raw) == 1
    assert raw[0].supporting_case_ids == (1, 2)


def test_a_missing_part_number_does_not_split_a_component_in_two(con):
    """part_number is populated on barely half of `defect_action`; grouping on
    it would show an engineer two identical rows with half the evidence each."""
    case(con, 1, tail="N1", part_number="PN-1")
    case(con, 2, tail="N2", part_number=None)
    raw, _ = causes.generate_candidates(con, request())
    assert len(raw) == 1
    assert raw[0].part_numbers == ("PN-1",)


def test_a_part_named_unknown_is_not_a_hypothesis(con):
    case(con, 1, part_name="UNKNOWN", part_number=None)
    raw, _ = causes.generate_candidates(con, request())
    assert raw == ()


def test_the_cause_id_is_stable_across_runs_and_databases(con):
    key = causes.normalise_cause("LEFT PITOT PROBE", None)[0]
    assert causes.cause_id_for(key) == causes.cause_id_for(key)
    assert causes.cause_id_for(key).startswith("cause:")


def test_a_part_number_never_leaks_into_the_prose_of_a_cause(con):
    """Standing rule 4: `cause` and `limitations` are numeral-checked prose.

    Counts and part numbers live in the structured fields, where the UI can
    render them from their own column.
    """
    case(con, 1, part_name=None, part_number="4040800911")
    report = causes.propose_causes(con, request())
    assert report.candidates[0].cause == causes.UNNAMED_COMPONENT
    assert report.candidates[0].provenance["part_numbers"] == ["4040800911"]
    for text in (report.candidates[0].cause,
                 *report.candidates[0].limitations,
                 *report.missing_information):
        assert not re.search(r"\d", text), text


# ── stage 2 · scoring is explainable, and only scores ───────────────────

def test_the_score_is_exactly_the_sum_of_its_features(con):
    """A score whose parts cannot be recomputed is a score nobody can argue
    with, which is what 'AI confidence' looks like written down honestly."""
    case(con, 1, tail="N1", finding=CONFIRMED_FAULT, task_number="34-11-01")
    case(con, 2, tail="N2")
    case(con, 3, tail="N3", finding=NO_FAULT_FOUND)
    scored = causes.score_candidates(
        causes.generate_candidates(con, request())[0], request())
    top = scored[0]
    assert top.score == pytest.approx(sum(f.contribution for f in top.features))
    assert top.score == pytest.approx(
        round(sum(f.observed * f.weight for f in top.features), 6))


def test_every_feature_names_itself_and_says_why_it_moved_the_score(con):
    case(con, 1, tail="N1", finding=CONFIRMED_FAULT)
    scored = causes.score_candidates(
        causes.generate_candidates(con, request())[0], request())
    names = {f.name for f in scored[0].features}
    assert names == set(causes.WEIGHTS)
    for feature in scored[0].features:
        assert feature.detail.strip()
        assert feature.contribution == pytest.approx(
            feature.observed * feature.weight)


def test_a_confirmed_finding_outweighs_a_bare_replacement(con):
    """The strongest evidence this corpus contains is a recorded condition."""
    case(con, 1, tail="N1", part_name="PITOT PROBE", finding=CONFIRMED_FAULT)
    case(con, 2, tail="N1", part_name="STATIC PORT", part_number="PN-2")
    scored = causes.score_candidates(
        causes.generate_candidates(con, request())[0], request())
    assert scored[0].candidate.cause == "PITOT PROBE"
    contribution = {f.name: f.contribution for f in scored[0].features}
    assert contribution["confirmed_findings"] > 0


def test_five_cases_on_one_tail_score_below_five_across_five_tails(con):
    """Independence is worth more than volume — one airframe is one story."""
    for i in range(1, 6):
        case(con, i, tail="N1")
    single = causes.score_candidates(
        causes.generate_candidates(con, request())[0], request())[0]
    for i in range(11, 16):
        # A single-token name: `normalise_cause` sorts tokens, so a two-word
        # part is displayed alphabetised and would not match this assertion.
        case(con, i, tail=f"N{i}", part_name="ADIRU", part_number="PN-2")
    spread = [s for s in causes.score_candidates(
        causes.generate_candidates(con, request())[0], request())
        if s.candidate.cause == "ADIRU"][0]
    assert spread.score > single.score


def test_a_contradiction_pushes_a_candidate_down(con):
    case(con, 1, tail="N1", part_name="PITOT PROBE")
    case(con, 2, tail="N2", part_name="PITOT PROBE")
    before = causes.score_candidates(
        causes.generate_candidates(con, request())[0], request())[0].score
    case(con, 3, tail="N3", part_name="PITOT PROBE", finding=NO_FAULT_FOUND)
    after = [s for s in causes.score_candidates(
        causes.generate_candidates(con, request())[0], request())
        if s.candidate.cause == "PITOT PROBE"][0]
    assert after.score < before
    assert after.evidence_level == CONFLICTING


def test_ranks_are_one_to_n_without_gaps_or_duplicates(con):
    for i, part in enumerate(("PITOT PROBE", "STATIC PORT", "ADC MODULE"), 1):
        case(con, i, tail=f"N{i}", part_name=part, part_number=f"PN-{i}")
    scored = causes.score_candidates(
        causes.generate_candidates(con, request())[0], request())
    assert [s.rank for s in scored] == [1, 2, 3]


def test_scoring_is_deterministic_for_identical_evidence(con):
    for i in range(1, 4):
        # Eight *different* components. `normalise_cause` strips a
        # trailing numeral as quantity noise, so "PART 1".."PART 8"
        # are one component named eight times, not eight components.
        case(con, i, tail=f"N{i}", part_name=DISTINCT_PARTS[i - 1],
             part_number=f"PN-{i}")
    raw = causes.generate_candidates(con, request())[0]
    first = [s.candidate.cause_id for s in causes.score_candidates(raw, request())]
    second = [s.candidate.cause_id for s in causes.score_candidates(raw, request())]
    assert first == second


# ── the three stages are separable ──────────────────────────────────────

def test_scoring_takes_stage_one_output_and_never_touches_a_database(con):
    """The signature is the contract: no connection argument, so the ranker
    can be replaced without the assistant interface knowing."""
    parameters = inspect.signature(causes.score_candidates).parameters
    assert "con" not in parameters
    assert list(parameters) == ["raw_candidates", "request"]


def test_explanation_takes_stage_two_output_and_nothing_else(con):
    parameters = inspect.signature(causes.explain_candidate).parameters
    assert list(parameters) == ["scored"]


def test_each_stage_runs_alone_on_the_previous_stage_s_output(con):
    case(con, 1, tail="N1", finding=CONFIRMED_FAULT)
    case(con, 2, tail="N2")
    raw, census = causes.generate_candidates(con, request())
    assert raw and census["cases_matched"] == 2
    scored = causes.score_candidates(raw, request())
    assert scored and scored[0].rank == 1
    explanation = causes.explain_candidate(scored[0])
    assert explanation.cause_id == scored[0].candidate.cause_id


def test_a_different_ranker_changes_the_order_and_nothing_else(con):
    """The separation, demonstrated: re-rank by a different rule and stage 1's
    candidates and stage 3's rendering both still work untouched."""
    case(con, 1, tail="N1", part_name="PITOT PROBE")
    case(con, 2, tail="N2", part_name="STATIC PORT", part_number="PN-2")
    case(con, 3, tail="N3", part_name="STATIC PORT", part_number="PN-2")
    raw, _ = causes.generate_candidates(con, request())
    reversed_order = sorted(
        causes.score_candidates(raw, request()), key=lambda s: s.score)
    assert causes.explain_candidate(reversed_order[0]).headline


# ── stage 3 · explanation is derived, never invented ────────────────────

def test_every_explained_sentence_points_at_a_case_that_was_read(con):
    case(con, 1, tail="N1", finding=CONFIRMED_FAULT)
    case(con, 2, tail="N2", finding=NO_FAULT_FOUND)
    report = causes.propose_causes(con, request())
    explanation = report.explanations[0]
    cited = set(re.findall(r"defect:\d+",
                           " ".join(explanation.support + explanation.contradiction)))
    assert cited == {"defect:1", "defect:2"}
    assert causes.CLAIM in explanation.headline
    assert causes.CLAIM in explanation.as_text()


def test_an_explanation_lists_only_features_that_observed_something(con):
    case(con, 1, tail="N1")
    explanation = causes.explain_candidate(causes.score_candidates(
        causes.generate_candidates(con, request())[0], request())[0])
    assert not any("tail_history" in line for line in explanation.feature_lines)
    assert any("supporting_cases" in line for line in explanation.feature_lines)


# ── evidence levels come from the closed Phase 2 set ────────────────────

def test_the_module_invents_no_evidence_vocabulary_of_its_own(con):
    """Four levels, and they are Phase 2's four."""
    assert causes.STRONG in EVIDENCE_LEVELS
    assert causes.CONFLICTING in EVIDENCE_LEVELS
    assert causes.INSUFFICIENT in EVIDENCE_LEVELS
    assert causes.LIMITED in EVIDENCE_LEVELS


def test_several_independent_agreeing_cases_are_strong(con):
    for i, tail in enumerate(("N1", "N2", "N3"), start=1):
        case(con, i, tail=tail)
    report = causes.propose_causes(con, request())
    assert report.candidates[0].evidence_level == STRONG


def test_one_case_is_limited_not_strong(con):
    case(con, 1)
    report = causes.propose_causes(con, request())
    assert report.candidates[0].evidence_level == LIMITED


def test_three_cases_on_one_airframe_are_not_strong(con):
    """'Several independent prior cases' — one tail is not independent."""
    for i in range(1, 4):
        case(con, i, tail="N1")
    report = causes.propose_causes(con, request())
    assert report.candidates[0].evidence_level == LIMITED
    assert any("single airframe" in line
               for line in report.candidates[0].limitations)


def test_any_contradiction_makes_the_level_conflicting(con):
    for i, tail in enumerate(("N1", "N2", "N3"), start=1):
        case(con, i, tail=tail)
    case(con, 4, tail="N4", finding=NO_FAULT_FOUND)
    report = causes.propose_causes(con, request())
    assert report.candidates[0].evidence_level == CONFLICTING


# ── thin evidence returns insufficient and says what is missing ─────────

def test_an_empty_case_base_returns_insufficient_not_an_empty_list(con):
    report = causes.propose_causes(con, request())
    assert report.evidence_level == INSUFFICIENT
    assert report.candidates == ()
    assert any("no prior case" in line for line in report.missing_information)


def test_the_absent_closure_table_is_named_in_every_report(con):
    """`defect_closure` has 0 rows and that is the headline limitation."""
    case(con, 1)
    report = causes.propose_causes(con, request())
    assert any("no confirmed maintenance outcome" in line
               for line in report.missing_information)


def test_a_report_is_never_silent_about_what_it_could_not_check(con):
    case(con, 1, tail="N1")
    report = causes.propose_causes(con, request(tail="N1", fault_codes=("34-11002",)))
    joined = " ".join(report.missing_information)
    assert "could not be matched against prior cases" in joined
    assert "not in the aircraft register" in joined


def test_a_candidate_with_no_recorded_finding_says_so(con):
    """1.34 M actions against 246 k findings: a swap is not a diagnosis."""
    case(con, 1, tail="N1")
    report = causes.propose_causes(con, request())
    assert any("not what was found" in line
               for line in report.candidates[0].limitations)


def test_the_sample_caveat_rides_on_every_report(con):
    case(con, 1)
    report = causes.propose_causes(con, request())
    assert any("reportable-occurrence sample" in line
               for line in report.limitations)


# ── fault codes: routing works, case matching does not ──────────────────

def test_a_fault_code_routes_to_a_chapter_from_a_real_mmsg_row(con):
    con.execute("INSERT INTO mmsg(code,ata_chapter,text) "
                "VALUES('34-11002','34','PITOT HEAT')")
    con.execute("INSERT INTO mmsg_task(code,seq,task_number) "
                "VALUES('34-11002',1,'34-11-01-400-801')")
    case(con, 1, tail="N1")
    raw, census = causes.generate_candidates(
        con, causes.CauseRequest(symptom=SYMPTOM, fault_codes=("34-11002",)))
    assert census["fault_codes_resolved"] == ["34-11002"]
    routing = raw[0].provenance["fault_code_routing"]["34-11002"]
    assert routing["tasks"] == ["34-11-01-400-801"]


def test_a_fault_code_absent_from_the_catalogue_is_reported_not_ignored(con):
    case(con, 1)
    _, census = causes.generate_candidates(con, request(fault_codes=("99-99999",)))
    assert census["fault_codes_unmatched"] == ["99-99999"]


# ── aggregate-only, and bounded ─────────────────────────────────────────

def test_no_query_in_the_module_can_attribute_anything_to_an_engineer(con):
    """Standing rule 6, checked against the source rather than by review.

    The columns that name a person — `defect_closure.closed_by`, the whole
    `note` table, the gold-label author columns — are not read here, and
    `_run` routes every statement through `db.stats_guard` so the note table
    cannot be reached even by accident.
    """
    source = Path(causes.__file__).read_text(encoding="utf-8")
    for forbidden in ("closed_by", "created_by", "author", "app_user",
                      "adjudicator", "FROM note", "JOIN note"):
        assert forbidden not in source, forbidden
    assert "stats_guard(sql)" in source


def test_the_note_table_stays_unreachable_from_here(con):
    with pytest.raises(ValueError, match="note table"):
        causes._run(con, "SELECT COUNT(*) FROM note")


def test_no_case_evidence_field_carries_a_person(con):
    case(con, 1)
    raw, _ = causes.generate_candidates(con, request())
    fields = set(vars(raw[0].supporting[0]))
    assert not {"engineer", "closed_by", "signed_by", "author"} & fields


def test_the_candidate_list_is_bounded_by_the_requested_limit(con):
    for i in range(1, 9):
        # Eight *different* components. `normalise_cause` strips a
        # trailing numeral as quantity noise, so "PART 1".."PART 8"
        # are one component named eight times, not eight components.
        case(con, i, tail=f"N{i}", part_name=DISTINCT_PARTS[i - 1],
             part_number=f"PN-{i}")
    report = causes.propose_causes(con, request(limit=3))
    assert len(report.candidates) == 3


def test_the_candidate_list_can_never_exceed_the_schema_maximum(con):
    """A hypothesis list long enough to cover everything costs the engineer
    the one thing the tool exists to save."""
    for i in range(1, 16):
        # Eight *different* components. `normalise_cause` strips a
        # trailing numeral as quantity noise, so "PART 1".."PART 8"
        # are one component named eight times, not eight components.
        case(con, i, tail=f"N{i}", part_name=DISTINCT_PARTS[i - 1],
             part_number=f"PN-{i}")
    report = causes.propose_causes(con, request(limit=999))
    assert len(report.candidates) == MAX_RANK
    assert report.candidates[-1].rank == MAX_RANK


def test_handed_in_case_ids_are_bounded_too(con):
    for i in range(1, 6):
        case(con, i, tail=f"N{i}")
    raw, _ = causes.generate_candidates(
        con, causes.CauseRequest(symptom=SYMPTOM,
                                 case_ids=tuple(range(1, 6))))
    assert len(raw[0].supporting_case_ids) <= causes.MAX_CASE_IDS


def test_the_report_carries_its_source_and_its_freshness(con):
    """A row with no provenance is a row that reads as current when it is old."""
    case(con, 1)
    report = causes.propose_causes(con, request())
    assert "SDR" in report.source
    assert report.freshness == "2025-03-01"
