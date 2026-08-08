"""PLAN 1.1 — repair the AMM chapter PDFs that no parser will open.

WHAT IS ACTUALLY WRONG (measured, not guessed)

Every one of the 16 chapter files is missing **exactly 1,048,576 bytes — one
MiB — from its front**. There is no `%PDF-` header anywhere in any of them,
and each file's `startxref` values overshoot its length by that same constant.
Whatever produced this collection dropped the first MiB block of each file.

Ten chapters survive that anyway: they store objects as plain `N 0 obj`, so
PyMuPDF's fallback scanner rebuilds an xref by walking the bytes. The other
six store their objects inside **compressed object streams** (`/ObjStm`). A
byte scanner cannot see inside a Flate-compressed stream, so it finds no page
tree, reports zero pages, and both MuPDF and qpdf give up with "unable to find
/Root dictionary".

Padding the missing MiB back is necessary but not sufficient: it realigns the
offsets (the last xref stream then lands exactly where `startxref` says), but
the `/Prev` chain still terminates at offset 116 — inside the lost block — so
the parsers abandon the whole chain.

THE REPAIR

Ignore the xref chain entirely and rebuild from the objects themselves:

  1. Scan out every top-level `N 0 obj … endobj`, keeping stream bytes verbatim
     so `/Length`, filters and image data survive untouched.
  2. Decompress every `/ObjStm` and recover the objects packed inside it, which
     is the step a byte scanner cannot do and the whole reason these six fail.
  3. Later definitions win, because these files are incremental updates and a
     later revision supersedes an earlier one.
  4. Emit a clean PDF: every object written plainly, a classic xref table, and
     a trailer whose `/Root` is taken from the newest xref stream that names
     one — falling back to a recovered `/Type/Catalog`.

Nothing is decrypted and nothing is circumvented: none of these files carry an
`/Encrypt` dictionary. This is byte-level structural recovery of a truncated
file, and it only rewrites a copy — the originals are never touched.

    python scripts/repair_amm.py            # repair the six into data/amm_repaired
    python scripts/repair_amm.py --all      # try all 16
    python scripts/repair_amm.py --verify   # report only, write nothing
"""
from __future__ import annotations

import argparse
import re
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from aivionics import config  # noqa: E402

BROKEN = ["21", "24", "26", "27", "29", "32"]
OBJ_RE = re.compile(rb"(?:^|[\r\n])(\d+)\s+(\d+)\s+obj\b")
MIB = 1_048_576


# ── low-level PDF helpers ────────────────────────────────────────────────
def find_dict_end(buf: bytes, start: int) -> int:
    """Index just past the `>>` closing the dictionary opening at `start`.

    Counts nesting and skips over literal strings, so a `>>` inside `(...)`
    does not close the dictionary early.
    """
    depth = 0
    i = start
    n = len(buf)
    while i < n - 1:
        c = buf[i:i + 2]
        if c == b"<<":
            depth += 1
            i += 2
            continue
        if c == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        if buf[i:i + 1] == b"(":                      # literal string
            esc = False
            par = 1
            i += 1
            while i < n and par:
                ch = buf[i:i + 1]
                if esc:
                    esc = False
                elif ch == b"\\":
                    esc = True
                elif ch == b"(":
                    par += 1
                elif ch == b")":
                    par -= 1
                i += 1
            continue
        i += 1
    return -1


