"""Per-OEM parser plugin interface (PLAN §P1-M).

Adding a second OEM = subclass + register; no core change. Gate 1 requires a
stub second-OEM parser to register without touching core code — see
tests/test_parser_registry.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParsedTask:
    task_number: str
    function_code: str
    title: str
    ata_chapter: str
    ata_section: str
    ata_subject: str
    body: str | None = None
    effectivity_raw: str | None = None
    warnings: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    catalogue_only: bool = False
    applic_refs: str | None = None


class OEMParser:
    """Base plugin. Subclasses set `oem` and implement extract_chapter()."""
    oem = "base"
    doc_standard = "pdf_legacy"

    def extract_chapter(self, path) -> tuple[list[ParsedTask], int]:
        """Return (tasks, toc_mention_count) for one chapter file."""
        raise NotImplementedError


_REGISTRY: dict[str, type[OEMParser]] = {}


def register(cls: type[OEMParser]) -> type[OEMParser]:
    _REGISTRY[cls.oem] = cls
    return cls


def get_parser(oem: str) -> OEMParser:
    return _REGISTRY[oem]()


def registered() -> list[str]:
    return sorted(_REGISTRY)
