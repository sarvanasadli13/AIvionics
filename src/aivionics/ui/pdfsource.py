"""Locating chapter PDFs and finding the page a task starts on.

No Qt in this module, so the part that can actually be wrong — path
resolution and the page lookup — is testable without a viewer.

The whitespace gotcha (PLAN 1.2, gotcha ③) is the reason this is not a
plain substring search. Task numbers come out of the AMM text layer with
spaces injected at arbitrary positions — `34-41-1 1-020-002` is the real
observed form of `34-41-11-020-002` — so both the query and the page text
are flattened before comparison.

Everything here fails soft: the corpus lives on an external drive, and a
missing drive is an ordinary Tuesday, not an exception the UI should show
a traceback for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import config

_WHITESPACE = re.compile(r"\s+")


def flatten(text: str) -> str:
    """Strip every whitespace character, so injected spaces stop mattering."""
    return _WHITESPACE.sub("", text)


def chapter_of(task_number: str) -> str:
    """ATA chapter for a task number — the first group, zero-padded."""
    head = task_number.strip().split("-")[0]
    return head.zfill(2)[:2]


def resolve_chapter_pdf(chapter: str, source_dir: Path | str | None = None
                        ) -> Path | None:
    """The PDF for an ATA chapter, or None if it cannot be reached.

    `source_dir` is `manual.source_file`, which for the Boeing AMM ingest is
    the chapter directory rather than a single file. Chapter PDFs are named
    with the two-digit chapter first (`34 AMM-1176.pdf`).
    """
    candidates: list[Path] = []
    if source_dir:
        path = Path(source_dir)
        if path.suffix.lower() == ".pdf":
            # A per-file manual row: use it directly if it is the right chapter.
            return path if _readable(path) else None
        candidates.append(path)
    candidates.append(config.AMM_DIR)

    chapter = str(chapter).strip().zfill(2)
    for directory in candidates:
        try:
            if not directory.is_dir():
                continue
            for pdf in sorted(directory.glob("*.pdf")):
                if pdf.name[:2] == chapter:
                    return pdf
        except OSError:
            # Drive not mounted, path too long, permission denied — all the
            # same answer to the caller.
            continue
    return None


def _readable(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


# Contents pages carry the AMM list-of-tasks column header. Measured on the
# 737-8 AMM: this marks pages 11-22 of chapter 34 and 7-16 of chapter 31,
# and the real task headings (page 38 and page 17 respectively) fall outside
# that block in both. This is a structural signal rather than a threshold —
# an earlier attempt to tell the two apart by counting TASK mentions per page
# worked on chapter 34 and failed on chapter 31, whose contents page lists
# only six.
CONTENTS_MARKERS = ("CONFPAGE", "SUBJECTCHAPTERSECTION", "CHAPTERSECTIONSUBJECT")


def is_contents_page(flat_text: str) -> bool:
    return any(marker in flat_text for marker in CONTENTS_MARKERS)


@dataclass(frozen=True)
class PageHit:
    page: int              # 0-based, as QPdfDocument counts
    exact: bool            # True when a real TASK heading was found, not a mention


class TaskPageIndex:
    """Caches task-number → page lookups for the session.

    A chapter is a few hundred pages and each lookup reads every page's text
    layer, so the same task must not be searched twice.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], PageHit | None] = {}

    def find(self, pdf_path: Path | str, task_number: str) -> PageHit | None:
        key = (str(pdf_path), flatten(task_number))
        if key not in self._cache:
            self._cache[key] = find_task_page(pdf_path, task_number)
        return self._cache[key]

    def clear(self) -> None:
        self._cache.clear()


def find_task_page(pdf_path: Path | str, task_number: str) -> PageHit | None:
    """Find the page a task *starts* on. Returns None if it is not there.

    Three tiers, because ``TASK <number>`` appears in three different roles
    and only one of them is the page the engineer wants:

      1. the **body heading** — what we are after;
      2. a **contents entry**, which is textually identical to a heading
         (PLAN 1.2, gotcha ②). Chapter 34's contents page and the real
         heading page both contain ``TASK34-00-00-040-801``, so matching the
         string alone lands the reader on the table of contents;
      3. a **cross-reference** inside some other task's text.

    Tiers 1 and 2 are told apart by `is_contents_page`. Tiers 2 and 3 are
    kept as fallbacks so a jump lands somewhere useful rather than nowhere.
    """
    target = flatten(task_number)
    if not target:
        return None
    try:
        import fitz
    except ImportError:
        return None

    contents_page: int | None = None
    mention_page: int | None = None
    try:
        with fitz.open(str(pdf_path)) as doc:
            for number, page in enumerate(doc):
                flat = flatten(page.get_text())
                if not flat or target not in flat:
                    continue
                if f"TASK{target}" not in flat:
                    if mention_page is None:
                        mention_page = number
                    continue
                if not is_contents_page(flat):
                    return PageHit(number, exact=True)
                if contents_page is None:
                    contents_page = number
    except Exception:
        # Unreadable, encrypted, missing, or not a PDF at all.
        return None

    for candidate in (contents_page, mention_page):
        if candidate is not None:
            return PageHit(candidate, exact=False)
    return None


def page_count(pdf_path: Path | str) -> int:
    try:
        import fitz
        with fitz.open(str(pdf_path)) as doc:
            return doc.page_count
    except Exception:
        return 0