def apply_png_predictor(data: bytes, colors: int, bpc: int, columns: int) -> bytes:
    """Undo a PNG row predictor (only ever needed for xref streams here)."""
    bpp = max(1, (colors * bpc) // 8)
    row_len = (columns * colors * bpc + 7) // 8
    out = bytearray()
    prev = bytearray(row_len)
    i = 0
    while i + 1 + row_len <= len(data):
        ft = data[i]
        row = bytearray(data[i + 1:i + 1 + row_len])
        if ft == 1:
            for j in range(bpp, row_len):
                row[j] = (row[j] + row[j - bpp]) & 0xFF
        elif ft == 2:
            for j in range(row_len):
                row[j] = (row[j] + prev[j]) & 0xFF
        elif ft == 3:
            for j in range(row_len):
                left = row[j - bpp] if j >= bpp else 0
                row[j] = (row[j] + ((left + prev[j]) >> 1)) & 0xFF
        elif ft == 4:
            for j in range(row_len):
                a = row[j - bpp] if j >= bpp else 0
                b = prev[j]
                c = prev[j - bpp] if j >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[j] = (row[j] + pr) & 0xFF
        out += row
        prev = row
        i += 1 + row_len
    return bytes(out)


def decode_stream(dict_bytes: bytes, raw: bytes) -> bytes | None:
    """Flate-decode a stream, undoing a PNG predictor when one is declared."""
    if b"/FlateDecode" not in dict_bytes:
        return raw
    try:
        data = zlib.decompress(raw)
    except zlib.error:
        try:                                   # truncated tail — salvage what we can
            d = zlib.decompressobj()
            data = d.decompress(raw)
        except zlib.error:
            return None
    m = re.search(rb"/Predictor\s+(\d+)", dict_bytes)
    if m and int(m.group(1)) >= 10:
        cols = int((re.search(rb"/Columns\s+(\d+)", dict_bytes) or [b"", b"1"])[1])
        colors = int((re.search(rb"/Colors\s+(\d+)", dict_bytes) or [b"", b"1"])[1])
        bpc = int((re.search(rb"/BitsPerComponent\s+(\d+)", dict_bytes) or [b"", b"8"])[1])
        data = apply_png_predictor(data, colors, bpc, cols)
    return data


# ── recovery ─────────────────────────────────────────────────────────────
@dataclass
class Recovered:
    objects: dict[int, bytes]        # object number -> body bytes (no "N 0 obj")
    root: int | None
    from_objstm: int
    top_level: int
    objstm_count: int
    synthesised_catalog: bool = False


def scan_objects(buf: bytes) -> tuple[dict[int, tuple[int, bytes]], list[int]]:
    """Every top-level object, latest definition winning. Streams kept verbatim."""
    objs: dict[int, tuple[int, bytes]] = {}
    objstm_positions: list[int] = []
    for m in OBJ_RE.finditer(buf):
        num = int(m.group(1))
        body_start = m.end()
        end = buf.find(b"endobj", body_start)
        if end < 0:
            continue
        body = buf[body_start:end]
        prev = objs.get(num)
        if prev is None or m.start() > prev[0]:      # later revision supersedes
            objs[num] = (m.start(), body)
        if b"/ObjStm" in body[:400]:
            objstm_positions.append(m.start())
    return objs, objstm_positions


def expand_objstm(buf: bytes, body: bytes) -> dict[int, bytes]:
    """Recover the objects packed inside one /ObjStm container."""
    dstart = body.find(b"<<")
    if dstart < 0:
        return {}
    dend = find_dict_end(body, dstart)
    if dend < 0:
        return {}
    d = body[dstart:dend]
    sm = re.search(rb"stream\r?\n?", body[dend:])
    if not sm:
        return {}
    raw_start = dend + sm.end()
    raw_end = body.find(b"endstream", raw_start)
    data = decode_stream(d, body[raw_start:raw_end if raw_end > 0 else len(body)])
    if not data:
        return {}
    n = re.search(rb"/N\s+(\d+)", d)
    first = re.search(rb"/First\s+(\d+)", d)
    if not (n and first):
        return {}
    n, first = int(n.group(1)), int(first.group(1))
    header = data[:first].split()
    out: dict[int, bytes] = {}
    for i in range(n):
        try:
            num = int(header[2 * i])
            off = int(header[2 * i + 1])
        except (IndexError, ValueError):
            break
        nxt = int(header[2 * i + 3]) if 2 * i + 3 < len(header) else len(data) - first
        out[num] = data[first + off: first + nxt].strip()
    return out


def newest_root(buf: bytes) -> int | None:
    """`/Root` from the last xref stream that declares one."""
    best = None
    for m in re.finditer(rb"/Root\s+(\d+)\s+\d+\s+R", buf):
        best = int(m.group(1))
    return best


def find_page_tree_root(objects: dict[int, bytes]) -> int | None:
    """The `/Type/Pages` node that nothing else lists as a kid.

    Needed because the catalog is one of the objects that did not survive in
    several chapters — it names `/Root 5724` and object 5724 comes back empty.
    The page tree itself is intact, so the tree root is recoverable directly:
    it is the only `/Pages` node that is not some other node's child.
    """
    pages_nodes = {n for n, b in objects.items()
                   if re.search(rb"/Type\s*/Pages\b", b)}
    if not pages_nodes:
        return None
    children: set[int] = set()
    for n in pages_nodes:
        kids = re.search(rb"/Kids\s*\[(.*?)\]", objects[n], re.S)
        if kids:
            children.update(int(x) for x in
                            re.findall(rb"(\d+)\s+\d+\s+R", kids.group(1)))
    roots = pages_nodes - children
    if not roots:
        return None

    def count_of(n: int) -> int:
        m = re.search(rb"/Count\s+(\d+)", objects[n])
        return int(m.group(1)) if m else 0

    return max(roots, key=count_of)


INHERITABLE = (b"/Resources", b"/MediaBox", b"/CropBox", b"/Rotate")


def _attr(body: bytes, key: bytes) -> bytes | None:
    """The raw value of one key: a dict, an array, a reference or a number."""
    m = re.search(re.escape(key) + rb"\s*", body)
    if not m:
        return None
    i = m.end()
    if body[i:i + 2] == b"<<":
        j = find_dict_end(body, i)
        return body[i:j] if j > 0 else None
    if body[i:i + 1] == b"[":
        depth, j = 0, i
        while j < len(body):
            if body[j:j + 1] == b"[":
                depth += 1
            elif body[j:j + 1] == b"]":
                depth -= 1
                if depth == 0:
                    return body[i:j + 1]
            j += 1
        return None
    m2 = re.match(rb"(\d+\s+\d+\s+R|[-\d.]+|/\w+)", body[i:])
    return m2.group(1) if m2 else None


def flatten_page_tree(objects: dict[int, bytes], tree_root: int) -> list[int]:
    """Walk the page tree and return the leaf pages, in document order.

    Kids that did not survive the truncation are dropped rather than left
    dangling — MuPDF rejects the whole tree with "non-page object in page
    tree" for a single bad reference, which would cost an entire chapter.
    Attributes a page inherits from its ancestors are pushed down on the way,
    because the intermediate nodes do not survive flattening and a page
    without /MediaBox or /Resources renders blank.
    """
    pages: list[int] = []
    seen: set[int] = set()

    def walk(num: int, inherited: dict[bytes, bytes]) -> None:
        if num in seen or num not in objects:
            return
        seen.add(num)
        body = objects[num]
        here = dict(inherited)
        for key in INHERITABLE:
            val = _attr(body, key)
            if val is not None:
                here[key] = val

        if re.search(rb"/Type\s*/Pages\b", body):
            kids = re.search(rb"/Kids\s*\[(.*?)\]", body, re.S)
            if kids:
                for ref in re.findall(rb"(\d+)\s+\d+\s+R", kids.group(1)):
                    walk(int(ref), here)
            return

        if re.search(rb"/Type\s*/Page\b", body):
            missing = b"".join(k + b" " + v for k, v in here.items()
                               if _attr(body, k) is None)
            if missing:
                objects[num] = body.rstrip()[:-2] + missing + b">>"
            pages.append(num)

    walk(tree_root, {})
    return pages


def rebuild_page_tree(objects: dict[int, bytes], tree_root: int) -> tuple[int, int]:
    """Replace the tree with one valid /Pages node over the surviving pages."""
    pages = flatten_page_tree(objects, tree_root)
    if not pages:
        return tree_root, 0
    kids = b"".join(b"%d 0 R " % n for n in pages).strip()
    objects[tree_root] = (b"<</Type/Pages/Count %d/Kids[%s]>>"
                          % (len(pages), kids))
    for num in pages:                       # every leaf must point at the root
        body = objects[num]
        if _attr(body, b"/Parent") is None:
            objects[num] = body.rstrip()[:-2] + b"/Parent %d 0 R>>" % tree_root
        else:
            objects[num] = re.sub(rb"/Parent\s+\d+\s+\d+\s+R",
                                  b"/Parent %d 0 R" % tree_root, body)
    return tree_root, len(pages)


def recover(path: Path) -> Recovered:
    buf = path.read_bytes()
    objs, objstm_pos = scan_objects(buf)
    merged: dict[int, bytes] = {num: body for num, (_, body) in objs.items()}

    from_stm = 0
    stm_count = 0
    # oldest container first, so a later one supersedes it
    for pos in sorted(objstm_pos):
        m = OBJ_RE.match(buf, pos) or OBJ_RE.search(buf, pos)
        if not m:
            continue
        end = buf.find(b"endobj", m.end())
        inner = expand_objstm(buf, buf[m.end():end])
        stm_count += 1
        for num, body in inner.items():
            merged[num] = body
            from_stm += 1

    merged = {n: b for n, b in merged.items() if b.strip()}

    root = newest_root(buf)
    if root is None or root not in merged:
        root = next((n for n, b in merged.items()
                     if b"/Type" in b and b"/Catalog" in b), None)

    # The catalog is itself one of the casualties in several chapters: the xref
    # names /Root 5724 and object 5724 comes back empty. The page tree is
    # intact either way, so synthesise a catalog over its real root rather than
    # discard a whole chapter for one missing dictionary.
    synthesised = False
    tree = None
    if root is not None and root in merged:
        m = re.search(rb"/Pages\s+(\d+)\s+\d+\s+R", merged[root])
        tree = int(m.group(1)) if m else None
    if tree is None or tree not in merged:
        tree = find_page_tree_root(merged)
        if tree is not None:
            root = (max(merged) + 1) if merged else 1
            merged[root] = b"<</Type/Catalog/Pages %d 0 R>>" % tree
            synthesised = True

    if tree is not None:
        rebuild_page_tree(merged, tree)

    rec = Recovered(merged, root, from_stm, len(objs), stm_count)
    rec.synthesised_catalog = synthesised
    return rec


def write_pdf(rec: Recovered, out: Path) -> None:
    """Emit every recovered object plainly, with a classic xref and trailer."""
    parts = [b"%PDF-1.6\n%\xe2\xe3\xcf\xd3\n"]
    offsets: dict[int, int] = {}
    pos = len(parts[0])
    for num in sorted(rec.objects):
        body = rec.objects[num].strip(b"\r\n")
        chunk = b"%d 0 obj\n" % num + body + b"\nendobj\n"
        offsets[num] = pos
        parts.append(chunk)
        pos += len(chunk)

    size = (max(offsets) + 1) if offsets else 1
    xref_pos = pos
    xref = [b"xref\n0 %d\n" % size, b"0000000000 65535 f \n"]
    for num in range(1, size):
        if num in offsets:
            xref.append(b"%010d 00000 n \n" % offsets[num])
        else:
            xref.append(b"0000000000 65535 f \n")
    parts.extend(xref)
    trailer = (b"trailer\n<</Size %d/Root %d 0 R>>\nstartxref\n%d\n%%%%EOF\n"
               % (size, rec.root or 1, xref_pos))
    parts.append(trailer)
    out.write_bytes(b"".join(parts))


def verify(path: Path) -> tuple[int, int, str]:
    """(pages, sampled chars, note) via PyMuPDF."""
    import fitz
    try:
        doc = fitz.open(str(path))
    except Exception as exc:                                     # noqa: BLE001
        return 0, 0, f"{type(exc).__name__}: {exc}"[:80]
    n = doc.page_count
    idx = sorted({0, n // 4, n // 2, min(n - 1, 3 * n // 4)}) if n else []
    chars = sum(len(doc[i].get_text("text").strip()) for i in idx if i < n)
    doc.close()
    return n, chars, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true", help="attempt all 16 chapters")
    ap.add_argument("--verify", action="store_true", help="report only, write nothing")
    ap.add_argument("--out", default=str(config.DATA_DIR / "amm_repaired"))
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(config.AMM_DIR.glob("*.pdf"))
    if not args.all:
        pdfs = [p for p in pdfs if p.name[:2] in BROKEN]
    if not pdfs:
        print(f"no chapter PDFs under {config.AMM_DIR}")
        return 1

    print(f"{'chapter':<9}{'objects':>9}{'in ObjStm':>11}{'root':>7}"
          f"{'pages':>8}{'chars':>9}  note")
    print("-" * 74)
    ok = 0
    for p in pdfs:
        ch = p.name[:2]
        rec = recover(p)
        note = ""
        pages = chars = 0
        if rec.root is None:
            note = "no /Root recovered"
        elif not args.verify:
            dest = out_dir / f"{ch}.pdf"
            write_pdf(rec, dest)
            pages, chars, note = verify(dest)
            if rec.synthesised_catalog:
                note = (note + " · catalog synthesised").strip(" ·")
            if pages:
                ok += 1
        print(f"{ch:<9}{len(rec.objects):>9}{rec.from_objstm:>11}"
              f"{(rec.root or 0):>7}{pages:>8}{chars:>9}  {note}")
    print(f"\n{ok}/{len(pdfs)} chapters now open with a page tree")
    if not args.verify:
        print(f"written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
