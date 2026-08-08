"""Phase 2 regression tests.

Everything runs against a synthetic fixture DB in a tmp dir and the offline
FakeEmbedder — pytest never opens the real database, never reaches a network and
never downloads a model.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aivionics import db
from aivionics.parsers import ata
from aivionics.retrieval import evalharness, indexer
from aivionics.retrieval.embedder import FakeEmbedder, blob_to_vec
from aivionics.retrieval.rerank import (
    FlashRankReranker, LLMReranker, NullReranker, parse_index_order)
from aivionics.retrieval.search import (
    Effectivity, SearchResult, SearchRun, Searcher, build_fts_match,
    has_exact_token)

# (task_number, function_code, chapter, section, subject, title, catalogue_only)
AMM_TASKS = [
    ("34-11-01-400-801", "400", "34", "11", "01", "PITOT PROBE - REMOVAL"),
    ("34-11-01-400-802", "400", "34", "11", "01", "PITOT PROBE - INSTALLATION"),
    ("34-11-01-810-801", "810", "34", "11", "01", "PITOT STATIC SYSTEM - FAULT ISOLATION"),
    ("34-12-00-710-801", "710", "34", "12", "00", "AIR DATA INERTIAL REFERENCE UNIT - OPERATIONAL TEST"),
    ("34-21-05-000-801", "000", "34", "21", "05", "STANDBY ATTITUDE INDICATOR - REMOVAL"),
    ("34-41-11-020-002", "020", "34", "41", "11", "WEATHER RADAR ANTENNA - REMOVAL"),
    ("23-11-01-400-801", "400", "23", "11", "01", "VHF COMMUNICATION TRANSCEIVER - REMOVAL"),
    ("23-51-02-810-801", "810", "23", "51", "02", "AUDIO CONTROL PANEL - FAULT ISOLATION"),
    ("27-31-00-820-801", "820", "27", "31", "00", "ELEVATOR FEEL COMPUTER - ADJUSTMENT"),
    ("27-11-05-400-801", "400", "27", "11", "05", "AILERON POSITION TRANSDUCER - REMOVAL"),
    ("31-41-00-710-801", "710", "31", "41", "00", "FLIGHT DATA RECORDER - OPERATIONAL TEST"),
    ("31-61-02-400-801", "400", "31", "61", "02", "DISPLAY UNIT - REMOVAL"),
    ("24-31-01-400-801", "400", "24", "31", "01", "TRANSFORMER RECTIFIER UNIT - REMOVAL"),
    ("24-21-00-810-801", "810", "24", "21", "00", "AC GENERATION SYSTEM - FAULT ISOLATION"),
]
# catalogue-only rows: title + hierarchy is the whole representation
FIM_TASKS = [
    ("34-11-01-810-802", "810", "34", "11", "01", "PITOT HEAT INOPERATIVE - FAULT ISOLATION"),
    ("34-21-00-810-805", "810", "34", "21", "00", "SMYD MAINTENANCE MESSAGE 34-21102 - FAULT ISOLATION"),
    ("23-11-00-810-803", "810", "23", "11", "00", "VHF NO TRANSMIT - FAULT ISOLATION"),
    ("31-61-00-810-801", "810", "31", "61", "00", "DISPLAY UNIT BLANK - FAULT ISOLATION"),
    ("27-31-00-810-802", "810", "27", "31", "00", "ELEVATOR FEEL SHIFT - FAULT ISOLATION"),
    ("G73-00-00-810-A73", "810", "73", "00", "00", "ENGINE FUEL FLOW INDICATION - FAULT ISOLATION"),
]
# (defect_text, ata_ref, gold task_number, split)
DEFECTS = [
    ("AIRSPEED DISAGREE ON CAPTAIN SIDE DURING CLIMB. PITOT PROBE FOUND BLOCKED.",
     "34", "34-11-01-400-801", "test"),
    ("NO 1 VHF TRANSCEIVER WILL NOT TRANSMIT ON ANY FREQUENCY.",
     "23", "23-11-01-400-801", "test"),
    ("CAPTAIN OUTBOARD DISPLAY UNIT BLANK AFTER TAKEOFF.",
     "31", "31-61-02-400-801", "test"),
    ("ELEVATOR FEEL SHIFT LIGHT ILLUMINATED IN CRUISE.",
     "27", "27-31-00-820-801", "test"),
    ("TRANSFORMER RECTIFIER UNIT NUMBER 2 OVERHEAT INDICATION.",
     "24", "24-31-01-400-801", "test"),
    ("WEATHER RADAR PICTURE INTERMITTENT AND ANTENNA DRIVE NOISY.",
     "34", "34-41-11-020-002", "test"),
    ("SMYD MAINTENANCE MESSAGE 34-21102 DISPLAYED ON CMC.",
     "34", "34-21-00-810-805", "test"),
    ("STANDBY ATTITUDE INDICATOR FLAG IN VIEW ON GROUND.",
     "34", "34-21-05-000-801", "test"),
    # ATA 21 is one of the six image-only chapters — no task rows exist, so this
    # pair is unanswerable by construction and must land in the unservable pool
    ("CABIN PRESSURE CONTROLLER FAULT, MANUAL MODE USED IN CRUISE.",
     "21", "21-31-01-400-801", "test"),
    ("PITOT HEAT INOPERATIVE ON PREFLIGHT CHECK.",
     "34", "34-11-01-400-801", "train"),
    ("AIRSPEED UNRELIABLE ON TAKEOFF ROLL.",
     "34", "34-11-01-400-801", "train"),
    ("AUDIO CONTROL PANEL DEAD AT FIRST OFFICER STATION.",
     "23", "23-51-02-810-801", "train"),
    ("FLIGHT DATA RECORDER FAIL LIGHT ON OVERHEAD PANEL.",
     "31", "31-41-00-710-801", "train"),
]
UNLABELLED_DEFECT = "CABIN READING LIGHT FLICKERS AT ROW 12."


def build_fixture(con):
    """Populate an empty schema with the synthetic corpus. Module level so the
    smoke runners can build the same DB without pytest."""
    amm = con.execute(
        "INSERT INTO manual(oem,aircraft_type,manual_type,doc_standard,revision,"
        "is_current) VALUES('boeing','737-8','AMM','ispec2200','48',1)").lastrowid
    fim = con.execute(
        "INSERT INTO manual(oem,aircraft_type,manual_type,doc_standard,revision,"
        "is_current) VALUES('boeing','737-8','FIM','metadata','12',1)").lastrowid

    for mid, rows, catalogue in ((amm, AMM_TASKS, 0), (fim, FIM_TASKS, 1)):
        for (tn, func, ch, sec, subj, title) in rows:
            con.execute(
                "INSERT INTO task(manual_id,task_number,function_code,title,"
                "ata_chapter,ata_section,ata_subject,effectivity_raw,applic_refs,"
                "body,catalogue_only,embed_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, tn, func, title, ch, sec, subj,
                 "EFFECTIVITY TBC ALL" if not catalogue else None,
                 "AP001 AP002" if catalogue else None,
                 None if catalogue else f"TASK {tn}\n{title}\nSTEP 1 do things",
                 catalogue, ata.build_embed_text(ch, sec, subj, title)))

    con.executemany(
        "INSERT INTO effectivity_airplane(eff_ref,model,owner,msn,line_no,tail,engine)"
        " VALUES(?,?,?,?,?,?,?)",
        [("AP001", "737-8", "OP", "60001", "7001", "4K-AZ12", "CFM"),
         ("AP002", "737-8", "OP", "60002", "7002", "4K-AZ13", "CFM")])

    for i, (text, jasc, gold, split) in enumerate(DEFECTS, start=1):
        did = con.execute(
            "INSERT INTO defect(aircraft_tail,reported_at,ata_ref,defect_text,"
            "rectification_text,sdr_year) VALUES(?,?,?,?,?,?)",
            (f"N{100 + i}", f"2025-01-{i:02d}", jasc, text,
             f"REPLACED IAW {gold}", 2025 if split == "test" else 2020)).lastrowid
        con.execute(
            "INSERT INTO label_silver(defect_id,task_number,manual_type,"
            "function_code,confidence_tier,leak_free,split) VALUES(?,?,?,?,?,1,?)",
            (did, gold, "AMM", gold.split("-")[3], "MED", split))
    con.execute("INSERT INTO defect(ata_ref,defect_text,sdr_year)"
                " VALUES('33',?,2025)", (UNLABELLED_DEFECT,))
    con.commit()
    return con


@pytest.fixture
def con(tmp_path):
    con = build_fixture(db.connect(tmp_path / "fixture.db"))
    yield con
    con.close()


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def indexed(con, embedder):
    indexer.build_all(con, embedder)
    return con


def _searcher(con, embedder, reranker=None):
    return Searcher(con, embedder, reranker=reranker)


# ── embedder ─────────────────────────────────────────────────────────────
def test_fake_embedder_is_deterministic_and_unit_norm(embedder):
    a = embedder.embed(["pitot probe removal", "vhf transceiver"])
    b = embedder.embed(["pitot probe removal", "vhf transceiver"])
    assert a.shape == (2, embedder.dim)
    assert (a == b).all()
    norms = (a ** 2).sum(axis=1) ** 0.5
    assert all(abs(n - 1.0) < 1e-5 for n in norms)
    # lexical overlap survives the hashing, which is what makes ranking tests real
    near = float(a[0] @ embedder.embed(["pitot probe installation"])[0])
    far = float(a[0] @ embedder.embed(["vhf transceiver"])[0])
    assert near > far


# ── indexer ──────────────────────────────────────────────────────────────
def test_indexer_covers_all_tasks_and_only_labelled_cases(con, embedder):
    out = indexer.build_all(con, embedder)
    n_tasks = len(AMM_TASKS) + len(FIM_TASKS)
    assert out["task_vectors"] == n_tasks
    assert out["task_fts"] == n_tasks
    # FIM catalogue rows carry no body and are indexed anyway — title is the
    # whole representation
    fim_vecs = con.execute(
        "SELECT COUNT(*) FROM vec_index v JOIN task t ON t.id=v.ref_id"
        " WHERE v.kind='task' AND t.catalogue_only=1").fetchone()[0]
    assert fim_vecs == len(FIM_TASKS)
    # cases: vectors for labelled defects only, FTS for all of them
    assert out["case_vectors"] == len(DEFECTS)
    assert out["case_fts"] == len(DEFECTS) + 1


def test_indexer_is_idempotent(con, embedder):
    first = indexer.build_all(con, embedder)
    second = indexer.build_all(con, embedder)
    assert first == second
    rows = con.execute("SELECT COUNT(*) FROM vec_index").fetchone()[0]
    assert rows == first["task_vectors"] + first["case_vectors"]
    dupes = con.execute(
        "SELECT COUNT(*) FROM (SELECT kind, ref_id, index_version FROM vec_index"
        " GROUP BY kind, ref_id, index_version HAVING COUNT(*) > 1)").fetchone()[0]
    assert dupes == 0


def test_vectors_are_stamped_with_index_version(con, embedder):
    indexer.build_all(con, embedder)
    versions = {r[0] for r in con.execute(
        "SELECT DISTINCT index_version FROM vec_index")}
    assert versions == {embedder.index_version}
    dim, blob = con.execute(
        "SELECT dim, vec FROM vec_index WHERE kind='task' LIMIT 1").fetchone()
    assert dim == embedder.dim
    assert blob_to_vec(blob, dim).shape == (embedder.dim,)


def test_second_index_version_does_not_disturb_the_first(con, embedder):
    indexer.build_all(con, embedder)
    other = FakeEmbedder(index_version="v0-fake-b")
    indexer.build_all(con, other)
    counts = dict(con.execute(
        "SELECT index_version, COUNT(*) FROM vec_index GROUP BY index_version"))
    assert set(counts) == {embedder.index_version, "v0-fake-b"}
    assert counts[embedder.index_version] == counts["v0-fake-b"]


# ── query shape / FTS ────────────────────────────────────────────────────
@pytest.mark.parametrize("text, expected", [
    ("REPLACED PITOT PROBE IAW AMM 34-11-01-400-801", True),
    ("SMYD MAINTENANCE MESSAGE 34-21102 DISPLAYED", True),
    ("P/N 622-1234-001 REPLACED", True),
    ("AIRSPEED DISAGREE ON CAPTAIN SIDE DURING CLIMB", False),
    ("DISPLAY UNIT BLANK AFTER TAKEOFF", False),
])
def test_exact_token_detection(text, expected):
    assert has_exact_token(text) is expected


def test_fts_match_turns_a_task_number_into_a_phrase():
    match = build_fts_match("REPLACED IAW 34-11-01-400-801 AND TESTED")
    assert '"34 11 01 400 801"' in match
    assert '"replaced"' in match


# ── hybrid search ────────────────────────────────────────────────────────
def test_exact_task_number_query_is_found_via_fts(indexed, embedder):
    s = _searcher(indexed, embedder)
    run = s.search("REPLACED PITOT PROBE IAW AMM 34-11-01-400-801")
    assert run.exact_query is True
    assert run.weights["fts"] > run.weights["dense"]        # PLAN 2.4b
    assert run.results[0].task_number == "34-11-01-400-801"
    prov = run.results[0].provenance
    assert prov["channels"] in ("fts", "both")
    assert prov["fts"] == 1.0        # the exact number is the strongest BM25 hit


def test_free_text_query_lets_the_dense_channel_dominate(indexed, embedder):
    s = _searcher(indexed, embedder)
    run = s.search("CAPTAIN OUTBOARD DISPLAY UNIT BLANK AFTER TAKEOFF", jasc="31")
    assert run.exact_query is False
    assert run.weights["dense"] > run.weights["fts"]
    assert run.results[0].ata_chapter == "31"


def test_jasc_boost_lifts_the_matching_chapter(indexed, embedder):
    s = _searcher(indexed, embedder)
    query = "ELEVATOR FEEL SHIFT LIGHT ILLUMINATED IN CRUISE"
    plain = s.search(query, jasc=None)
    boosted = s.search(query, jasc="27")
    rank = lambda run: [r.ata_chapter for r in run.results].index("27")  # noqa: E731
    assert rank(boosted) <= rank(plain)
    assert boosted.results[0].ata_chapter == "27"
    top = boosted.results[0]
    assert top.provenance["jasc_boost"] > 0
    assert top.provenance["jasc_hint"] == "27"


def test_jasc_boost_is_soft_and_never_excludes_other_chapters(indexed, embedder):
    """PLAN 2.5 — reporter-entered codes are miscoded at chapter boundaries, so
    a hard gate would put the right task permanently out of reach."""
    s = _searcher(indexed, embedder)
    run = s.search("VHF TRANSCEIVER WILL NOT TRANSMIT", jasc="34", top_k=50)
    chapters = {r.ata_chapter for r in run.results}
    assert "23" in chapters and chapters != {"34"}
    off_chapter = [r for r in run.results if r.ata_chapter != "34"]
    assert all(r.provenance["jasc_boost"] == 0 for r in off_chapter)
    # the miscoded query still surfaces the correct chapter-23 task
    assert any(r.task_number.startswith("23-") for r in run.results[:5])


def test_six_group_engine_task_numbers_resolve_exactly(tmp_path, embedder):
    """Engine chapters (71/73/75/77/79) carry a 6th group — 71-00-00-800-801-G00.
    The token patterns here are five-group shaped, so the trailing group must
    still reach FTS as its own phrase and the exact number must win."""
    con = db.connect(tmp_path / "engine.db")
    mid = con.execute(
        "INSERT INTO manual(oem,aircraft_type,manual_type,doc_standard,revision,"
        "is_current) VALUES('boeing','737-8','AMM','ispec2200','48',1)").lastrowid
    for tn, title in [("71-00-00-800-801-G00", "POWER PLANT - ADJUSTMENT"),
                      ("71-00-00-800-802-G00", "POWER PLANT - TEST"),
                      ("34-11-01-400-801", "PITOT PROBE - REMOVAL")]:
        ch, sec, subj = tn.split("-")[:3]
        con.execute(
            "INSERT INTO task(manual_id,task_number,function_code,title,ata_chapter,"
            "ata_section,ata_subject,body,catalogue_only,embed_text)"
            " VALUES(?,?,?,?,?,?,?,'body',0,?)",
            (mid, tn, tn.split("-")[3], title, ch, sec, subj,
             ata.build_embed_text(ch, sec, subj, title)))
    con.commit()
    indexer.build_all(con, embedder)

    target = "71-00-00-800-801-G00"
    match = build_fts_match(f"ENGINE ADJUSTMENT DONE IAW {target}")
    assert '"71 00 00 800 801 g00"' in match      # the full six-group phrase
    run = Searcher(con, embedder).search(f"ENGINE ADJUSTMENT DONE IAW {target}")
    assert run.results[0].task_number == target
    # relaxed matching still means chapter-section-subject + function code
    assert evalharness.relaxed_key(target) == "71-00-00-800"
    con.close()


def test_task_results_are_locators_not_bodies(indexed, embedder):
    s = _searcher(indexed, embedder)
    run = s.search("PITOT PROBE REMOVAL", jasc="34")
    top = run.results[0]
    assert top.task_number and top.title and top.manual_type
    assert not hasattr(top, "body")
    assert "STEP 1" not in json.dumps(top.provenance)


def test_catalogue_only_fim_rows_are_retrievable_and_flagged(indexed, embedder):
    s = _searcher(indexed, embedder)
    run = s.search("SMYD MAINTENANCE MESSAGE 34-21102 DISPLAYED ON CMC", jasc="34")
    fim = [r for r in run.results if r.manual_type == "FIM"]
    assert fim, "FIM catalogue rows must be retrievable from titles alone"
    assert fim[0].provenance["catalogue_only"] is True


def test_task_effectivity_path_resolves_and_fails_closed(indexed, embedder):
    s = _searcher(indexed, embedder)
    hit = s.search("SMYD MAINTENANCE MESSAGE", jasc="34",
                   effectivity=Effectivity(msn="60001"))
    states = {r.provenance["applicability"] for r in hit.results}
    assert "applicable" in states
    miss = s.search("SMYD MAINTENANCE MESSAGE", jasc="34",
                    effectivity=Effectivity(msn="99999"))
    fim = [r for r in miss.results if r.manual_type == "FIM"]
    # standing rule 8: never dropped, always reported
    assert fim and fim[0].provenance["applicability"] == "not_applicable"
    assert len(miss.results) == len(hit.results)
    unchecked = s.search("SMYD MAINTENANCE MESSAGE", jasc="34")
    assert {r.provenance["applicability"] for r in unchecked.results} == {"not_checked"}


def test_case_search_tags_provenance_and_has_no_mod_state(indexed, embedder):
    """PLAN 2.6 — SDR records no modification state, so cases get a provenance
    tag instead of an effectivity filter."""
    s = _searcher(indexed, embedder)
    run = s.search("AIRSPEED DISAGREE PITOT PROBE BLOCKED", kind="case", jasc="34")
    assert run.results
    prov = run.results[0].provenance
    assert prov["source"] == "sdr"
    assert prov["mod_state"].startswith("unknown")
    assert run.results[0].kind == "case"


def test_channels_can_be_isolated_for_the_baselines(indexed, embedder):
    s = _searcher(indexed, embedder)
    fts = s.search("PITOT PROBE REMOVAL", channels=("fts",))
    dense = s.search("PITOT PROBE REMOVAL", channels=("dense",))
    assert fts.weights["dense"] == 0 and dense.weights["fts"] == 0
    assert fts.results and dense.results


# ── rerankers ────────────────────────────────────────────────────────────
def _candidates(n=5):
    return [
        SearchResult(kind="task", id=i, score=1.0 - i * 0.1,
                     task_number=f"34-11-0{i}-400-80{i}",
                     title=f"TASK {i}", hierarchy=f"Navigation > 34-11-0{i} > TASK {i}",
                     provenance={"channels": "dense"})
        for i in range(n)
    ]


def test_null_reranker_preserves_order():
    cands = _candidates()
    assert [r.id for r in NullReranker().rerank("q", cands)] == [0, 1, 2, 3, 4]


def test_llm_reranker_applies_a_valid_permutation():
    cands = _candidates()
    rr = LLMReranker(lambda prompt: "[4, 3, 2, 1, 0]")
    out = rr.rerank("pitot", cands)
    assert [r.id for r in out] == [4, 3, 2, 1, 0]
    assert rr.stats["fallbacks"] == 0
    assert out[0].provenance["reranker"] == "llm"
    assert out[0].provenance["rerank_rank"] == 0
    # the caller's stage-1 list must not be reordered or annotated in place
    assert [r.id for r in cands] == [0, 1, 2, 3, 4]
    assert "reranker" not in cands[0].provenance


def test_llm_reranker_tolerates_a_code_fence():
    rr = LLMReranker(lambda p: "```json\n[1, 0, 2, 3, 4]\n```")
    assert [r.id for r in rr.rerank("q", _candidates())] == [1, 0, 2, 3, 4]
    assert rr.stats["fallbacks"] == 0


@pytest.mark.parametrize("response", [
    "not json at all",
    "Sure! Here is the ordering: [4, 3, 2, 1, 0]",     # prose around valid JSON
    "[0, 1]",                                          # partial list
    "[0, 0, 1, 2, 3]",                                 # duplicate index
    "[0, 1, 2, 3, 99]",                                # out of range
    "[-1, 1, 2, 3, 4]",                                # negative index
    "[0.5, 1, 2, 3, 4]",                               # not integers
    '{"order": [0, 1, 2, 3, 4]}',                      # object, not array
    "[0, 1, 2, 3,",                                    # truncated
    "",
])
def test_llm_reranker_fail_safe_returns_dense_order_unchanged(response):
    """PLAN 2.4 — the fail-safe is mandatory. Anything that is not exactly a
    permutation falls back to the dense ranking."""
    cands = _candidates()
    rr = LLMReranker(lambda prompt: response)
    out = rr.rerank("pitot", cands)
    assert [r.id for r in out] == [0, 1, 2, 3, 4]
    assert rr.stats["fallbacks"] == 1


def test_llm_reranker_survives_a_raising_llm():
    def boom(prompt):
        raise RuntimeError("model unavailable")

    rr = LLMReranker(boom)
    assert [r.id for r in rr.rerank("q", _candidates())] == [0, 1, 2, 3, 4]
    assert rr.stats["fallbacks"] == 1


def test_llm_prompt_carries_locators_only_and_demands_json():
    rr = LLMReranker(lambda p: "[0]")
    prompt = rr.build_prompt("pitot probe blocked", _candidates(3))
    assert "JSON array" in prompt
    assert "[0] 34-11-00-400-800" in prompt
    assert "STEP 1" not in prompt and "WARNING" not in prompt


@pytest.mark.parametrize("raw, n, expected", [
    ("[1, 0]", 2, [1, 0]),
    ("[0, 1]", 3, None),
    ("nope", 2, None),
    (None, 2, None),
])
def test_parse_index_order(raw, n, expected):
    assert parse_index_order(raw, n) == expected


def test_flashrank_reranker_is_lazy_and_needs_no_model_to_construct():
    rr = FlashRankReranker()
    assert rr._ranker is None and rr.name == "flashrank"
    assert rr.rerank("q", _candidates(1))[0].id == 0     # short-circuits


def test_flashrank_rejects_an_unknown_model_instead_of_degrading_silently():
    """A misnamed model is a configuration error and must raise. Swallowing it
    would report un-reranked results as reranked — which is how PLAN 2.4a's
    ms-marco-MiniLM-L-6-v2 (absent from FlashRank's model map) went unnoticed."""
    rr = FlashRankReranker(model_name="ms-marco-MiniLM-L-6-v2")
    with pytest.raises(ValueError, match="no model"):
        rr.rerank("q", _candidates(3))


# ── evaluation harness ───────────────────────────────────────────────────
def test_load_eval_queries_groups_gold_by_defect(indexed):
    queries = evalharness.load_eval_queries(indexed, split="test")
    assert len(queries) == sum(1 for d in DEFECTS if d[3] == "test")
    assert all(q.gold and q.query for q in queries)
    assert queries[0].jasc == "34"


def test_ndcg_never_exceeds_one_when_siblings_share_a_relaxed_key():
    """-801 and -802 collapse to the same relaxed key. One gold item may only
    be credited once, at its best rank, or NDCG climbs above 1."""
    q = evalharness.EvalQuery(defect_id=1, query="pitot", jasc="34",
                              gold=("34-11-01-400-801",))
    siblings = [
        SearchResult(kind="task", id=i, score=1.0 - i * 0.1, task_number=tn)
        for i, tn in enumerate(["34-11-01-400-801", "34-11-01-400-802",
                                "34-11-01-400-803", "23-11-01-400-801",
                                "27-11-05-400-801"])
    ]
    run = SearchRun(query=q.query, ranked=siblings, results=siblings,
                    weights={}, exact_query=False)
    strict = evalharness._mode_metrics(run, q, evalharness.strict_key, 5, 50)
    relaxed = evalharness._mode_metrics(run, q, evalharness.relaxed_key, 5, 50)
    assert strict["ndcg_at_k"] == 1.0
    assert relaxed["ndcg_at_k"] == 1.0        # was 1.63 before the fix
    assert relaxed["hit_at_k"] and relaxed["top1_correct"]


def test_relaxed_key_ignores_the_sequence_number():
    assert evalharness.relaxed_key("34-11-01-400-801") == "34-11-01-400"
    assert (evalharness.relaxed_key("34-11-01-400-801")
            == evalharness.relaxed_key("34-11-01-400-802"))
    assert evalharness.strict_key("34-11-01-400-801") != "34-11-01-400"


def test_eval_harness_reports_every_metric_and_runs_every_baseline(
        indexed, embedder, tmp_path):
    rr = LLMReranker(lambda prompt: "")          # always falls back — still a run
    searcher = _searcher(indexed, embedder, reranker=rr)
    report = evalharness.run_all(indexed, searcher, split="test", threshold=0.3)

    names = [r["name"] for r in report["runs"]]
    assert names == [
        "baseline: top-20 frequency/ATA",
        "baseline: FTS5 alone",
        "baseline: vector alone",
        "hybrid, no rerank",
        "hybrid + llm rerank",
    ]
    for run in report["runs"]:
        assert run["n_queries"] == report["n_queries"] > 0
        assert 0.0 <= run["abstention_rate"] <= 1.0
        for mode in ("strict", "relaxed"):
            m = run[mode]
            for key in ("stage1_recall", "recall_at_50", "ndcg_at_5", "hit_at_5",
                        "top1_accuracy"):
                assert 0.0 <= m[key] <= 1.0, (run["name"], mode, key)
            cw = m["confident_wrong"]
            for key in ("n_wrong", "rate_of_all", "score_mean", "score_p50",
                        "score_p75", "score_p90", "score_max",
                        "n_above_threshold", "rate_above_threshold"):
                assert key in cw
            assert cw["n_wrong"] + round(m["top1_accuracy"] * run["n_queries"]) \
                == run["n_queries"]
        # relaxed can only ever be at least as generous as strict
        assert run["relaxed"]["hit_at_5"] >= run["strict"]["hit_at_5"]

    table = evalharness.format_table(report)
    assert "strict" in table and "relaxed" in table
    assert "baseline: top-20 frequency/ATA" in table
    # this reranker fell back on every query, so its row is really the
    # un-reranked run — the report must say so rather than let it pass as a gain
    assert report["reranker_stats"]["fallbacks"] == report["n_queries"]
    assert "WARNING" in table

    out = evalharness.write_json(report, tmp_path / "eval" / "report.json")
    assert json.loads(out.read_text(encoding="utf-8"))["n_queries"] == report["n_queries"]


def test_hybrid_beats_the_frequency_baseline_on_the_fixture(indexed, embedder):
    """Not Gate 2 — the fixture is 20 tasks — but it proves the comparison is
    wired end to end and pointing the right way."""
    searcher = _searcher(indexed, embedder)
    queries = evalharness.load_eval_queries(indexed, split="test")
    freq = evalharness.evaluate(
        indexed, evalharness.frequency_fn(indexed), "freq", queries)
    hybrid = evalharness.evaluate(
        indexed, evalharness.hybrid_fn(searcher, rerank=False), "hybrid", queries)
    assert hybrid["strict"]["ndcg_at_5"] > freq["strict"]["ndcg_at_5"]
    assert hybrid["strict"]["recall_at_50"] >= freq["strict"]["recall_at_50"]


@pytest.mark.parametrize("task_number, expected", [
    ("34-11-01-400-801", "34"),
    ("G73-00-00-810-A73", "73"),          # engine letter prefix
    ("71-00-00-800-801-G00", "71"),       # six-group engine form
    ("", None),
    (None, None),
])
def test_chapter_of(task_number, expected):
    assert evalharness.chapter_of(task_number) == expected


def test_servable_chapters_covers_amm_and_fim_alike(indexed):
    servable = evalharness.servable_chapters(indexed)
    assert "34" in servable and "23" in servable
    assert "73" in servable, "FIM catalogue rows make a chapter servable too"
    assert "21" not in servable, "ATA 21 has no task rows in the fixture"


def test_gold_chapters_come_from_the_label_not_the_jasc_hint(indexed):
    """The JASC hint is what the reporter typed and is miscoded at chapter
    boundaries; coverage is a fact about the label and the corpus."""
    q = evalharness.EvalQuery(defect_id=1, query="x", jasc="34",
                              gold=("23-11-01-400-801",))
    assert q.gold_chapters == ("23",)


def test_pools_split_servable_from_unservable_and_label_the_headline(
        indexed, embedder):
    searcher = _searcher(indexed, embedder)
    queries = evalharness.load_eval_queries(indexed, split="test")
    run = evalharness.evaluate(
        indexed, evalharness.hybrid_fn(searcher, rerank=False), "hybrid", queries)

    pools = {p["pool"]: p for p in run["pools"]}
    assert set(pools) == {"all", "servable", "unservable",
                          "answerable", "unanswerable"}
    assert pools["all"]["n_queries"] == len(queries) == run["n_queries"]
    assert (pools["answerable"]["n_queries"] + pools["unanswerable"]["n_queries"]
            == pools["all"]["n_queries"])
    assert (pools["servable"]["n_queries"] + pools["unservable"]["n_queries"]
            == pools["all"]["n_queries"])
    # the ATA 21 pair has no corpus, so it is unanswerable by construction
    assert pools["unservable"]["n_queries"] == 1
    assert pools["unservable"]["strict"]["recall_at_50"] == 0.0
    assert pools["unservable"]["strict"]["hit_at_5"] == 0.0
    # excluding the hole must not be quotable as the headline
    assert "HEADLINE" in pools["all"]["label"]
    assert "NOT the headline" in pools["servable"]["label"]
    # and the servable pool should read better than the pooled headline
    assert (pools["servable"]["strict"]["recall_at_50"]
            >= pools["all"]["strict"]["recall_at_50"])


def test_by_chapter_is_sorted_by_n_and_flags_servability(indexed, embedder):
    searcher = _searcher(indexed, embedder)
    queries = evalharness.load_eval_queries(indexed, split="test")
    run = evalharness.evaluate(
        indexed, evalharness.hybrid_fn(searcher, rerank=False), "hybrid", queries)

    rows = run["by_chapter"]
    assert sum(r["n_queries"] for r in rows) == run["n_queries"]
    assert [r["n_queries"] for r in rows] == sorted(
        (r["n_queries"] for r in rows), reverse=True)
    by_ch = {r["ata_chapter"]: r for r in rows}
    assert by_ch["34"]["servable"] is True
    assert by_ch["34"]["chapter_name"] == "Navigation"
    assert by_ch["21"]["servable"] is False
    assert by_ch["21"]["strict"]["recall_at_50"] == 0.0
    for row in rows:
        for mode in ("strict", "relaxed"):
            assert 0.0 <= row[mode]["ndcg_at_5"] <= 1.0


def test_answerable_pool_separates_a_missing_manual_from_a_missed_retrieval(
        indexed, embedder):
    """The measurement that decides Gate 2 on this corpus.

    A pair whose labelled task is not in the index cannot be retrieved at any
    rank. Pooling those into the headline measures how much of the fleet's
    manuals we hold, not how well the engine ranks — so the answerable pool
    has to be reported next to the ceiling, and the ceiling has to bound the
    headline exactly.
    """
    searcher = _searcher(indexed, embedder)
    queries = evalharness.load_eval_queries(indexed, split="test")
    run = evalharness.evaluate(
        indexed, evalharness.hybrid_fn(searcher, rerank=False), "hybrid", queries)

    pools = {p["pool"]: p for p in run["pools"]}
    ceiling = run["ceiling"]
    assert ceiling["n_answerable"] == pools["answerable"]["n_queries"]

    # strict recall over ALL pairs can never exceed the answerable fraction
    assert (pools["all"]["strict"]["recall_at_50"]
            <= ceiling["max_recall_strict"] + 1e-9)

    # a pair we hold no task for scores zero strictly, by construction
    assert pools["unanswerable"]["strict"]["recall_at_50"] == 0.0
    assert pools["unanswerable"]["strict"]["hit_at_5"] == 0.0

    # and the label must forbid quoting the engine's score as overall quality
    assert "Never quote this as overall performance" in pools["answerable"]["label"]


def test_evaluate_without_corpus_keys_treats_every_pair_as_answerable(
        indexed, embedder):
    """A caller that does not ask the corpus what it holds gets the honest
    degenerate case — answerable == all — rather than a silently empty pool."""
    searcher = _searcher(indexed, embedder)
    queries = evalharness.load_eval_queries(indexed, split="test")
    run = evalharness.evaluate(
        None, evalharness.hybrid_fn(searcher, rerank=False), "hybrid", queries)
    pools = {p["pool"]: p for p in run["pools"]}
    assert pools["answerable"]["n_queries"] == pools["all"]["n_queries"]
    assert pools["unanswerable"]["n_queries"] == 0
    assert run["ceiling"]["max_recall_strict"] == 1.0


def test_coverage_summary_and_new_sections_reach_the_output(
        indexed, embedder, tmp_path):
    searcher = _searcher(indexed, embedder)
    report = evalharness.run_all(indexed, searcher, split="test")

    cov = report["coverage"]
    assert cov["n_pairs_unservable"] == 1
    assert cov["n_pairs_servable"] == report["n_queries"] - 1
    assert "21" not in cov["servable_chapters"]
    assert 0.0 < cov["pct_pairs_servable"] < 100.0

    table = evalharness.format_table(report)
    assert "coverage pools" in table
    assert "per-ATA-chapter" in table
    assert "unservable" in table
    assert "srv" in table
    # the JSON must carry the same structure, for every run
    out = evalharness.write_json(report, tmp_path / "cov.json")
    loaded = json.loads(out.read_text(encoding="utf-8"))
    for run in loaded["runs"]:
        assert len(run["pools"]) == 5
        assert run["by_chapter"]
        assert run["ceiling"]["n_answerable"] <= loaded["n_queries"]


def test_frequency_baseline_prefers_the_precomputed_phase0_table(indexed):
    """Phase 0.8 writes baseline_freq(ata_chapter, task_number, cnt, rank)."""
    indexed.executemany(
        "INSERT INTO baseline_freq(ata_chapter,task_number,cnt,rank)"
        " VALUES(?,?,?,?)",
        [("34", "34-99-99-400-999", 42, 1), ("34", "34-11-01-400-801", 7, 2)])
    table = evalharness.load_frequency_baseline(indexed)
    assert table["34"][0] == "34-99-99-400-999"
    assert table[""], "an ungrouped fallback list must exist"


def test_frequency_baseline_falls_back_when_the_table_is_empty(indexed):
    """The table exists in the schema from day one, so 'present' is not the same
    as 'populated' — an empty one must fall back to the train split."""
    assert indexed.execute("SELECT COUNT(*) FROM baseline_freq").fetchone()[0] == 0
    table = evalharness.load_frequency_baseline(indexed)
    assert "34-11-01-400-801" in table["34"]
    assert "23-51-02-810-801" in table["23"]
