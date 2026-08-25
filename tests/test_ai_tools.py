"""The tool boundary and the answer schemas — Phase 2.

Two things are under test here and they fail in opposite directions.

`tools.py` is the boundary a model reaches the database through, and almost
every test below is about a *refusal*: an argument that was not coerced, a
limit that was not quietly clamped, a permission that was not assumed, a
missing table that was named rather than returned as an empty result. The one
failure mode this file exists to catch is the pleasant one — a tool that
answers when it should have said it cannot.

`schemas.py` is the gate the model's reply comes back through, and the tests
there are about *grounding*: a task number nobody retrieved, a page nobody
indexed, a numeral nobody measured. These are the failures that look like
successes on a screen, which is why each one is asserted on its violation code
rather than on "it raised something".

Everything runs against a hand-built SQLite corpus in `tmp_path`. Nothing
touches `data/aivionics.db` and nothing opens a socket.
"""
from __future__ import annotations

import sqlite3

import pytest

from aivionics import db
from aivionics.llm import schemas as S
from aivionics.llm import tools as T
from aivionics.stats import schema as stats_schema
from aivionics.ui.auth import ROLES, User

# A body is put on task 1 so that "no tool returns a task body" is a test of a
# decision rather than a test of an empty column. If this string ever reaches
# a tool result, standing rule 1 has been broken.
BODY_MARKER = "PRESSURISED NITROGEN"

# Long enough that `SNIPPET` has to cut it. 900 characters of one repeated
# word, so the assertion is about length and nothing else.
LONG_NARRATIVE = "FLAP SKEW MESSAGE ANNUNCIATED REPEATEDLY " * 30


# ── the corpus ──────────────────────────────────────────────────────────

def _build_corpus(path) -> None:
    """A four-table corpus with one of everything the tools read.

    Small on purpose. Every row here is referenced by at least one assertion,
    so a change that breaks a test points at the row that caused it rather
    than at a fixture nobody can hold in their head.
    """
    con = db.connect(path)
    stats_schema.ensure(con)

    con.execute(
        "INSERT INTO manual(id,oem,aircraft_type,manual_type,doc_standard,"
        "revision,revision_date,is_current,ingested_at) VALUES"
        "(1,'boeing','737 MAX','AMM','ispec2200','R12','2024-03-01',1,"
        "'2025-08-01T10:00:00Z')")
    con.execute(
        "INSERT INTO task(id,manual_id,task_number,function_code,title,"
        "ata_chapter,ata_section,effectivity_raw,applic_refs,body,embed_text,"
        "catalogue_only,has_warning,has_caution) VALUES"
        "(1,1,'34-11-01-400-801','400','Install the Air Data Module','34','11',"
        "'AIRPLANES 001-050','EFF001',?,'34-11 Air Data Module',0,1,0)",
        (f"WARNING: DO NOT VENT {BODY_MARKER}. 1. Remove the module.",))
    con.execute(
        "INSERT INTO task(id,manual_id,task_number,function_code,title,"
        "ata_chapter,ata_section,embed_text,catalogue_only) VALUES"
        "(2,1,'27-51-00-700-802','700','Flap Skew Test','27','51',"
        "'27-51 Flaps',0)")
    # FTS5 here is contentless (`content=''`), so the rowid has to be written
    # explicitly — it is what `fts_search` returns and what the task lookup
    # joins back on.
    con.execute("INSERT INTO task_fts(rowid,task_number,title,embed_text) "
                "VALUES(1,'34-11-01-400-801','Install the Air Data Module',"
                "'34-11 Air Data Module')")
    con.execute("INSERT INTO task_fts(rowid,task_number,title,embed_text) "
                "VALUES(2,'27-51-00-700-802','Flap Skew Test','27-51 Flaps')")

    con.execute("INSERT INTO effectivity_airplane(eff_ref,model,msn,line_no,"
                "tail) VALUES('EFF001','737-8','60123','7001','D-ABCD')")
    con.execute("INSERT INTO mmsg(code,ata_chapter,text) "
                "VALUES('34-11002','34','ADM disagree')")
    con.execute("INSERT INTO mmsg_task(code,seq,task_number) "
                "VALUES('34-11002',1,'34-11-01-400-801')")
    con.execute("INSERT INTO coverage(manual_id,ata_chapter,toc_count,"
                "extracted_count,pct) VALUES(1,'34',10,9,90.0)")
    con.execute(
        "INSERT INTO aircraft(id,tail,type,msn,line_number,year_built,"
        "total_time_hrs,total_cycles) VALUES"
        "(1,'D-ABCD','737-8','60123','7001',2019,12000.5,4100)")

    cases = [
        (1, 'D-ABCD', '2024-01-05', '34',
         'AIR DATA MODULE FAULT ON APPROACH', 'REPLACED AIR DATA MODULE'),
        (2, 'D-ABCD', '2024-01-20', '34',
         'ADM DISAGREE REPEATED', 'RESEATED CONNECTOR'),
        (3, 'D-EFGH', '2023-11-02', '27', LONG_NARRATIVE, 'ADJUSTED FLAP'),
    ]
    for case_id, tail, when, ata, text, rectification in cases:
        con.execute(
            "INSERT INTO defect(id,aircraft_tail,reported_at,ata_ref,"
            "fault_code,defect_text,rectification_text,source,tool_assisted,"
            "sdr_year) VALUES(?,?,?,?,'34-11002',?,?,'sdr',0,?)",
            (case_id, tail, when, ata, text, rectification, int(when[:4])))
        con.execute("INSERT INTO case_fts(rowid,defect_text) VALUES(?,?)",
                    (case_id, text))
    con.execute("INSERT INTO defect_action(defect_id,action_type,part_name,"
                "part_number,task_number) VALUES(1,'replaced',"
                "'Air Data Module','PN-9911','34-11-01-400-801')")
    con.execute("INSERT INTO defect_finding(defect_id,finding_type,"
                "finding_text) VALUES(1,'confirmed_fault',"
                "'Internal transducer drift')")
    con.execute("INSERT INTO repeat_norm(defect_id,repeat_defect_id,"
                "days_apart,similarity,same_action,ata_chapter) "
                "VALUES(1,2,15,0.81,1,'34')")

    con.commit()
    con.close()


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path / "corpus.db"
    _build_corpus(path)
    return path


