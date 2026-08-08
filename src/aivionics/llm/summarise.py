"""Extractive case summaries, and the validator that keeps them honest.

The LLM's job here is deliberately small: given a handful of prior case
narratives already retrieved from the database, say what was attempted and how
it turned out. It is optional, and everything on the screen works without it.

**Standing rule 3 — it never touches procedural text.** Only case narratives
are ever put in front of it. AMM and FIM task bodies render verbatim from the
database, outside this path entirely, because a 4B model summarising a long
procedure will eventually drop a WARNING or a CAUTION and the loss is invisible
to the reader.

**Standing rule 4 — every numeral in the UI comes from the database.** A model
writing "3 of 7 removals" is generating a statistic, and a plausible wrong
number is worse than no number. `validate` therefore rejects any digit that is
not present in the sources it was given, any task number outside the retrieved
set, and any output that invents a WARNING or CAUTION.

Rejection is not a failure mode to be smoothed over. When validation fails the
summary is dropped and the screen says so; the cases themselves are already on
screen and remain the evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .client import LLMUnavailable, OllamaClient

SYSTEM = (
    "You summarise prior aircraft maintenance case records for an engineer. "
    "Rules you must follow exactly:\n"
    "1. Use ONLY the case records given. Never add knowledge of your own.\n"
    "2. Never state a number, count, percentage or date that is not written "
    "in the records.\n"
    "3. Never invent a task number.\n"
    "4. Never write WARNING or CAUTION.\n"
    "5. Two to four sentences. Say what was attempted and how it turned out. "
    "If the records disagree, say they disagree.\n"
    "6. If the records do not support a summary, reply exactly: "
    "INSUFFICIENT EVIDENCE"
)

INSUFFICIENT = "INSUFFICIENT EVIDENCE"

TASK_NUMBER = re.compile(r"\b[A-Z]?\d{2}-\d{2}-\d{2}-\d{3}-[A-Z0-9]{3,4}\b")
NUMERAL = re.compile(r"\d+(?:[.,]\d+)?")
SAFETY_WORD = re.compile(r"\b(WARNING|CAUTION)\b", re.I)

# Small counts that are ordinary English rather than claims about the data
# ("one of the cases", "both"). Anything larger has to come from the sources.
ALLOWED_NUMERALS = {"0", "1", "2", "3", "4"}


@dataclass
class Summary:
    """The result, and — when it was refused — exactly why."""

    text: str = ""
    accepted: bool = False
    reason: str = ""
    violations: list[str] = field(default_factory=list)
    model: str = ""

    @property
    def insufficient(self) -> bool:
        return self.text.strip().upper().startswith(INSUFFICIENT)


def _digits(text: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in NUMERAL.finditer(text or "")}


def validate(output: str, *, sources: list[str],
             allowed_tasks: set[str] | None = None) -> Summary:
    """Check a generated summary against the evidence it was given.

    Every check is a rejection rather than a repair: silently editing a model's
    output would leave a sentence that reads as though it were grounded when
    part of it was removed.
    """
    text = (output or "").strip()
    result = Summary(text=text)
    if not text:
        result.reason = "the model returned nothing"
        return result
    if text.upper().startswith(INSUFFICIENT):
        result.accepted = True          # declining is a valid answer
        result.reason = "the model reported insufficient evidence"
        return result

    corpus = " ".join(sources or [])
    source_digits = _digits(corpus)

    # 1. numerals must appear in the sources (rule 4)
    for value in sorted(_digits(text) - source_digits - ALLOWED_NUMERALS):
        result.violations.append(f"numeral {value!r} is not in the case records")

    # 2. task numbers must be ones we actually retrieved (rule 3)
    allowed = {t.upper() for t in (allowed_tasks or set())}
    for match in TASK_NUMBER.finditer(text):
        number = match.group(0).upper()
        if number not in allowed and number not in corpus.upper():
            result.violations.append(f"task number {number} was not retrieved")

    # 3. safety words are never the model's to write (rule 3)
    if SAFETY_WORD.search(text) and not SAFETY_WORD.search(corpus):
        result.violations.append(
            "the summary introduces a WARNING or CAUTION that is not in the "
            "records — safety text comes from the manual, never from a model")

    if result.violations:
        result.reason = "; ".join(result.violations)
        return result
    result.accepted = True
    return result


def build_prompt(query: str, cases: list[dict]) -> tuple[str, list[str]]:
    """The prompt and the exact source strings it may be checked against."""
    sources: list[str] = []
    lines = [f"Reported symptom: {query.strip()}", "", "Case records:"]
    for i, case in enumerate(cases, start=1):
        replaced = (case.get("replaced") or "").strip()
        found = (case.get("found") or "").strip()
        tail = (case.get("tail") or "").strip()
        when = (case.get("reported_at") or "").strip()
        body = (f"Case {i} — aircraft {tail}, reported {when}. "
                f"Action taken: {replaced or 'not recorded'}. "
                f"What was found: {found or 'not recorded'}.")
        sources.append(body)
        lines.append(body)
    lines += ["", "Summarise what was attempted for this symptom and how it "
                  "turned out, following the rules exactly."]
    return "\n".join(lines), sources


def summarise_cases(client: OllamaClient | None, query: str,
                    cases: list[dict], *,
                    allowed_tasks: set[str] | None = None) -> Summary:
    """Summarise prior cases, or explain why there is no summary.

    A missing or unreachable model is an ordinary outcome, not an error: the
    application is required to work without one.
    """
    if client is None:
        return Summary(reason="no language model configured")
    if not cases:
        return Summary(reason="no prior cases to summarise")

    prompt, sources = build_prompt(query, cases)
    try:
        raw = client.generate(prompt, system=SYSTEM)
    except LLMUnavailable as exc:
        return Summary(reason=f"model unavailable — {exc}")
    except Exception as exc:                                     # noqa: BLE001
        return Summary(reason=f"model call failed — {type(exc).__name__}: {exc}")

    result = validate(raw, sources=sources, allowed_tasks=allowed_tasks)
    result.model = getattr(getattr(client, "config", None), "model", "")
    return result
