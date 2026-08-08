"""PDF structural recovery (scripts/repair_amm.py).

These are the parts that decide whether a chapter comes back at all, so they
are tested on synthetic structures rather than on the corpus: the object-stream
expansion (the step a byte scanner cannot do, and the whole reason six chapters
failed), and the page-tree rebuild that drops references the truncation killed.
"""
import importlib.util
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "repair_amm", ROOT / "scripts" / "repair_amm.py")
repair = importlib.util.module_from_spec(_spec)
# Registered before execution because @dataclass resolves annotations through
# sys.modules[cls.__module__], which is None for a module that is still loading.
sys.modules["repair_amm"] = repair
_spec.loader.exec_module(repair)


# ── dictionary scanning ──────────────────────────────────────────────────
def test_find_dict_end_handles_nesting():
    b = b"<</A<</B 1>>/C 2>>tail"
    assert b[:repair.find_dict_end(b, 0)] == b"<</A<</B 1>>/C 2>>"


def test_find_dict_end_ignores_angle_brackets_inside_a_string():
    """A literal string containing '>>' must not close the dictionary."""
    b = rb"<</T (caution >> do not)/X 1>>rest"
    end = repair.find_dict_end(b, 0)
    assert b[:end].endswith(b"/X 1>>")
    assert b[end:] == b"rest"


def test_find_dict_end_reports_failure_on_an_unterminated_dict():
    assert repair.find_dict_end(b"<</A 1", 0) == -1


# ── object streams ───────────────────────────────────────────────────────
def _objstm(objects: list[tuple[int, bytes]]) -> bytes:
    """Build a /ObjStm container body the way a real PDF writer would."""
    header, payload = [], b""
    for num, body in objects:
        header.append(b"%d %d" % (num, len(payload)))
        payload += body + b" "
    head = b" ".join(header) + b" "
    data = head + payload
    stream = zlib.compress(data)
    return (b"<</Type/ObjStm/N %d/First %d/Filter/FlateDecode/Length %d>>\n"
            b"stream\n" % (len(objects), len(head), len(stream))
            ) + stream + b"\nendstream"


def test_expand_objstm_recovers_packed_objects():
    body = _objstm([(7, b"<</Type/Page/MediaBox[0 0 612 792]>>"),
                    (9, b"<</Type/Pages/Count 1/Kids[7 0 R]>>")])
    out = repair.expand_objstm(b"", body)
    assert set(out) == {7, 9}
    assert b"/Type/Page" in out[7]
    assert b"/Kids[7 0 R]" in out[9]


def test_expand_objstm_survives_a_corrupt_stream():
    body = b"<</Type/ObjStm/N 1/First 4/Filter/FlateDecode>>\nstream\nnotflate\nendstream"
    assert repair.expand_objstm(b"", body) == {}


# ── page tree ────────────────────────────────────────────────────────────
def test_find_page_tree_root_picks_the_node_nothing_points_at():
    objects = {
        1: b"<</Type/Pages/Count 2/Kids[2 0 R 3 0 R]>>",
        2: b"<</Type/Pages/Count 1/Kids[4 0 R]>>",
        3: b"<</Type/Pages/Count 1/Kids[5 0 R]>>",
        4: b"<</Type/Page>>",
        5: b"<</Type/Page>>",
    }
    assert repair.find_page_tree_root(objects) == 1


def test_flatten_drops_kids_that_did_not_survive():
    """One dangling reference makes MuPDF reject the entire tree, which would
    cost a whole chapter — so a missing kid is dropped, not kept."""
    objects = {
        1: b"<</Type/Pages/Count 3/Kids[2 0 R 99 0 R 3 0 R]>>",
        2: b"<</Type/Page/MediaBox[0 0 612 792]>>",
        3: b"<</Type/Page/MediaBox[0 0 612 792]>>",
    }
    assert repair.flatten_page_tree(objects, 1) == [2, 3]


def test_flatten_pushes_inherited_attributes_onto_the_leaves():
    """/MediaBox and /Resources are inherited from ancestors that do not
    survive flattening; a page without them renders blank."""
    objects = {
        1: b"<</Type/Pages/MediaBox[0 0 595 842]/Resources<</Font 9 0 R>>"
           b"/Count 1/Kids[2 0 R]>>",
        2: b"<</Type/Page>>",
    }
    repair.flatten_page_tree(objects, 1)
    nospace = lambda b: b.replace(b" ", b"")            # noqa: E731
    flat = nospace(objects[2])
    assert nospace(b"/MediaBox[0 0 595 842]") in flat
    assert nospace(b"/Resources<</Font 9 0 R>>") in flat


def test_flatten_does_not_override_a_page_that_sets_its_own():
    objects = {
        1: b"<</Type/Pages/MediaBox[0 0 595 842]/Count 1/Kids[2 0 R]>>",
        2: b"<</Type/Page/MediaBox[0 0 612 792]>>",
    }
    repair.flatten_page_tree(objects, 1)
    assert objects[2].count(b"/MediaBox") == 1
    assert b"[0 0 612 792]" in objects[2]


def test_flatten_terminates_on_a_cyclic_tree():
    objects = {
        1: b"<</Type/Pages/Count 1/Kids[2 0 R]>>",
        2: b"<</Type/Pages/Count 1/Kids[1 0 R 3 0 R]>>",
        3: b"<</Type/Page>>",
    }
    assert repair.flatten_page_tree(objects, 1) == [3]


def test_rebuild_page_tree_produces_one_valid_node():
    objects = {
        1: b"<</Type/Pages/Count 2/Kids[2 0 R 3 0 R]>>",
        2: b"<</Type/Page/MediaBox[0 0 612 792]>>",
        3: b"<</Type/Page/MediaBox[0 0 612 792]>>",
    }
    root, n = repair.rebuild_page_tree(objects, 1)
    assert (root, n) == (1, 2)
    assert objects[1] == b"<</Type/Pages/Count 2/Kids[2 0 R 3 0 R]>>"
    for page in (2, 3):
        assert b"/Parent 1 0 R" in objects[page]


# ── predictor ────────────────────────────────────────────────────────────
def test_png_up_predictor_reverses_row_differencing():
    # filter type 2 (Up): each row is the difference from the row above
    raw = bytes([2, 1, 2, 3]) + bytes([2, 1, 1, 1])
    out = repair.apply_png_predictor(raw, colors=1, bpc=8, columns=3)
    assert out == bytes([1, 2, 3]) + bytes([2, 3, 4])


def test_writer_emits_a_header_xref_and_trailer(tmp_path):
    rec = repair.Recovered(
        objects={1: b"<</Type/Catalog/Pages 2 0 R>>",
                 2: b"<</Type/Pages/Count 1/Kids[3 0 R]>>",
                 3: b"<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>"},
        root=1, from_objstm=0, top_level=3, objstm_count=0)
    out = tmp_path / "r.pdf"
    repair.write_pdf(rec, out)
    data = out.read_bytes()
    assert data.startswith(b"%PDF-")
    assert b"\ntrailer\n" in data and b"/Root 1 0 R" in data
    assert data.rstrip().endswith(b"%%EOF")

    import fitz
    doc = fitz.open(str(out))
    assert doc.page_count == 1
    doc.close()