@pytest.fixture
def registry(corpus):
    """A registry over its own read-only connection to the corpus file.

    Constructed with `db_path` rather than a live connection so the read-only
    contract in `ToolRegistry.connection` is the thing under test, not a
    detail bypassed by handing it a writable handle.
    """
    reg = T.ToolRegistry(db_path=corpus)
    yield reg
    reg.close()


@pytest.fixture
def engineer():
    return User(1, "eng", "E. Engineer", "engineer", False)


@pytest.fixture
def administrator():
    return User(2, "adm", "A. Admin", "admin", False)


@pytest.fixture
def evidence(registry, engineer):
    """The tool results a grounded answer is allowed to have been built from."""
    return [
        registry.call("search_manual_tasks",
                      {"query": "air data module", "tail": "D-ABCD"}, engineer),
        registry.call("search_similar_cases",
                      {"symptom": "air data module fault"}, engineer),
        registry.call("get_case_evidence", {"case_ids": [1, 2]}, engineer),
    ]


@pytest.fixture
def answer():
    """A well-formed answer that is grounded in the `evidence` fixture.

    Every test in the grounding section starts from this and breaks exactly
    one thing, so a failure names the rule that was broken rather than leaving
    the reader to diff two payloads.
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
            "task_number": "34-11-01-400-801",
            "title": "Install the Air Data Module",
            "revision": "R12",
            "effectivity_result": "applicable",
            "retrieval_evidence": {"channel": "fts"},
        }],
        "recommended_pages": [],
        "supporting_evidence": ["a prior case replaced the air data module"],
        "contradicting_evidence": ["a prior case reseated a connector instead"],
        "missing_information": ["no closure state has been imported"],
        "limitations": ["absence of a case is not evidence "
                        "that nothing happened"],
        "abstained": False,
        "abstention_reason": "",
        "model_version": "nemotron-test",
        "index_version": "idx-test",
        "evidence_ids": ["task:34-11-01-400-801", "defect:1", "defect:2"],
        "narrative": "Prior reports on this tail point at the air data module.",
    }


# ── argument validation: nothing is coerced ─────────────────────────────

def test_a_limit_above_the_maximum_is_refused_not_clamped(registry, engineer):
    """The whole point of the maximum.

    Silently returning 25 rows to a caller that asked for 1,000 hands the
    model a sample it will reason about as though it were the population, and
    nothing downstream can tell that happened.
    """
    with pytest.raises(T.ToolError) as excinfo:
        registry.call("search_manual_tasks", {"query": "x", "limit": 26},
                      engineer)
    message = str(excinfo.value)
    assert "above the maximum of 25" in message
    assert "not silently truncated" in message


@pytest.mark.parametrize("arguments, fragment", [
    ({"query": "x", "limit": 0}, "below the minimum"),
    ({"query": "x", "limit": "5"}, "must be a whole number, got str"),
    ({"query": "x", "limit": True}, "must be a whole number, got bool"),
    ({"query": 5}, "must be text, got int"),
    ({"query": "   "}, "is empty"),
    ({"limit": 5}, "query is required"),
    ({"query": "x", "chapter": "34"}, "unknown argument(s) chapter"),
])
def test_a_bad_argument_is_refused(registry, engineer, arguments, fragment):
    """One case per way an argument can be wrong, each named in the message.

    The message matters as much as the refusal: a model that is told which
    argument was wrong can fix it and retry, and one that is told "invalid
    input" will try the same call again.
    """
    with pytest.raises(T.ToolError) as excinfo:
        registry.call("search_manual_tasks", arguments, engineer)
    assert fragment in str(excinfo.value)


def test_arguments_that_are_not_an_object_are_refused(registry, engineer):
    with pytest.raises(T.ToolError) as excinfo:
        registry.call("search_manual_tasks", "query=x", engineer)
    assert "must be an object" in str(excinfo.value)


@pytest.mark.parametrize("case_ids, fragment", [
    (list(range(11)), "at most 10 may be asked for at once"),
    ([1, "2"], "case_ids[1] must be a whole number"),
    ("1,2", "must be a list"),
    ([], "empty list"),
])
def test_a_bad_list_argument_is_refused(registry, engineer, case_ids, fragment):
    with pytest.raises(T.ToolError) as excinfo:
        registry.call("get_case_evidence", {"case_ids": case_ids}, engineer)
    assert fragment in str(excinfo.value)


def test_an_optional_argument_left_out_is_not_invented(registry, engineer):
    """An absent `tail` means "not checked", never a guess at which airframe."""
    result = registry.call("check_effectivity",
                           {"task_number": "34-11-01-400-801"}, engineer)
    assert result.ok
    assert result.data["asked_for"] == {"tail": None, "msn": None,
                                        "line_number": None}
    assert result.data["results"][0]["effectivity_result"] == "not_checked"


# ── permission ──────────────────────────────────────────────────────────

def test_permissions_are_read_off_the_roles_table_login_uses():
    """Not a second copy of the role list that can drift from the first."""
    for role, spelled in ROLES.items():
        user = User(1, "u", "U", role, False)
        assert T.permissions_of(user) == frozenset(spelled.split(","))


def test_an_unknown_role_holds_nothing(registry):
    """Failing closed matters more here than anywhere else in the file."""
    stranger = User(9, "guest", "Guest", "contractor", False)
    assert T.permissions_of(stranger) == frozenset()
    with pytest.raises(T.PermissionDenied):
        registry.call("search_manual_tasks", {"query": "x"}, stranger)


def test_no_signed_in_user_may_call_anything(registry):
    assert T.permissions_of(None) == frozenset()
    with pytest.raises(T.PermissionDenied) as excinfo:
        registry.call("search_manual_tasks", {"query": "x"}, None)
    assert "no user" in str(excinfo.value)


def test_a_role_without_the_permission_is_refused(registry, administrator):
    """`record_engineer_feedback` is gated on `notes`, which admin lacks.

    This is the only permission boundary the registry actually draws — every
    other tool is gated on `read`, which both roles carry. Writing engineering
    judgement that becomes label data is an engineer's act, so the direction
    of this refusal is deliberate and not an oversight.
    """
    with pytest.raises(T.PermissionDenied) as excinfo:
        registry.call("record_engineer_feedback",
                      {"defect_id": 1, "verdict": "yes"}, administrator)
    message = str(excinfo.value)
    assert "requires the 'notes' permission" in message
    assert "role 'admin'" in message


def test_a_denied_call_never_reaches_the_handler(registry, administrator):
    """The refusal comes before argument validation and before any query.

    Asserted through a call whose arguments are also invalid: if the handler
    or the validator ran first, this would raise the argument error instead.
    """
    with pytest.raises(T.PermissionDenied):
        registry.call("record_engineer_feedback", {"defect_id": "one"},
                      administrator)


def test_describe_hides_a_tool_the_user_may_not_call(registry, administrator,
                                                     engineer):
    """Offering a model a tool it will be refused on wastes a turn."""
    for_admin = {spec["name"] for spec in registry.describe(administrator)}
    for_engineer = {spec["name"] for spec in registry.describe(engineer)}
    assert "record_engineer_feedback" in for_engineer
    assert "record_engineer_feedback" not in for_admin
    assert registry.describe() and len(registry.describe()) > len(for_admin)


def test_every_tool_is_gated_on_a_permission_some_role_grants(registry):
    """A tool nobody may call is a tool that will be discovered in production."""
    granted: set[str] = set()
    for spelled in ROLES.values():
        granted |= {p.strip() for p in spelled.split(",") if p.strip()}
    for spec in registry.describe():
        assert spec["permission"] in granted, spec["name"]


# ── unknown tools are a different failure from denied ones ──────────────

def test_an_unknown_tool_name_is_refused(registry, engineer):
    with pytest.raises(T.UnknownTool) as excinfo:
        registry.call("delete_all_defects", {}, engineer)
    assert "no tool named 'delete_all_defects'" in str(excinfo.value)


def test_an_unknown_name_is_not_reported_as_a_permission_problem(registry):
    """An unknown name is a bug in the caller; a denied one is a policy
    decision about a real person. Telling them apart is the point of having
    two classes, and the name is checked first so a user who holds nothing
    still gets the truthful answer."""
    with pytest.raises(T.UnknownTool):
        registry.call("delete_all_defects", {}, None)
    assert not issubclass(T.UnknownTool, T.PermissionDenied)
    assert issubclass(T.UnknownTool, T.ToolError)
    assert issubclass(T.PermissionDenied, T.ToolError)


# ── results are bounded ─────────────────────────────────────────────────

def test_results_are_bounded_and_say_when_they_were_cut(registry, engineer):
    result = registry.call(
        "search_similar_cases",
        {"symptom": "module fault flap adm disagree connector", "limit": 1},
        engineer)
    assert result.ok
    assert result.data["matched"] > result.data["returned"]
    assert result.data["returned"] == 1 == len(result.data["results"])
    assert result.truncated


def test_a_result_that_fits_is_not_marked_truncated(registry, engineer):
    result = registry.call("get_aircraft_history", {"tail": "D-ABCD"},
                           engineer)
    assert result.ok
    assert result.data["returned"] == 2
    assert not result.truncated


def test_the_limit_the_caller_asked_for_is_reported_back(registry, engineer):
    result = registry.call("search_manual_tasks",
                           {"query": "air data module", "limit": 3}, engineer)
    assert result.data["limit"] == 3
    assert len(result.data["results"]) <= 3


def test_a_case_narrative_is_truncated_to_the_snippet_budget(registry,
                                                             engineer):
    """A tool result must not become a way of streaming the corpus into a
    context window, so the one text a model may read is capped."""
    result = registry.call("get_case_evidence", {"case_ids": [3]}, engineer)
    assert result.ok
    symptom = result.data["results"][0]["symptom"]
    assert len(LONG_NARRATIVE) > T.SNIPPET
    assert len(symptom) <= T.SNIPPET


# ── provenance on every result ──────────────────────────────────────────

@pytest.mark.parametrize("tool, arguments", [
    ("search_manual_tasks", {"query": "air data module"}),
    ("search_similar_cases", {"symptom": "air data module fault"}),
    ("get_case_evidence", {"case_ids": [1]}),
    ("get_aircraft_history", {"tail": "D-ABCD"}),
    ("get_repeat_defects", {}),
    ("get_manual_metadata", {}),
    ("check_effectivity", {"task_number": "34-11-01-400-801"}),
])
def test_every_successful_result_carries_a_source_and_a_freshness(
        registry, engineer, tool, arguments):
    """A row with no provenance will eventually be read as current."""
    result = registry.call(tool, arguments, engineer)
    assert result.ok, result.error
    assert result.source.strip()
    assert result.freshness.strip()


def test_freshness_is_the_newest_report_not_the_wall_clock(registry, engineer):
    """The corpus is a static download. Stamping it with today's date would
    claim a currency the data does not have."""
    result = registry.call("search_similar_cases", {"symptom": "air data"},
                           engineer)
    assert result.freshness == "newest report 2024-01-20"


def test_manual_freshness_is_the_ingest_stamp(registry, engineer):
    result = registry.call("get_manual_metadata", {}, engineer)
    assert result.freshness == "2025-08-01T10:00:00Z"


def test_results_carry_stable_ids_for_the_grounding_check(registry, engineer):
    """Grounding is a set-membership test, which needs ids and not titles."""
    tasks = registry.call("search_manual_tasks",
                          {"query": "air data module"}, engineer)
    cases = registry.call("get_case_evidence", {"case_ids": [1]}, engineer)
    assert tasks.data["results"][0]["evidence_id"] == "task:34-11-01-400-801"
    assert cases.data["results"][0]["evidence_id"] == "defect:1"


# ── what is real and what is not ────────────────────────────────────────

@pytest.mark.parametrize("tool, arguments, missing_fragment", [
    ("get_page_metadata", {"manual_id": 1, "source_page": 203}, "page index"),
    ("check_document_authorization",
     {"manual_id": 1, "task_number": "34-11-01-400-801"},
     "authorization records"),
    ("get_compliance_context", {"tail": "D-ABCD"}, "compliance_item"),
    ("get_live_aircraft_position", {"tail": "D-ABCD"},
     "online operations module"),
    ("get_airport_movements", {"icao": "EDDF"}, "online operations module"),
    ("record_engineer_feedback", {"defect_id": 1, "verdict": "yes"},
     "feedback table"),
    ("get_open_defects", {}, "defect_closure"),
])
def test_a_tool_with_no_data_behind_it_names_what_is_missing(
        registry, engineer, tool, arguments, missing_fragment):
    """Not an empty success. An empty success is indistinguishable from
    "nothing exists", and the two mean opposite things to an engineer."""
    result = registry.call(tool, arguments, engineer)
    assert not result.ok
    assert result.missing, f"{tool} reported unavailable without saying what"
    assert any(missing_fragment in item for item in result.missing)
    assert result.data.get("unavailable") is True
    assert result.data.get("detail")
    assert result.error.startswith("unavailable — ")


def test_an_unavailable_tool_returns_no_rows_at_all(registry, engineer):
    """There is no partial answer here — no `results` key to be misread as an
    empty list of findings."""
    result = registry.call("get_compliance_context", {"tail": "D-ABCD"},
                           engineer)
    assert "results" not in result.data


def test_a_tool_that_matched_nothing_is_a_success_that_says_what_it_searched(
        registry, engineer):
    """The other half of the rule. A real search that found nothing must not
    look like a tool that could not run, so it succeeds, reports `matched: 0`,
    and describes the population it looked in."""
    result = registry.call("search_manual_tasks",
                           {"query": "hydraulic reservoir sight glass"},
                           engineer)
    assert result.ok
    assert result.data["matched"] == 0
    assert result.data["note"]
    assert "AMM rev R12" in result.data["searched"]


def test_a_case_search_that_matched_nothing_carries_the_sdr_caveat(
        registry, engineer):
    """Absence of a case is not evidence that nothing happened — SDR is a
    reportable-occurrence sample and systematically excludes routine work."""
    result = registry.call("search_similar_cases",
                           {"symptom": "hydraulic reservoir sight glass"},
                           engineer)
    assert result.ok
    assert result.data["matched"] == 0
    assert "not evidence that nothing happened" in result.data["caveat"]


def test_a_missing_database_is_named_rather_than_read_as_no_results(tmp_path,
                                                                    engineer):
    absent = T.ToolRegistry(db_path=tmp_path / "never-ingested.db")
    result = absent.call("search_manual_tasks", {"query": "x"}, engineer)
    assert not result.ok
    assert result.missing == ("aivionics.db",)
    assert "no database at" in result.error


def test_a_task_that_is_not_in_the_index_cannot_be_checked(registry, engineer):
    """A third state, distinct from unavailable: the tool ran, the airframe
    question is answerable in principle, and this particular task is not
    there — so it must not be recommended either."""
    result = registry.call("check_effectivity", {"task_number": "99-99-99"},
                           engineer)
    assert not result.ok
    assert result.missing == ()
    assert "must not be recommended" in result.error


def test_an_absent_case_id_is_named_rather_than_dropped(registry, engineer):
    """A caller that asked for three cases and silently got two would draw a
    conclusion from a set it never knew was incomplete."""
    result = registry.call("get_case_evidence", {"case_ids": [1, 2, 4242]},
                           engineer)
    assert result.ok
    assert result.data["requested"] == 3
    assert result.data["matched"] == 2
    assert result.data["not_found"] == [4242]


# ── the read-only contract ──────────────────────────────────────────────

def test_the_registry_connection_cannot_write(registry):
    """`mode=ro` with `query_only=ON` is the contract, not a precaution.

    The case base is the department's evidence and nothing in the model path
    has any business changing it.
    """
    con = registry.connection()
    assert con is not None
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO defect(id,aircraft_tail) VALUES(999,'X')")


def test_no_tool_hands_a_model_a_task_body(registry, engineer):
    """Standing rule 1: the tool indexes into controlled data, it does not
    reproduce it. Task 1 has a body in the corpus, so this is a test of a
    decision and not of an empty column."""
    result = registry.call("search_manual_tasks",
                           {"query": "air data module"}, engineer)
    row = result.data["results"][0]
    assert "body" not in row
    assert not any(BODY_MARKER in str(value) for value in row.values())


def test_a_result_that_is_a_locator_still_carries_its_effectivity(registry,
                                                                  engineer):
    """What comes back is what you would write on a slip of paper before
    walking to the approved source — including whether it applies."""
    result = registry.call("search_manual_tasks",
                           {"query": "air data module", "tail": "D-ABCD"},
                           engineer)
    row = result.data["results"][0]
    assert row["task_number"] == "34-11-01-400-801"
    assert row["revision"] == "R12"
    assert row["effectivity_result"] == "applicable"
    assert row["effectivity_note"] == ""


def test_an_unresolved_locator_carries_the_notice_the_print_path_uses(
        registry, engineer):
    result = registry.call("search_manual_tasks",
                           {"query": "air data module", "tail": "D-ZZZZ"},
                           engineer)
    row = result.data["results"][0]
    assert row["effectivity_result"] in S.EFFECTIVITY_NEEDS_CLOSING
    assert row["effectivity_note"] == S.UNRESOLVED_NOTICE


# ── tool definitions ────────────────────────────────────────────────────

def test_a_definition_forbids_arguments_it_did_not_declare(registry):
    """`additionalProperties: False` is what stops a model inventing an
    argument that the validator would then have to refuse a turn later."""
    for spec in registry.describe():
        schema = spec["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])


def test_a_definition_publishes_its_bounds(registry):
    spec = next(s for s in registry.describe()
                if s["name"] == "search_manual_tasks")
    limit = spec["input_schema"]["properties"]["limit"]
    assert limit["minimum"] == 1
    assert limit["maximum"] == 25
    assert "refused, not truncated" in limit["description"]


def test_a_tool_with_no_data_says_so_in_its_availability(registry):
    unavailable = {s["name"] for s in registry.describe()
                   if s["availability"] != "backed by the local database"}
    assert unavailable == {
        "get_page_metadata", "check_document_authorization",
        "get_live_aircraft_position", "get_airport_movements",
        "record_engineer_feedback"}


# ── schema validation: reject, never repair ─────────────────────────────

def test_the_evidence_levels_are_exactly_four_categories():
    """A closed vocabulary, and not one of them is a number.

    The six-model review killed the confidence percentage: these labels record
    what engineers did, not what was correct, so no figure derived from them
    means what a percentage would appear to mean.
    """
    assert S.EVIDENCE_LEVELS == ("strong", "limited", "conflicting",
                                "insufficient")
    assert set(S.EVIDENCE_MEANING) == set(S.EVIDENCE_LEVELS)


@pytest.mark.parametrize("level", ["high", "87% confident", "STRONG", "",
                                   "probable"])
def test_an_unknown_evidence_level_is_rejected(answer, level):
    answer["hypotheses"][0]["evidence_level"] = level
    with pytest.raises(S.SchemaError) as excinfo:
        S.parse_assistant_answer(answer)
    assert "evidence_level" in str(excinfo.value)


@pytest.mark.parametrize("rank, fragment", [
    (0, "outside 1..10"),
    (11, "outside 1..10"),
    ("1", "must be a whole number, got str"),
    (True, "must be a whole number, got bool"),
])
def test_a_bad_rank_is_rejected(answer, rank, fragment):
    answer["hypotheses"][0]["rank"] = rank
    with pytest.raises(S.SchemaError) as excinfo:
        S.parse_assistant_answer(answer)
    assert fragment in str(excinfo.value)


def test_ranks_must_be_one_to_n_without_duplicates(answer):
    """A duplicated rank means the model was not ordering anything, and the
    engineer reads the first entry as the best one regardless."""
    second = dict(answer["hypotheses"][0], cause_id="c2",
                  cause="Pitot heat failure", rank=1)
    answer["hypotheses"].append(second)
    with pytest.raises(S.SchemaError) as excinfo:
        S.parse_assistant_answer(answer)
    assert "are not 1..2 without duplicates" in str(excinfo.value)


def test_a_cause_with_no_supporting_case_ids_is_rejected_at_the_schema(answer):
    """A hypothesis with nothing behind it is not a hypothesis, it is a guess."""
    answer["hypotheses"][0]["supporting_case_ids"] = []
    with pytest.raises(S.SchemaError) as excinfo:
        S.parse_assistant_answer(answer)
    assert "supporting_case_ids" in str(excinfo.value)


def test_a_missing_required_field_is_rejected(answer):
    del answer["model_version"]
    with pytest.raises(S.SchemaError) as excinfo:
        S.parse_assistant_answer(answer)
    assert "'model_version' is missing" in str(excinfo.value)


def test_a_field_of_the_wrong_type_is_rejected(answer):
    answer["hypotheses"][0]["cause"] = 3
    with pytest.raises(S.SchemaError) as excinfo:
        S.parse_assistant_answer(answer)
    assert "must be text, got int" in str(excinfo.value)


def test_a_payload_that_is_not_an_object_is_rejected():
    with pytest.raises(S.SchemaError) as excinfo:
        S.parse_assistant_answer(["hypotheses"])
    assert "expected an object, got list" in str(excinfo.value)


def test_an_unknown_manual_type_is_rejected(answer):
    answer["recommended_documents"][0]["manual_type"] = "QRH"
    with pytest.raises(S.SchemaError):
        S.parse_assistant_answer(answer)


def test_an_unknown_effectivity_state_is_rejected(answer):
    answer["recommended_documents"][0]["effectivity_result"] = "probably"
    with pytest.raises(S.SchemaError):
        S.parse_assistant_answer(answer)


def test_a_recommendation_with_no_retrieval_evidence_is_rejected(answer):
    """A recommendation from nowhere. The document may be real and the reason
    for surfacing it still be invented."""
    answer["recommended_documents"][0]["retrieval_evidence"] = {}
    with pytest.raises(S.SchemaError) as excinfo:
        S.parse_assistant_answer(answer)
    assert "recommendation from nowhere" in str(excinfo.value)


def test_an_unknown_evidence_kind_is_rejected():
    assert S.evidence_id("defect", 146266) == "defect:146266"
    with pytest.raises(S.SchemaError):
        S.evidence_id("hunch", 1)


def test_an_answer_with_nothing_in_it_must_abstain(answer):
    """"No hypotheses and no abstention" is silence dressed as a result."""
    answer["hypotheses"] = []
    with pytest.raises(S.SchemaError) as excinfo:
        S.parse_assistant_answer(answer)
    assert "must set abstained and say why" in str(excinfo.value)


def test_an_abstention_must_say_why(answer):
    answer["hypotheses"] = []
    answer["abstained"] = True
    answer["abstention_reason"] = ""
    with pytest.raises(S.SchemaError) as excinfo:
        S.parse_assistant_answer(answer)
    assert "abstention_reason" in str(excinfo.value)


@pytest.mark.parametrize("reply", [
    "{not json at all",
    "",
    "```json\n{\"abstained\": true,}\n```",
    "I think the answer is probably the air data module.",
])
def test_invalid_json_is_rejected_rather_than_repaired(reply):
    """A malformed answer that is quietly patched into a well-formed one reads
    exactly like an answer that was right the first time."""
    with pytest.raises(S.SchemaError):
        S.parse_answer_json(reply)


def test_a_reasoning_preamble_and_a_fence_are_tolerated(answer):
    """The tolerance is about packaging, never about content: Nemotron emits a
    preamble and a fence every time, and neither changes what was said."""
    import json
    reply = ("Let me think about this step by step.\n"
             "```json\n" + json.dumps(answer) + "\n```\n"
             "Hope that helps.")
    parsed = S.parse_answer_json(reply)
    assert parsed.model_version == "nemotron-test"
    assert parsed.hypotheses[0].cause_id == "c1"


# ── grounding: admissible, not merely well formed ───────────────────────

def test_a_grounded_answer_is_accepted(answer, evidence):
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert report.accepted, report.describe()
    assert report.codes == ()


def test_enforce_grounding_returns_the_answer_it_was_given(answer, evidence):
    parsed = S.parse_assistant_answer(answer)
    assert S.enforce_grounding(parsed, evidence) is parsed


def test_a_hallucinated_task_number_is_rejected(answer, evidence):
    """The model cites a task that is not in the retrieved candidate set."""
    answer["recommended_documents"][0]["task_number"] = "34-99-99-400-801"
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert "ungrounded_task_number" in report.codes
    assert "34-99-99-400-801" in report.describe()


def test_a_task_number_invented_in_prose_is_rejected_too(answer, evidence):
    """Structured fields are the easy half. A plausible-looking invention
    lands in the sentence, which is the only place that claim lives."""
    answer["narrative"] = ("Follow 27-99-00-820-810 after the module is "
                           "installed.")
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert "ungrounded_task_number" in report.codes
    assert "27-99-00-820-810" in report.describe()


def test_a_hallucinated_page_is_rejected(answer, evidence):
    """Every page citation fails today, because no page index has been built.

    That is the correct behaviour and not a placeholder: a page number nobody
    can verify against a revision is precisely the citation that sends an
    engineer to the wrong sheet.
    """
    answer["recommended_pages"] = [{
        "manual_id": 1,
        "revision": "R12",
        "source_page": 203,
        "printed_page_label": "34-11-01 Page 203",
        "task_number": "34-11-01-400-801",
        "task_relationship": "contains_task_start",
    }]
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert "ungrounded_page" in report.codes
    assert "no page index has been built" in report.describe()


def test_an_unsupported_numeral_is_rejected(answer, evidence):
    """Standing rule 4: every numeral shown to an engineer comes from the
    database. "In 87 of the cases" is a statistic generated out of nothing."""
    answer["narrative"] = "In 87 of the prior cases the module was replaced."
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert "unsupported_numeral" in report.codes
    assert "'87'" in report.describe()


def test_a_numeral_in_any_free_text_field_is_checked(answer, evidence):
    """All of them, uniformly. Exempting the field that describes the absence
    of data would leave one place a fabricated count could survive."""
    answer["limitations"] = ["only 63 of the fleet were sampled"]
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert "unsupported_numeral" in report.codes
    assert "'63'" in report.describe()


def test_a_task_number_in_prose_is_not_counted_as_six_invented_numerals(
        answer, evidence):
    """Task numbers are stripped before numerals are counted; they are checked
    as identifiers instead."""
    answer["narrative"] = "Task 34-11-01-400-801 is the installation task."
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert report.accepted, report.describe()


def test_small_counts_that_are_ordinary_english_are_allowed(answer, evidence):
    """"3 of the 4 checks" is a sentence, not a claim about the corpus. The
    same allowance `summarise.validate` already makes."""
    answer["narrative"] = "3 of the 4 checks in this area are unaffected."
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert report.accepted, report.describe()


def test_a_case_id_no_tool_returned_is_rejected(answer, evidence):
    """The subtler half of the cause rule: ids that are well formed and point
    at records the tools never handed over."""
    answer["hypotheses"][0]["supporting_case_ids"] = [4242]
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert "unknown_case_id" in report.codes


def test_a_cause_with_no_supporting_evidence_ids_is_rejected(answer, evidence):
    """The schema refuses an empty list first, so this is reached only by
    building the object directly — which is exactly what a caller assembling
    an answer in code would do, and the reason the check is duplicated here.
    """
    parsed = S.parse_assistant_answer(answer)
    bare = S.CauseCandidate(
        cause_id="c1", cause="Air data module internal drift", rank=1,
        evidence_level=S.INSUFFICIENT, supporting_case_ids=())
    report = S.check_grounding(
        S.AssistantAnswer(**{**parsed.__dict__, "hypotheses": (bare,)}),
        evidence)
    assert "cause_without_evidence" in report.codes


def test_an_evidence_id_no_tool_issued_is_rejected(answer, evidence):
    answer["evidence_ids"].append("defect:987654")
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert "unknown_evidence_id" in report.codes


def test_unresolved_effectivity_fails_closed(answer, evidence):
    """Standing rule 8. Anything not positively applicable has to carry the
    notice, in the answer's own limitations, where the engineer will read it."""
    answer["recommended_documents"][0]["effectivity_result"] = "unresolved"
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert "effectivity_not_failed_closed" in report.codes
    assert S.UNRESOLVED_NOTICE in report.describe()


