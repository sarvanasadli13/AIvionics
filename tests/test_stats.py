"""Phase 3 — case base, repeat linkage and the rate layer.

All logic, no Qt: every test here runs against a fixture database built by
``db.connect(tmp_path/...)``. The real corpus is never opened.

The tests that matter most are the ones guarding a claim the product makes
about itself: that the repeat rate is not the naive tail x chapter product,
that a rate can never escape its n, and that a proxy is never averaged with a
measurement.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aivionics import db
from aivionics.stats import casebase, metrics, repeat, schema
from aivionics.stats.metrics import (OPERATOR_CONFIRMED, SDR_MINED, Rate,
                                     wilson_interval)


# ── fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture()
def con(tmp_path):
    c = db.connect(tmp_path / "stats.db")
    schema.ensure(c)
    yield c
    c.close()


def add_defect(con, did, *, tail="N101AA", ata="34", at="2025-03-01",
               defect_text="CAPT AIRSPEED UNRELIABLE",
               rect="REPLACED PITOT PROBE PER AMM 34-11-01-400-801.",
               source="sdr"):
    con.execute(
        "INSERT INTO defect(id,aircraft_tail,reported_at,ata_ref,defect_text,"
        "rectification_text,source,sdr_year) VALUES(?,?,?,?,?,?,?,2025)",
        (did, tail, at, ata, defect_text, rect, source))
    return did


def add_action(con, did, action_type="replaced", part_number="PN-1",
               part_name="PITOT PROBE"):
    con.execute("INSERT INTO defect_action(defect_id,action_type,part_name,"
                "part_number) VALUES(?,?,?,?)",
                (did, action_type, part_name, part_number))


def add_repeat(con, first, second, days=5, similarity=0.9, ata="34"):
    con.execute("INSERT INTO repeat_norm(defect_id,repeat_defect_id,days_apart,"
                "similarity,same_action,ata_chapter) VALUES(?,?,?,?,0,?)",
                (first, second, days, similarity, ata))


# ── 3.1 action extraction ────────────────────────────────────────────────

def test_removed_and_replaced_beats_its_own_components():
    """The compound form must not be shredded into a bare REMOVED."""
    action = casebase.extract_action(
        "REMOVED AND REPLACED TRIPLE INDICATOR IAW AMM 77-14-00, 4-1. MOC OKAY.")
    assert action.action_type == "removed_replaced"
    assert action.part_name == "TRIPLE INDICATOR"
    assert action.is_removal


def test_part_phrase_stops_at_the_citation_and_drops_the_quantity():
    action = casebase.extract_action(
        "REPLACED FWD ENTRY AREA UPPER EMERGENCY LIGHT 1EA IAW B737-700 AMM "
        "33-51-01-960-801.  TEST IS OK")
    assert action.part_name == "FWD ENTRY AREA UPPER EMERGENCY LIGHT"
    assert action.position == "FWD"


def test_structured_sdr_columns_win_over_the_narrative():
    """A field the reporter filled in beats anything a regex recovers."""
    action = casebase.extract_action(
        "MAINTENANCE REPLACED WBU24. IAW B787-AMM-33-50-71-N7A-421A-A.",
        part_name="BATTERY UNIT", part_number="ADD71701100")
    assert (action.part_name, action.part_number) == ("BATTERY UNIT",
                                                      "ADD71701100")


def test_part_number_is_recovered_from_the_text_when_sdr_has_none():
    action = casebase.extract_action("REPLACED VALVE P/N 3214-31A PER AMM.")
    assert action.part_number == "3214-31A"


def test_a_narrative_with_no_action_verb_yields_nothing():
    assert casebase.extract_action("CREW REPORTED A SMELL IN THE CABIN.") is None
    assert casebase.extract_action("") is None
    assert casebase.extract_action(None) is None


def test_inspection_is_not_counted_as_a_removal():
    action = casebase.extract_action("PERFORMED INSPECT IAW AMM 05-51-56.")
    assert action.action_type == "inspected"
    assert not action.is_removal


# ── 3.1 finding extraction ───────────────────────────────────────────────

def test_found_condition_is_a_confirmed_fault():
    finding = casebase.extract_finding(
        "TROUBLESHOT FAULT. FOUND CHAFED WIRE AT CONNECTOR BEHIND P6-4.")
    assert finding.finding_type == casebase.CONFIRMED_FAULT
    assert "CHAFED WIRE" in finding.finding_text


@pytest.mark.parametrize("text", [
    "REPLACED UNIT. OPS CHECK GOOD.",
    "NO FAULTS FOUND DURING GROUND RUN.",
    "COULD NOT DUPLICATE THE REPORTED CONDITION.",
    "NO DEFECTS NOTED.",
])
def test_no_fault_language_is_classified_as_such(text):
    assert casebase.extract_finding(text).finding_type == casebase.NO_FAULT_FOUND


def test_a_confirmed_fault_outranks_a_trailing_ops_check():
    """"Replaced it, found it chafed, ops check good" confirmed a fault."""
    finding = casebase.extract_finding(
        "REPLACED HARNESS. FOUND CORRODED PIN IN THE CONNECTOR. OPS CHECK GOOD.")
    assert finding.finding_type == casebase.CONFIRMED_FAULT


def test_silence_is_recorded_as_silence_not_as_a_clean_result():
    finding = casebase.extract_finding("REPLACED THE UNIT PER AMM 34-11-01.")
    assert finding.finding_type == casebase.NOT_RECORDED
    assert not finding.recorded


def test_the_word_nff_never_appears_in_a_user_facing_metric_name():
    """PLAN 3.3: NFF is a shop finding and is not in this data."""
    rendered = " ".join([
        metrics.METRIC_NAME, metrics.METRIC_SHORT,
        *metrics.PROVENANCE_TEXT.values(),
    ]).upper()
    assert "NFF" not in rendered
    assert "NO FAULT FOUND" not in rendered


# ── 3.1 build ────────────────────────────────────────────────────────────

def test_build_populates_both_tables_and_is_idempotent(con):
    add_defect(con, 1, rect="REMOVED AND REPLACED PITOT PROBE PER AMM 34-11-01.")
    add_defect(con, 2, rect="TROUBLESHOT. FOUND CHAFED WIRE BEHIND P6-4.")
    add_defect(con, 3, rect=None, defect_text="ODOUR IN CABIN")
    con.commit()

    first = casebase.build(con)
    assert first["actions"] == 2
    assert first["findings"] == 1

    again = casebase.build(con)
    assert again["actions"] == 0
    assert con.execute("SELECT COUNT(*) FROM defect_action").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM defect_finding").fetchone()[0] == 1


def test_rebuild_keeps_an_engineer_recorded_finding(con):
    """Phase 4C promotes a note to a finding. A stats rebuild must not eat it."""
    add_defect(con, 1)
    con.commit()
    casebase.build(con)
    con.execute("INSERT INTO defect_finding(defect_id,finding_type,finding_text,"
                "source) VALUES(1,'confirmed_fault','CHAFE AT P6-4','engineer')")
    con.commit()

    casebase.build(con, rebuild=True)
    sources = {r[0] for r in con.execute("SELECT source FROM defect_finding")}
    assert "engineer" in sources


# ── 3.2 symptom normalisation ────────────────────────────────────────────

def test_abbreviated_and_spelled_out_symptoms_normalise_to_one_form():
    a = repeat.normalise_symptom("CAPT AIRSPEED UNRELIABLE")
    b = repeat.normalise_symptom("CAPTAIN AIRSPEED UNRELIABLE.")
    assert a == b
    assert repeat.similarity(a, b) == 1.0


def test_normalisation_drops_bare_numbers_and_stopwords():
    tokens = repeat.normalise_symptom(
        "ON 14 MARCH THE CREW REPORTED A LEFT ENGINE OIL PRESSURE INDICATION")
    assert "14" not in tokens
    assert "THE" not in tokens and "REPORTED" not in tokens
    assert {"LEFT", "ENGINE", "OIL", "PRESSURE", "INDICATION"} <= tokens


def test_negation_survives_normalisation():
    """Dropping NO would turn 'no fault indication' into 'fault indication'."""
    assert "NO" in repeat.normalise_symptom("NO FAULT INDICATION ON CDU")


def test_unrelated_symptoms_in_one_chapter_do_not_match():
    a = repeat.normalise_symptom("SKIN CRACK FOUND AT STATION 300 STRINGER 12")
    b = repeat.normalise_symptom("CORROSION ON FLOOR BEAM AT STATION 720")
    assert repeat.similarity(a, b) < repeat.DEFAULT_THRESHOLD


def test_a_one_word_symptom_cannot_match_anything():
    """Otherwise 'CRACK' matches every structural write-up ever filed."""
    assert repeat.similarity(repeat.normalise_symptom("CRACK"),
                             repeat.normalise_symptom("CRACK")) == 0.0


# ── 3.2 linkage ──────────────────────────────────────────────────────────

def _seed_ata53_clique(con):
    """Six structural findings on one tail, one chapter, one month.

    This is the exact shape that produced the implausible 54.7% fleet-wide
    repeat rate: naive linkage counts every pair inside the clique.
    """
    texts = [
        "SKIN CRACK AT STATION 300 STRINGER 12 UPPER LOBE",
        "CORROSION ON FLOOR BEAM AT STATION 720 LEFT SIDE",
        "DENT IN FUSELAGE SKIN AT STATION 480 RIGHT SIDE",
        "SEAT TRACK WORN AT ROW 14 LEFT SIDE",
        "CARGO FLOOR PANEL DELAMINATION AFT COMPARTMENT",
        "STRINGER 27 CRIPPLED AT FRAME 640 LOWER LOBE",
    ]
    for i, text in enumerate(texts):
        add_defect(con, 100 + i, tail="N53AA", ata="53",
                   at=f"2025-06-{i + 1:02d}", defect_text=text,
                   rect="REPAIRED IAW SRM 53-00-50.")


def test_repeat_linkage_does_not_recreate_the_ata53_clique(con):
    _seed_ata53_clique(con)
    # ...and one genuine avionics repeat on another tail.
    add_defect(con, 1, tail="N34AA", ata="34", at="2025-06-01",
               defect_text="CAPT AIRSPEED UNRELIABLE ON TAKEOFF ROLL")
    add_defect(con, 2, tail="N34AA", ata="34", at="2025-06-09",
               defect_text="CAPTAIN AIRSPEED UNRELIABLE ON TAKEOFF ROLL.")
    con.commit()

    run = repeat.build(con)

    naive_pairs_in_the_clique = 6 * 5 // 2      # what tail x chapter would give
    assert run.pairs < naive_pairs_in_the_clique
    by_chapter = run.per_chapter
    assert by_chapter.get("53", 0) == 0, "structural clique must not link"
    assert by_chapter.get("34") == 1
    assert con.execute("SELECT COUNT(*) FROM repeat_norm").fetchone()[0] == 1


def test_a_repeat_outside_the_window_is_not_a_repeat(con):
    add_defect(con, 1, at="2025-01-01", defect_text="CAPT AIRSPEED UNRELIABLE")
    add_defect(con, 2, at="2025-04-01", defect_text="CAPT AIRSPEED UNRELIABLE")
    con.commit()
    assert repeat.build(con, window_days=30).pairs == 0
    assert repeat.build(con, window_days=120).pairs == 1


def test_the_same_symptom_on_a_different_tail_is_not_a_repeat(con):
    add_defect(con, 1, tail="N1", at="2025-01-01")
    add_defect(con, 2, tail="N2", at="2025-01-05")
    con.commit()
    assert repeat.build(con).pairs == 0


def test_the_forward_cap_is_reported_rather_than_hidden(con):
    for i in range(12):
        add_defect(con, 200 + i, tail="N9", ata="21", at=f"2025-05-{i + 1:02d}",
                   defect_text="PACK ONE TRIP OFF IN CRUISE")
    con.commit()
    run = repeat.build(con, max_forward=2)
    assert run.capped_defects > 0
    assert run.pairs < 12 * 11 // 2


def test_chapter_counts_report_eligible_alongside_repeated(con):
    add_defect(con, 1, at="2025-01-01")
    add_defect(con, 2, at="2025-01-05")
    con.commit()
    repeat.build(con)
    rows = {r["chapter"]: r for r in repeat.chapter_counts(con)}
    assert rows["34"]["eligible"] == 2
    assert rows["34"]["repeated"] == 1


# ── 3.4 Wilson intervals ─────────────────────────────────────────────────

def test_wilson_bounds_stay_inside_zero_and_one():
    assert wilson_interval(0, 10)[0] == 0.0
    # floating point: the closed form lands on 0.9999999999999999 for k==n
    assert wilson_interval(10, 10)[1] == pytest.approx(1.0)
    for k, n in ((0, 1), (1, 1), (3, 7), (50, 100), (1, 1000)):
        lo, hi = wilson_interval(k, n)
        assert 0.0 <= lo <= k / n <= hi <= 1.0 + 1e-9


def test_wilson_narrows_as_n_grows():
    small = wilson_interval(5, 10)
    large = wilson_interval(500, 1000)
    assert (large[1] - large[0]) < (small[1] - small[0])


def test_wilson_on_zero_observations_claims_nothing():
    assert wilson_interval(0, 0) == (0.0, 1.0)


# ── 3.4 suppression ──────────────────────────────────────────────────────

def test_a_rate_below_the_support_threshold_yields_no_number():
    rate = Rate(3, 4)
    assert rate.suppressed
    assert rate.value is None
    assert rate.interval is None
    assert rate.text == "n too small (n=4)"
    assert "%" not in rate.text


def test_a_rate_at_the_threshold_reports_and_carries_its_n():
    rate = Rate(2, 5)
    assert not rate.suppressed
    assert rate.value == pytest.approx(0.4)
    assert "n=5" in rate.text and "40.0%" in rate.text


def test_no_public_stats_function_can_return_a_bare_rate():
    """Rule: a rate must be inseparable from its n.

    Anything callable that hands back a plain float would break that, so the
    return annotations of the query layer are checked directly.
    """
    import inspect
    for name, fn in vars(metrics).items():
        if name.startswith("_") or not inspect.isfunction(fn):
            continue
        annotation = str(inspect.signature(fn).return_annotation)
        assert annotation not in ("float", "<class 'float'>"), name


# ── 3.5 provenance ───────────────────────────────────────────────────────

def test_combining_across_provenance_raises(con):
    mined = Rate(2, 20, SDR_MINED)
    confirmed = Rate(4, 20, OPERATOR_CONFIRMED)
    with pytest.raises(ValueError, match="provenance"):
        metrics.combine([mined, confirmed])


def test_combining_within_one_provenance_sums_support():
    total = metrics.combine([Rate(2, 20, SDR_MINED), Rate(3, 30, SDR_MINED)])
    assert (total.numerator, total.n) == (5, 50)
    assert total.provenance == SDR_MINED


def test_every_rate_names_its_provenance_in_words():
    assert "proxy" in Rate(1, 10, SDR_MINED).provenance_text
    assert "tech log" in Rate(1, 10, OPERATOR_CONFIRMED).provenance_text


def test_operator_confirmed_excludes_an_incomplete_closure(con):
    """The PLAN §4 closure invariant, where a closure can exist at all."""
    add_defect(con, 1, source="operator")
    add_defect(con, 2, source="operator")
    add_action(con, 1)
    add_action(con, 2)
    con.execute("INSERT INTO defect_closure(defect_id,complete) VALUES(1,0)")
    con.execute("INSERT INTO defect_closure(defect_id,complete) VALUES(2,0)")
    con.execute("INSERT INTO defect_finding(defect_id,finding_type,source)"
                " VALUES(1,'confirmed_fault','engineer')")
    con.execute("UPDATE defect_closure SET complete=1 WHERE defect_id=1")
    con.commit()

    rate = metrics.removal_repeat_rate(con, provenance=OPERATOR_CONFIRMED)
    assert rate.n == 1, "the incomplete closure must not be counted"


def test_mined_and_confirmed_populations_do_not_leak_into_each_other(con):
    add_defect(con, 1, source="sdr")
    add_defect(con, 2, source="operator")
    add_action(con, 1)
    add_action(con, 2)
    con.execute("INSERT INTO defect_closure(defect_id,complete) VALUES(2,0)")
    con.execute("INSERT INTO defect_finding(defect_id,finding_type,source)"
                " VALUES(2,'confirmed_fault','engineer')")
    con.execute("UPDATE defect_closure SET complete=1 WHERE defect_id=2")
    con.commit()
    assert metrics.removal_repeat_rate(con, provenance=SDR_MINED).n == 1
    assert metrics.removal_repeat_rate(con, provenance=OPERATOR_CONFIRMED).n == 1


# ── 3.3 the metric ───────────────────────────────────────────────────────

def test_the_metric_counts_removals_followed_by_a_repeat(con):
    for i in range(1, 9):
        add_defect(con, i, at=f"2025-03-{i:02d}")
        add_action(con, i, "replaced")
    add_repeat(con, 1, 2, days=6)
    add_repeat(con, 3, 4, days=12)
    con.commit()
    rate = metrics.removal_repeat_rate(con)
    assert (rate.numerator, rate.n) == (2, 8)
    assert rate.value == pytest.approx(0.25)


def test_an_inspection_is_not_in_the_denominator(con):
    add_defect(con, 1)
    add_action(con, 1, "inspected")
    con.commit()
    assert metrics.removal_repeat_rate(con).n == 0


def test_a_repeat_outside_the_metric_window_does_not_count(con):
    for i in (1, 2):
        add_defect(con, i)
        add_action(con, i)
    add_repeat(con, 1, 2, days=60)
    con.commit()
    assert metrics.removal_repeat_rate(con, window_days=30).numerator == 0
    assert metrics.removal_repeat_rate(con, window_days=90).numerator == 1


def test_top_repeat_parts_suppresses_a_thin_part_number(con):
    for i in range(1, 8):
        add_defect(con, i)
        add_action(con, i, part_number="PN-BUSY")
    add_repeat(con, 1, 2)
    add_defect(con, 90)
    add_action(con, 90, part_number="PN-THIN")
    con.commit()
    rows = {r["part_number"]: r for r in metrics.top_repeat_parts(con)}
    assert rows["PN-BUSY"]["rate"].value is not None
    assert rows["PN-THIN"]["rate"].suppressed
    assert rows["PN-THIN"]["rate"].text.startswith("n too small")


def test_chapter_rates_carry_one_rate_per_chapter(con):
    for i, ata in enumerate(("34", "34", "21", "21", "21"), start=1):
        add_defect(con, i, ata=ata)
        add_action(con, i)
    add_repeat(con, 3, 4, ata="21")
    con.commit()
    rows = {r["chapter"]: r["rate"] for r in metrics.chapter_rates(con)}
    assert rows["21"].numerator == 1 and rows["21"].n == 3
    assert rows["34"].numerator == 0 and rows["34"].n == 2


def test_period_start_is_anchored_on_the_data_not_the_wall_clock(con):
    add_defect(con, 1, at="2020-05-20")
    con.commit()
    assert metrics.latest_report_date(con) == "2020-05-20"
    assert metrics.period_start(con, 30) == "2020-04-20"


def test_period_start_on_an_empty_database_is_none(con):
    assert metrics.period_start(con, 30) is None


# ── 3.7 cross-standard ───────────────────────────────────────────────────

def test_airframe_standard_buckets_by_year_of_manufacture():
    assert metrics.airframe_standard(1996) == "pre-2000"
    assert metrics.airframe_standard(2008) == "2000–2014"
    assert metrics.airframe_standard(2021) == "2015+"
    assert metrics.airframe_standard(None) == metrics.UNKNOWN_STANDARD


def test_stratification_keeps_the_standards_apart(con):
    con.execute("INSERT INTO aircraft(tail,type,year_built) VALUES('OLD','737',1996)")
    con.execute("INSERT INTO aircraft(tail,type,year_built) VALUES('NEW','737',2021)")
    for i in range(1, 7):
        add_defect(con, i, tail="OLD")
        add_action(con, i)
    for i in range(10, 16):
        add_defect(con, i, tail="NEW")
        add_action(con, i)
    add_repeat(con, 1, 2)
    add_repeat(con, 3, 4)
    add_repeat(con, 10, 11)
    con.commit()

    split = metrics.stratify_by_standard(con)
    labels = {r.label: r for r in split.strata}
    assert labels["pre-2000"].numerator == 2 and labels["pre-2000"].n == 6
    assert labels["2015+"].numerator == 1 and labels["2015+"].n == 6
    assert split.crude.n == 12


def test_standardisation_reweights_away_from_the_larger_stratum():
    """Equal weights, so a big low-rate stratum cannot swamp a small high one."""
    strata = [Rate(90, 100, SDR_MINED, "pre-2000"),
              Rate(1, 100, SDR_MINED, "2015+")]
    crude = Rate(91, 200, SDR_MINED, "fleet (crude)")
    standardised = metrics.Stratified(strata, crude).standardised()
    assert standardised.value == pytest.approx(0.455, abs=0.01)
    assert standardised.n == 200


def test_a_directional_conflict_between_strata_is_flagged():
    """Simpson: both strata above the pooled figure means pooling misleads."""
    strata = [Rate(9, 10, SDR_MINED, "pre-2000"), Rate(8, 10, SDR_MINED, "2015+")]
    honest = metrics.Stratified(strata, Rate(17, 20, SDR_MINED, "fleet (crude)"))
    assert not honest.direction_conflict
    reversed_ = metrics.Stratified(strata, Rate(2, 20, SDR_MINED, "fleet (crude)"))
    assert reversed_.direction_conflict


def test_standardisation_with_no_reportable_stratum_reports_nothing(con):
    split = metrics.Stratified([Rate(1, 2, SDR_MINED, "pre-2000")],
                               Rate(1, 2, SDR_MINED, "fleet (crude)"))
    assert split.standardised().value is None


# ── standing rules ───────────────────────────────────────────────────────

STATS_SOURCES = [
    Path(__file__).resolve().parents[1] / "src" / "aivionics" / "stats" / name
    for name in ("casebase.py", "repeat.py", "metrics.py", "schema.py")
] + [
    Path(__file__).resolve().parents[1] / "src" / "aivionics" / "ui" / p
    for p in ("pages/reliability.py", "pages/fleet.py", "statsservice.py")
]


def test_no_statistics_query_references_the_note_table():
    """PLAN §4: notes are evidence a human reads, never a row in an aggregate.

    Checked at the source level as well as through ``db.stats_guard`` — the
    guard only fires on a query that is actually executed, and an unexercised
    code path would slip past it.
    """
    pattern = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+note\b", re.I)
    for path in STATS_SOURCES:
        assert path.exists(), path
        offenders = pattern.findall(path.read_text(encoding="utf-8"))
        assert not offenders, f"{path.name} joins the note table: {offenders}"


def test_the_guard_rejects_a_query_that_reaches_for_notes(con):
    with pytest.raises(ValueError):
        metrics._run(con, "SELECT COUNT(*) FROM note")


def test_no_statistics_query_attributes_anything_to_an_engineer():
    """Standing rule 6: aggregate only, no per-engineer attribution.

    BetrVG §87(1)(6) — a system suitable for performance monitoring triggers
    Betriebsrat involvement, and engineers who believe they are measured write
    vaguer narratives, which poisons the data source this all rests on.
    """
    banned = re.compile(r"\b(?:closed_by|author_id|user_id)\b")
    for path in STATS_SOURCES:
        hits = banned.findall(path.read_text(encoding="utf-8"))
        assert not hits, f"{path.name} selects a person: {hits}"
