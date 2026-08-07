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


def test_gotcha3_whitespace_inside_numbers():
    assert despace_tasknum("34-41-1 1-020-002") == "34-41-11-020-002"
    assert despace_tasknum("34 -11- 01 -400- 801") == "34-11-01-400-801"


def test_gotcha2_toc_entries_rejected():
    text = (
        "TASK 34-11-01-400-801\nPitot Probe Removal (TOC line)\n"       # TOC
        "TASK 34-11-01-400-802\nPitot Probe Installation (TOC line)\n"  # TOC
        "TASK 34-11-01-400-801\nPITOT PROBE — REMOVAL\nEFFECTIVITY TBC ALL\n"
        "WARNING: PITOT PROBES ARE HOT.\nSTEP 1 do things\nEND OF TASK\n"
    )
    tasks, mentions = BoeingAMMParser().parse_text(text)
    assert len(tasks) == 1                       # both TOC lines rejected
    assert tasks[0].task_number == "34-11-01-400-801"
    assert mentions == 2                         # distinct numbers mentioned
    assert tasks[0].warnings and "HOT" in tasks[0].warnings[0]
    assert "TBC ALL" in tasks[0].effectivity_raw


def test_gotcha4_ddg_ref_in_title():
    text = ("TASK 34-11-01-400-801\n"
            "PITOT PROBE — REMOVAL (DDG 34-11-01)\nbody\nEND OF TASK\n")
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