@pytest.mark.parametrize("state", ["not_applicable", "unresolved",
                                   "not_checked"])
def test_every_state_that_is_not_applicable_must_be_failed_closed(
        answer, evidence, state):
    answer["recommended_documents"][0]["effectivity_result"] = state
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert "effectivity_not_failed_closed" in report.codes


def test_carrying_the_notice_clears_the_violation(answer, evidence):
    answer["recommended_documents"][0]["effectivity_result"] = "unresolved"
    answer["limitations"].append(S.UNRESOLVED_NOTICE)
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert report.accepted, report.describe()


def test_a_failed_tool_contributes_no_evidence():
    """The whole reason an unavailable tool must fail loudly.

    A result carrying rows but flagged `ok=False` grounds nothing — otherwise
    an empty success would look like evidence of absence and nothing here
    could tell the difference.
    """
    pretend = T.ToolResult(
        ok=False, tool="search_manual_tasks",
        data={"results": [{"task_number": "34-11-01-400-801",
                           "evidence_id": "task:34-11-01-400-801",
                           "defect_id": 1}]},
        error="database error — no such table: task")
    collected = S.ToolEvidence.collect([pretend])
    assert collected.task_numbers == frozenset()
    assert collected.evidence_ids == frozenset()
    assert collected.case_ids == frozenset()


