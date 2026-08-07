"""Gate 1 regression tests — one per known gotcha, plus the plugin seam."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aivionics.parsers.base import OEMParser, register, registered, get_parser
from aivionics.parsers.boeing import (
    BoeingAMMParser, despace_tasknum, classify_function)


def test_gotcha1_engine_sixth_group_prefix():
    # engine chapters carry a letter prefix / lettered seq (G73-..., ...-A73)
    assert despace_tasknum("G73-00-00-810-A73") == "G73-00-00-810-A73"
    assert despace_tasknum("34-11-01-400-801") == "34-11-01-400-801"
    # ...and a trailing 6th group. Missing this yielded zero tasks for the
    # whole of chapters 71/73/75/77/79.
    assert despace_tasknum("71-00-00-800-801-G00") == "71-00-00-800-801-G00"


def test_gotcha3_whitespace_inside_numbers():
    assert despace_tasknum("34-41-1 1-020-002") == "34-41-11-020-002"
    assert despace_tasknum("34 -11- 01 -400- 801") == "34-11-01-400-801"


# Layout below is the one measured in the real 737 MAX AMM PDFs (2026-08-08):
# a TOC line is followed by a page number and the effectivity column, a real
# heading by a paragraph number and then the title.
TOC = ("Pitot Probe Removal\nTASK 34-11-01-000-801\n401\nTBC ALL\n"
       "Pitot Probe Installation\nTASK 34-11-01-400-801\n404\nTBC ALL\n")
BODY = ("TASK 34-11-01-400-801\n3.\nPITOT PROBE — REMOVAL\n(Figure 401)\n"
        "EFFECTIVITY TBC ALL\nA.\nGeneral\n"
        "WARNING: PITOT PROBES ARE HOT.\n"
        "SUBTASK 34-11-01-400-001\n(1)\ndo things\nEND OF TASK\n")


def test_gotcha2_toc_entries_rejected():
    tasks, toc_count = BoeingAMMParser().parse_text(TOC + BODY)
    assert len(tasks) == 1                       # both TOC lines rejected
    assert tasks[0].task_number == "34-11-01-400-801"
    assert toc_count == 2                        # TOC counted for coverage
    assert tasks[0].warnings and "HOT" in tasks[0].warnings[0]
    assert "TBC ALL" in tasks[0].effectivity_raw
    assert tasks[0].title == "PITOT PROBE — REMOVAL"


def test_gotcha5_subtask_is_not_a_task():
    """SUBTASK contains TASK and sits right before END OF TASK, so a naive
    scan stores the subtask number as the task number. Measured on chapter
    34 before the fix: 173 of 244 rows were subtasks."""
    tasks, _ = BoeingAMMParser().parse_text(TOC + BODY)
    assert [t.task_number for t in tasks] == ["34-11-01-400-801"]
    assert all("400-001" not in t.task_number for t in tasks)


def test_engine_chapter_sixth_group_task_is_extracted():
    text = ("Engine Operation Limits\nTASK 71-00-00-800-801-G00\n201.01\nTBC ALL\n"
            "TASK 71-00-00-800-801-G00\n2.\nEngine Operation Limits\n(Figure 201)\n"
            "A.\nGeneral\nEND OF TASK\n")
    tasks, toc_count = BoeingAMMParser().parse_text(text)
    assert [t.task_number for t in tasks] == ["71-00-00-800-801-G00"]
    assert toc_count == 1                        # decimal page number parsed
    assert tasks[0].function_code == "800"


def test_gotcha4_ddg_ref_in_title():
    text = ("TASK 34-11-01-400-801\n3.\n"
            "PITOT PROBE — REMOVAL (DDG 34-11-01)\n(Figure 401)\n"
            "body\nEND OF TASK\n")
    tasks, _ = BoeingAMMParser().parse_text(text)
    assert "DDG" not in tasks[0].title


def test_function_classification():
    assert classify_function("810") == "diagnostic"
    assert classify_function("200") == "diagnostic"
    assert classify_function("710") == "diagnostic"
    assert classify_function("400") == "action"
    assert classify_function("000") == "action"


def test_plugin_seam_second_oem_registers_without_core_change():
    @register
    class StubAirbusParser(OEMParser):
        oem = "airbus-stub"

        def extract_chapter(self, path):
            return [], 0

    assert "airbus-stub" in registered()
    assert isinstance(get_parser("airbus-stub"), StubAirbusParser)
