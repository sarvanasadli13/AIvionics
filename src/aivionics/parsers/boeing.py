"""Boeing iSpec-2200-style AMM PDF parser.

Structure measured directly from the 737 MAX AMM PDFs (2026-08-08). Every
task number in a chapter appears in exactly three roles, and the three counts
agree exactly on every readable chapter:

  TOC entry      ``TASK <num>\\n<page>\\nTBC ...``   (title on the neighbouring line)
  real heading   ``TASK <num>\\n<n>.\\n<Title>``      (opens the body)
  body terminator ``END OF TASK``

The five gotchas, each unit-tested:
  1. Engine chapters carry a letter prefix (G73-...) *and* a 6th group
     (``71-00-00-800-801-G00``). Both must be spanned or the whole chapter
     silently yields zero tasks.
  2. TOC entries look identical to task headings -> discriminate on what
     FOLLOWS the number: a page number + effectivity is a TOC line, a
     paragraph number is a real heading.
  3. Whitespace gets injected inside task numbers (``34-41-1 1-020-002``) ->
     the pattern tolerates internal spaces; the canonical form is de-spaced.
  4. DDG refs are embedded in task titles -> split them out, keep both.
  5. ``SUBTASK`` contains ``TASK``. Subtasks are the numbered steps *inside* a
     task and immediately precede END OF TASK, so a naive scan pairs the END
     with the last subtask and stores a subtask number as the task number.
     Measured on chapter 34: 173 of 244 extracted rows were subtasks.
"""
from __future__ import annotations

import re

from .base import OEMParser, ParsedTask, register

# ── task number ──────────────────────────────────────────────────────────
# Raw form as it appears in extracted PDF text: whitespace may be injected
# anywhere inside a digit group (gotcha 3).
_NUM_RAW = (
    r"(?:[A-Z][ \t]*)?"                                    # engine prefix
    r"\d[ \t]*\d[ \t]*-[ \t]*"                             # chapter
    r"\d[ \t]*\d[ \t]*-[ \t]*"                             # section
    r"\d[ \t]*\d[ \t]*-[ \t]*"                             # subject
    r"\d[ \t]*\d[ \t]*\d[ \t]*-[ \t]*"                     # function code
    r"(?:[A-Z][ \t]*\d[ \t]*\d|\d[ \t]*\d[ \t]*\d[ \t]*[A-Z]?)"   # sequence
    r"(?:[ \t]*-[ \t]*[A-Z][ \t]*\d[ \t]*\d)?"             # 6th group (-G00)
)
# Canonical form, matched against a de-spaced string.
TASKNUM = re.compile(
    r"(?P<pfx>[A-Z])?"
    r"(?P<ch>\d{2})-(?P<sec>\d{2})-(?P<subj>\d{2})"
    r"-(?P<func>\d{3})-(?P<seq>[A-Z]\d{2}|\d{3}[A-Z]?)"
    r"(?:-(?P<grp6>[A-Z]\d{2}))?"
)

# gotcha 5: (?<!SUB) keeps SUBTASK out of every scan below.
_TASK_KW = r"(?<!SUB)TASK[ \t]+"
# gotcha 2: a real heading is followed by a paragraph number ("3."),
#           a TOC line by a page number then the effectivity column.
HEADING = re.compile(_TASK_KW + f"({_NUM_RAW})" + r"[ \t]*\n[ \t]*\d+\.")
# engine chapters paginate in decimals ("201.01"), avionics chapters in integers
TOC_ENTRY = re.compile(
    _TASK_KW + f"({_NUM_RAW})" + r"[ \t]*\n[ \t]*\d{1,4}(?:\.\d{1,2})?[ \t]*\n[ \t]*TBC")
ANY_NUM = re.compile(_NUM_RAW)
END = re.compile(r"END\s+OF\s+TASK", re.I)

# The DDG number may carry en/em dashes and internal spaces from the PDF
# ("DDG 34-33–01"); a class of plain hyphens only leaves digits behind.
DDG_IN_TITLE = re.compile(r"\(?\s*(DDG[\s:]*[\d\s\-‐-―().]+)\)?", re.I)
EFFECTIVITY = re.compile(r"EFFECTIVITY[^\n]*\n?((?:[^\n]*\n){0,3})", re.I)
WARNING_BLOCK = re.compile(r"WARNING\s*:?\s*((?:[^\n]+\n){1,6})", re.I)
CAUTION_BLOCK = re.compile(r"CAUTION\s*:?\s*((?:[^\n]+\n){1,6})", re.I)