def test_an_answer_built_on_an_unavailable_tool_is_refused(answer, registry,
                                                           engineer):
    """End to end: the tool said it could not answer, and the model answered
    anyway."""
    unavailable = [registry.call("get_compliance_context", {"tail": "D-ABCD"},
                                 engineer)]
    with pytest.raises(S.GroundingError) as excinfo:
        S.enforce_grounding(S.parse_assistant_answer(answer), unavailable)
    assert "ungrounded_task_number" in excinfo.value.report.codes


def test_a_rejected_answer_is_not_returned_with_the_bad_parts_removed(
        answer, evidence):
    """There is no third outcome. An answer that cited one invented task among
    four real ones is an answer whose reasoning included the invention."""
    answer["recommended_documents"][0]["task_number"] = "34-99-99-400-801"
    answer["narrative"] = "In 87 of the prior cases the module was replaced."
    with pytest.raises(S.GroundingError) as excinfo:
        S.enforce_grounding(S.parse_assistant_answer(answer), evidence)
    codes = excinfo.value.report.codes
    assert "ungrounded_task_number" in codes
    assert "unsupported_numeral" in codes


def test_the_report_carries_every_violation_not_the_first(answer, evidence):
    answer["hypotheses"][0]["supporting_case_ids"] = [4242]
    answer["evidence_ids"].append("defect:987654")
    report = S.check_grounding(S.parse_assistant_answer(answer), evidence)
    assert len(report.violations) >= 2
    assert not report.accepted


