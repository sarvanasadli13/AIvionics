"""Boeing iSpec-2200-style AMM PDF parser.

The four gotchas found the hard way (2026-08-06 session), each unit-tested:
  1. Engine chapters carry a 6th group / letter prefix (G73-..., ...-A73).
  2. TOC entries look identical to task headings -> a heading is only real if
     the next boundary marker after it is END OF TASK, not another heading.
  3. Whitespace gets injected inside task numbers ("34-41-1 1-020-002") ->
     match on a de-spaced window, keep original span for provenance.
  4. DDG refs are embedded in task titles -> split them out, keep both.
"""
from __future__ import annotations

import re

from .base import OEMParser, ParsedTask, register

# task number on a de-spaced string: optional engine letter prefix,
# 2-2-2 digit groups, 3-digit function code, 3-char seq (may carry a letter)
TASKNUM = re.compile(
    r"(?P<pfx>[A-Z])?"
    r"(?P<ch>\d{2})-(?P<sec>\d{2})-(?P<subj>\d{2})"
    r"-(?P<func>\d{3})-(?P<seq>[A-Z]\d{2}|\d{3}[A-Z]?)"
)
# heading: 'TASK' then a window that de-spaces into a task number
HEADING = re.compile(r"TASK[ \t]+([A-Z]?\d[\d \t-]{10,24}\d[A-Z]?)")
END = re.compile(r"END\s+OF\s+TASK", re.I)
DDG_IN_TITLE = re.compile(r"\(?\s*(DDG[\s:]*[\d\-().]+)\s*\)?", re.I)
EFFECTIVITY = re.compile(
    r"EFFECTIVITY[^\n]*\n?((?:[^\n]*\n){0,3})", re.I)
WARNING_BLOCK = re.compile(r"WARNING\s*:?\s*((?:[^\n]+\n){1,6})", re.I)
CAUTION_BLOCK = re.compile(r"CAUTION\s*:?\s*((?:[^\n]+\n){1,6})", re.I)

DIAGNOSTIC_FUNCS = {"2", "7", "8"}   # 2xx insp/check, 7xx test, 8xx fault isolation


def despace_tasknum(raw: str) -> str | None:
    """Collapse whitespace inside a candidate number, validate, return canon."""
    flat = re.sub(r"[\s ]+", "", raw)
    m = TASKNUM.fullmatch(flat) or TASKNUM.match(flat)
    if not m:
        return None
    return (f"{m['pfx'] or ''}{m['ch']}-{m['sec']}-{m['subj']}"
            f"-{m['func']}-{m['seq']}")


def classify_function(func_code: str) -> str:
    return "diagnostic" if func_code[:1] in DIAGNOSTIC_FUNCS else "action"


@register
class BoeingAMMParser(OEMParser):
    oem = "boeing"
    doc_standard = "ispec2200"

    def extract_text(self, path) -> str:
        import fitz  # PyMuPDF
        doc = fitz.open(str(path))
        parts = []
        for page in doc:
            parts.append(page.get_text("text"))
        doc.close()
        return "\n".join(parts)

    def extract_chapter(self, path) -> tuple[list[ParsedTask], int]:
        text = self.extract_text(path)
        return self.parse_text(text)

    def parse_text(self, text: str) -> tuple[list[ParsedTask], int]:
        # collect ordered markers: (pos, 'head', tasknum, headline_end) / (pos,'end')
        markers: list[tuple[int, str, str | None, int]] = []
        for m in HEADING.finditer(text):
            tn = despace_tasknum(m.group(1))
            if tn:
                markers.append((m.start(), "head", tn, m.end()))
        for m in END.finditer(text):
            markers.append((m.start(), "end", None, m.end()))
        markers.sort(key=lambda x: x[0])

        all_mentions = {m[2] for m in markers if m[1] == "head"}
        tasks: list[ParsedTask] = []
        i = 0
        while i < len(markers):
            pos, kind, tn, hend = markers[i]
            if kind != "head":
                i += 1
                continue
            # gotcha 2: real only if next marker is END OF TASK
            if i + 1 < len(markers) and markers[i + 1][1] == "end":
                body = text[hend:markers[i + 1][0]]
                tasks.append(self._build(tn, body))
                i += 2
            else:
                i += 1  # TOC line or duplicate heading
        return tasks, len(all_mentions)

    def _build(self, tn: str, body: str) -> ParsedTask:
        m = TASKNUM.match(re.sub(r"\s", "", tn))
        # title: first non-empty line of the body
        title = ""
        for ln in body.splitlines():
            ln = ln.strip()
            if ln:
                title = ln
                break
        ddg = DDG_IN_TITLE.search(title)
        if ddg:  # gotcha 4
            title = DDG_IN_TITLE.sub("", title).strip(" -–")
        eff = EFFECTIVITY.search(body)
        warnings = [w.strip() for w in WARNING_BLOCK.findall(body)]
        cautions = [c.strip() for c in CAUTION_BLOCK.findall(body)]
        refs = sorted({
            despace_tasknum(r.group(0)) for r in TASKNUM.finditer(body)
        } - {tn} - {None})
        return ParsedTask(
            task_number=tn,
            function_code=m["func"],
            title=title[:300],
            ata_chapter=m["ch"], ata_section=m["sec"], ata_subject=m["subj"],
            body=body,
            effectivity_raw=(eff.group(0).strip()[:500] if eff else None),
            warnings=warnings, cautions=cautions,
            references=[r for r in refs if r],
        )