# a line that ends the title block: figure ref, lettered or numbered paragraph
_TITLE_STOP = re.compile(r"^(?:\(Figure\b|\(\d+\)$|[A-Z]\.$|EFFECTIVITY\b|"
                         r"WARNING\b|CAUTION\b|GENERAL$)", re.I)

DIAGNOSTIC_FUNCS = {"2", "7", "8"}   # 2xx insp/check, 7xx test, 8xx fault isolation


def despace_tasknum(raw: str) -> str | None:
    """Collapse whitespace inside a candidate number, validate, return canon."""
    flat = re.sub(r"\s+", "", raw)
    m = TASKNUM.fullmatch(flat) or TASKNUM.match(flat)
    if not m:
        return None
    tn = (f"{m['pfx'] or ''}{m['ch']}-{m['sec']}-{m['subj']}"
          f"-{m['func']}-{m['seq']}")
    return f"{tn}-{m['grp6']}" if m["grp6"] else tn


def classify_function(func_code: str) -> str:
    return "diagnostic" if func_code[:1] in DIAGNOSTIC_FUNCS else "action"


@register
class BoeingAMMParser(OEMParser):
    oem = "boeing"
    doc_standard = "ispec2200"

    def extract_text(self, path) -> str:
        import fitz  # PyMuPDF
        doc = fitz.open(str(path))
        parts = [page.get_text("text") for page in doc]
        doc.close()
        return "\n".join(parts)

    def extract_chapter(self, path) -> tuple[list[ParsedTask], int]:
        text = self.extract_text(path)
        # Unreadable chapters must fail loudly, never yield a silent zero.
        if len(text.strip()) < 500:
            raise ValueError(
                f"no extractable text ({len(text.strip())} chars) — "
                "chapter is image-only or damaged")
        return self.parse_text(text)

    def parse_text(self, text: str) -> tuple[list[ParsedTask], int]:
        toc = {despace_tasknum(m.group(1)) for m in TOC_ENTRY.finditer(text)}
        toc.discard(None)

        heads = []
        for m in HEADING.finditer(text):
            tn = despace_tasknum(m.group(1))
            if tn:
                heads.append((m.start(), m.end(), tn))
        ends = [m.start() for m in END.finditer(text)]

        tasks: list[ParsedTask] = []
        for i, (start, body_from, tn) in enumerate(heads):
            nxt = heads[i + 1][0] if i + 1 < len(heads) else len(text)
            close = next((e for e in ends if e > start), None)
            if close is None or close > nxt:
                continue            # no terminator before the next task
            tasks.append(self._build(tn, text[body_from:close]))
        return tasks, len(toc)

    def _build(self, tn: str, body: str) -> ParsedTask:
        m = TASKNUM.match(re.sub(r"\s", "", tn))
        title_parts: list[str] = []
        for ln in body.splitlines():
            ln = ln.strip()
            if not ln:
                if title_parts:
                    break
                continue
            if _TITLE_STOP.match(ln):
                break
            title_parts.append(ln)
            if len(title_parts) >= 4:
                break
        title = " ".join(title_parts)
        ddg = DDG_IN_TITLE.search(title)
        if ddg:  # gotcha 4
            title = DDG_IN_TITLE.sub("", title).strip(" -–,")
        eff = EFFECTIVITY.search(body)
        refs = sorted({despace_tasknum(r.group(0)) for r in ANY_NUM.finditer(body)}
                      - {tn} - {None})
        return ParsedTask(
            task_number=tn,
            function_code=m["func"],
            title=title[:300],
            ata_chapter=m["ch"], ata_section=m["sec"], ata_subject=m["subj"],
            body=body,
            effectivity_raw=(eff.group(0).strip()[:500] if eff else None),
            warnings=[w.strip() for w in WARNING_BLOCK.findall(body)],
            cautions=[c.strip() for c in CAUTION_BLOCK.findall(body)],
            references=[r for r in refs if r],
        )