# ── abstention ──────────────────────────────────────────────────────────

def test_an_abstention_is_a_valid_grounded_answer(evidence):
    """Where the case base is silent the correct output is an abstention, not
    a ranked cause with an `insufficient` label attached to make it look
    accounted for."""
    payload = {
        "interpreted_complaint": {
            "normalized_symptom": "intermittent noise in the forward galley",
            "evidence_level": "insufficient",
            "missing_information": ["no ATA chapter could be inferred"],
        },
        "hypotheses": [],
        "recommended_documents": [],
        "recommended_pages": [],
        "supporting_evidence": [],
        "contradicting_evidence": [],
        "missing_information": ["no prior case resembles this complaint"],
        "limitations": ["the case base does not answer this question"],
        "abstained": True,
        "abstention_reason": ("no prior case resembles this complaint and no "
                              "manual task matched the symptom"),
        "model_version": "nemotron-test",
        "index_version": "idx-test",
        "evidence_ids": [],
        "narrative": "",
    }
    parsed = S.parse_assistant_answer(payload)
    assert parsed.abstained
    assert parsed.hypotheses == ()
    assert S.enforce_grounding(parsed, evidence) is parsed


def test_an_abstention_still_may_not_invent_a_task(evidence):
    """Declining to answer is not a way around grounding."""
    payload = {
        "interpreted_complaint": {"normalized_symptom": "noise",
                                  "evidence_level": "insufficient"},
        "hypotheses": [],
        "recommended_documents": [],
        "recommended_pages": [],
        "supporting_evidence": [],
        "contradicting_evidence": [],
        "missing_information": [],
        "limitations": [],
        "abstained": True,
        "abstention_reason": "nothing matched; try 49-11-00-710-801 manually",
        "model_version": "nemotron-test",
        "index_version": "idx-test",
        "evidence_ids": [],
    }
    report = S.check_grounding(S.parse_assistant_answer(payload), evidence)
    assert "ungrounded_task_number" in report.codes
