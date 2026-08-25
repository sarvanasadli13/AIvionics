"""Bridge from the Diagnose screen to the model, the tools and the gate.

Phase 3. Phase 1 gave the application a model behind an interface, Phase 2 gave
it a shape for what a model may hand back and a grounding check that refuses
anything else. This file is what stands between those two and a screen an
engineer reads, and it exists to enforce four things that are easy to lose the
moment a panel is drawn:

* **Retrieval owns the candidate set.** The model is never asked "which task
  applies"; it is handed the tasks retrieval found and asked to reason over
  *those*. This is not a stylistic preference. Asked where a pitot probe heater
  fault-isolation task lives, Nemotron answered ATA 31; the corpus puts it at
  30-31-00-810-801, in ATA 30. A model that is allowed to supply its own task
  numbers will eventually supply that one, and it looks exactly like a correct
  answer. `ALLOWED TASK NUMBERS` in the prompt and `check_grounding` on the way
  back are the same rule enforced twice, because one of them is a request and
  the other is a gate.
* **A rejected answer is not a repaired answer.** When grounding fails the
  `Investigation` carries the violations and `answer` stays `None` — there is
  no field a panel could reach into and render by accident. That is a
  structural guarantee rather than a discipline the UI has to remember.
* **Model-unavailable is an ordinary state.** It is a value of `state`, not an
  exception, and nothing on this path can reach the deterministic search in
  `searchservice.py`. The ranked-locator screen is complete without a model and
  stays that way.
* **Nothing here runs on the UI thread.** `submit` follows `searchservice`'s
  `QRunnable` + signals pattern exactly, and every stage checks the
  `CancelToken` so a cancelled investigation stops between tool calls rather
  than after the model has finished thinking.

The budget is deliberately generous. Nemotron 3.5 Lightning emits a chain of
thought before every answer — `/no_think` does not suppress it — and two
structured-extraction probes ran out of tokens inside the thinking block and
returned no JSON at all. `budget_for` is the measured allowance, not a guess.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from .. import config
from ..llm.schemas import (EVIDENCE_LEVELS, UNRESOLVED_NOTICE, AssistantAnswer,
                           GroundingReport, SchemaError, Violation,
                           check_grounding, parse_answer_json)
from ..llm.service import CancelToken, Cancelled, ModelIdentity, budget_for
from ..llm.tools import ToolError, ToolRegistry, ToolResult

# ── the states an investigation can end in ──────────────────────────────
#
# Every one of these is rendered. None of them is an error swallowed on the
# way to a blank panel: an engineer who asks a question and gets nothing back
# learns only that the tool is unreliable.

IDLE = "idle"
RUNNING = "running"
UNAVAILABLE = "model_unavailable"      # the model is down — search is not
NO_EVIDENCE = "no_evidence"            # retrieval returned nothing to reason over
MALFORMED = "malformed"                # the reply was not a valid answer object
REJECTED = "rejected"                  # the answer failed grounding
ACCEPTED = "accepted"                  # the answer passed every check
CANCELLED = "cancelled"
FAILED = "failed"                      # something unexpected, named in full

# How many tokens the answer itself is expected to need, before the reasoning
# multiplier is applied. Sized for a handful of hypotheses with their evidence
# lists, not for prose.
ANSWER_TOKENS = 800

# Bounds on what goes into the prompt. A larger candidate set does not produce
# a better answer; it produces a longer one with more places to be wrong.
MAX_TASKS = 6
MAX_CASES = 8
SNIPPET = 240


# ── what the model is told ──────────────────────────────────────────────

SYSTEM = (
    "You are a maintenance decision-support assistant for a licensed aircraft "
    "engineer. You reason over evidence that has already been retrieved for "
    "you from a controlled database. You never contribute knowledge of your "
    "own.\n"
    "\n"
    "Rules you must follow exactly:\n"
    "1. Use ONLY the evidence in the EVIDENCE block. If it does not answer "
    "the question, say so — do not fill the gap.\n"
    "2. Every task_number you write must be copied character for character "
    "from ALLOWED TASK NUMBERS. A task number that is not in that list is a "
    "fabrication even if it looks correct, and it will be rejected.\n"
    "3. Every case id you cite must come from ALLOWED CASE IDS.\n"
    "4. evidence_level is one of: " + ", ".join(EVIDENCE_LEVELS) + ". It is a "
    "category. Never write a percentage, a probability, a confidence score or "
    "any number in its place.\n"
    "5. Never state a numeral that does not appear in the evidence.\n"
    "6. These are hypotheses, not causes. The case base records what an "
    "engineer DID, never whether it was correct, and it contains no confirmed "
    "outcomes. Never describe a hypothesis as a confirmed, proven or "
    "identified cause.\n"
    "7. recommended_pages must be an empty list. No page index exists, so no "
    "page may be cited.\n"
    "8. If any recommended document has an effectivity_result other than "
    f"'applicable', include this exact sentence in limitations: "
    f"\"{UNRESOLVED_NOTICE}\".\n"
    "9. If the evidence supports no hypothesis, set abstained to true and give "
    "an abstention_reason. An abstention is a correct answer.\n"
    "10. Reply with ONE JSON object and nothing else after it."
)

# The shape, spelled out. A model that has to infer the schema from an example
# will infer a plausible variant of it, and a plausible variant is a rejection.
SCHEMA_NOTE = """The JSON object has exactly these fields:
{
  "interpreted_complaint": {
    "normalized_symptom": str,
    "ata_candidates": [str], "system_candidates": [str], "fault_codes": [str],
    "phase_of_flight": str, "environmental": [str],
    "missing_information": [str],
    "evidence_level": one of the evidence levels
  },
  "hypotheses": [{
    "cause_id": str, "cause": str,
    "rank": int starting at 1, consecutive, no duplicates,
    "evidence_level": one of the evidence levels,
    "supporting_case_ids": [int] (required, never empty),
    "contradicting_case_ids": [int],
    "limitations": [str]
  }],
  "recommended_documents": [{
    "manual_id": int, "manual_type": str, "task_number": str, "title": str,
    "revision": str, "effectivity_result": str,
    "retrieval_evidence": {...} (copy it from the candidate)
  }],
  "recommended_pages": [],
  "supporting_evidence": [str], "contradicting_evidence": [str],
  "missing_information": [str], "limitations": [str],
  "abstained": bool, "abstention_reason": str,
  "model_version": str, "index_version": str,
  "evidence_ids": [str] (copy from the evidence_id fields you used),
  "narrative": str
}
Every field shown as [str] or [int] MUST be a JSON array, even when it has
only one item. Use [] when it is empty. Never replace an array with bare text
or a bare number; for example, limitations is [] or ["text"], never "text".
"""


# ── provenance ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Provenance:
    """Who answered, from where, over which index.

    Carried separately from the answer's own `model_version` / `index_version`
    on purpose: those are what the *model wrote*, and the two disagreeing is
    itself a finding. `_provenance_violations` compares them.
    """

    requested_model: str = ""
    served_model: str = ""
    provider: str = ""
    endpoint: str = ""
    index_version: str = ""
    tools_called: tuple[str, ...] = ()

    @property
    def model_mismatch(self) -> bool:
        """True when the endpoint answered from a model nobody asked for.

        A hosted catalogue will list a model it refuses to serve, and a client
        that assumes the requested name is the answering name will attribute
        one model's output to another for as long as nobody checks.
        """
        if not self.requested_model or not self.served_model:
            return False
        def core(name: str) -> str:
            return (name or "").strip().lower().split("/")[-1].split(":")[0]
        return core(self.requested_model) != core(self.served_model)

    def describe(self) -> str:
        model = self.served_model or self.requested_model or "no model"
        if self.model_mismatch:
            model = (f"requested {self.requested_model}, endpoint served "
                     f"{self.served_model}")
        parts = [f"model {model}"]
        if self.provider:
            parts.append(self.provider)
        if self.endpoint:
            parts.append(self.endpoint)
        parts.append(f"index {self.index_version or 'unknown'}")
        return " · ".join(parts)


@dataclass(frozen=True)
class AIStatus:
    """Whether the model can be asked anything, and what it actually is."""

    available: bool
    reason: str = ""
    identity: ModelIdentity = field(default_factory=ModelIdentity)
    endpoint: str = ""
    provider: str = ""
    index_version: str = ""

    @property
    def mismatch(self) -> bool:
        return bool(self.identity.served and not self.identity.matches_request)

    def describe(self) -> str:
        if not self.available:
            return f"model unavailable — {self.reason or 'no reason given'}"
        return self.identity.describe()


# ── the result the panel renders ────────────────────────────────────────

@dataclass(frozen=True)
class Investigation:
    """One complete attempt, in whichever way it ended.

    `answer` is populated in exactly one state: `ACCEPTED`. A rejected answer
    is not carried here in any field — not truncated, not annotated, not
    hidden behind a flag — because a field that holds it is a field a future
    panel will render. The violations are what the engineer is shown instead,
    and they are the whole report rather than the first failure: an answer that
    cites an invented task *and* states an unmeasured number has two problems,
    and fixing the one that happened to be checked first leaves the other.
    """

    state: str
    complaint: str = ""
    tail: str = ""
    answer: AssistantAnswer | None = None
    report: GroundingReport = field(default_factory=GroundingReport)
    tool_results: tuple[ToolResult, ...] = ()
    provenance: Provenance = field(default_factory=Provenance)
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.state == ACCEPTED and self.answer is not None

    @property
    def rejected(self) -> bool:
        return self.state == REJECTED

    @property
    def violations(self) -> tuple[Violation, ...]:
        return self.report.violations

    @property
    def available_tools(self) -> tuple[ToolResult, ...]:
        return tuple(r for r in self.tool_results if r.ok)

    @property
    def unavailable_tools(self) -> tuple[ToolResult, ...]:
        """The tools that could not answer, each naming what it is missing.

        Rendered rather than dropped. Seven of the fourteen declared tools have
        no data behind them, and a panel that quietly omits them tells the
        engineer the investigation was more complete than it was.
        """
        return tuple(r for r in self.tool_results if not r.ok)

    def cases(self) -> dict[int, dict]:
        """defect_id → case row, merged across every tool that returned cases.

        The hypothesis expanders resolve their `supporting_case_ids` through
        this, so a cited case renders as the record it points at rather than as
        a bare integer the engineer cannot check.
        """
        out: dict[int, dict] = {}
        for result in self.available_tools:
            for row in result.data.get("results") or ():
                if isinstance(row, dict) and isinstance(row.get("defect_id"), int):
                    out.setdefault(row["defect_id"], row)
        return out

    def tasks(self) -> dict[str, dict]:
        """task_number → retrieved locator, for the same reason."""
        out: dict[str, dict] = {}
        for result in self.available_tools:
            for row in result.data.get("results") or ():
                if isinstance(row, dict) and row.get("task_number"):
                    out.setdefault(str(row["task_number"]).upper(), row)
        return out


# ── the evidence plan ───────────────────────────────────────────────────

def plan_tools(complaint: str, tail: str = "") -> list[tuple[str, dict]]:
    """The calls that make up one investigation, in order.

    Fixed rather than chosen by the model. A tool-calling loop would let the
    model decide what evidence it wants, which is the same thing as letting it
    decide what evidence it does not want — and the failure would be invisible,
    because the answer would still be grounded in everything it happened to
    look at.

    The unbacked tools are called anyway. `get_open_defects` and
    `get_compliance_context` are exactly the questions an engineer would ask
    next, and the honest answer — no closure state has been imported, no
    compliance export exists — is worth more than their silent absence.
    """
    complaint = (complaint or "").strip()
    tail = (tail or "").strip()
    plan: list[tuple[str, dict]] = [
        ("search_manual_tasks",
         {"query": complaint, "limit": MAX_TASKS,
          **({"tail": tail} if tail else {})}),
        # Not filtered by tail: a fleet-wide symptom match is the point of a
        # case base, and narrowing to one airframe before anything is known
        # about the fault throws away most of the evidence.
        ("search_similar_cases", {"symptom": complaint, "limit": MAX_CASES}),
        ("get_manual_metadata", {}),
    ]
    if tail:
        plan += [
            ("get_aircraft_history", {"tail": tail, "limit": 10}),
            ("get_repeat_defects", {"tail": tail, "limit": 10}),
            ("get_open_defects", {"tail": tail, "limit": 10}),
            ("get_compliance_context", {"tail": tail, "limit": 10}),
        ]
    return plan


def _case_ids(results: list[ToolResult], limit: int = 10) -> list[int]:
    seen: list[int] = []
    for result in results:
        if not result.ok:
            continue
        for row in result.data.get("results") or ():
            if isinstance(row, dict):
                did = row.get("defect_id")
                if isinstance(did, int) and did not in seen:
                    seen.append(did)
    return seen[:limit]


def _task_numbers(results: list[ToolResult]) -> list[str]:
    seen: list[str] = []
    for result in results:
        if not result.ok:
            continue
        for row in result.data.get("results") or ():
            if isinstance(row, dict) and row.get("task_number"):
                number = str(row["task_number"])
                if number not in seen:
                    seen.append(number)
    return seen


# ── the prompt ──────────────────────────────────────────────────────────

def _brief(complaint: str, tail: str,
           results: list[ToolResult]) -> tuple[str, list[str], list[int]]:
    """The evidence block, plus the two closed sets the answer is held to."""
    tasks: list[dict] = []
    cases: list[dict] = []
    unavailable: list[dict] = []

    for result in results:
        if not result.ok:
            unavailable.append({
                "tool": result.tool,
                "missing": list(result.missing),
                "why": result.error,
            })
            continue
        for row in result.data.get("results") or ():
            if not isinstance(row, dict):
                continue
            if row.get("task_number") and row.get("manual_id") is not None:
                if len(tasks) < MAX_TASKS and not any(
                        t["task_number"] == row["task_number"] for t in tasks):
                    tasks.append({
                        "task_number": row["task_number"],
                        "evidence_id": row.get("evidence_id"),
                        "title": row.get("title"),
                        "manual_id": row.get("manual_id"),
                        "manual_type": row.get("manual_type"),
                        "revision": row.get("revision"),
                        "ata_chapter": row.get("ata_chapter"),
                        "effectivity_result": row.get("effectivity_result"),
                        "retrieval_evidence": row.get("retrieval_evidence") or {},
                    })
            elif isinstance(row.get("defect_id"), int):
                if len(cases) < MAX_CASES and not any(
                        c["defect_id"] == row["defect_id"] for c in cases):
                    cases.append({
                        "defect_id": row["defect_id"],
                        "evidence_id": row.get("evidence_id"),
                        "tail": row.get("tail"),
                        "reported_at": row.get("reported_at"),
                        "ata_chapter": row.get("ata_chapter"),
                        "symptom": (row.get("symptom") or "")[:SNIPPET],
                        "action_text": (row.get("action_text") or "")[:SNIPPET],
                        "actions": row.get("actions") or [],
                        "finding": row.get("finding", "not recorded"),
                    })

    allowed_tasks = [t["task_number"] for t in tasks]
    allowed_cases = [c["defect_id"] for c in cases]
    block = json.dumps({
        "complaint": complaint,
        "tail": tail or None,
        "candidate_tasks": tasks,
        "candidate_cases": cases,
        "tools_that_could_not_answer": unavailable,
    }, indent=1, default=str)
    return block, allowed_tasks, allowed_cases


def build_prompt(complaint: str, tail: str, results: list[ToolResult], *,
                 index_version: str, model_version: str) -> str:
    block, allowed_tasks, allowed_cases = _brief(complaint, tail, results)
    # Both lists are spelled out even when empty. "(none)" is an instruction —
    # recommend nothing — whereas an absent line reads as no constraint at all.
    tasks_line = ", ".join(allowed_tasks) or "(none — recommend no task)"
    cases_line = ", ".join(str(i) for i in allowed_cases) or "(none)"
    return (
        f"EVIDENCE\n{block}\n\n"
        f"ALLOWED TASK NUMBERS: {tasks_line}\n"
        f"ALLOWED CASE IDS: {cases_line}\n\n"
        f'Set model_version to "{model_version}" and index_version to '
        f'"{index_version}".\n\n'
        f"{SCHEMA_NOTE}\n\n"
        f"COMPLAINT: {complaint}\n"
        f"Answer with the JSON object."
    )


def _provenance_violations(answer: AssistantAnswer,
                           prov: Provenance) -> list[Violation]:
    """Grounding for the stamps the answer put on itself.

    `check_grounding` verifies what the answer *says about the data*; nothing
    verifies what it says about itself. An answer stamped with the wrong index
    version cannot be re-run against the vectors that produced it, and one
    stamped with a model that did not write it attributes the output to the
    wrong system — which is the same failure as the endpoint serving a model
    nobody asked for, arriving from the other direction.
    """
    found: list[Violation] = []
    served = prov.served_model or prov.requested_model
    if prov.model_mismatch:
        found.append(Violation(
            "served_model_mismatch",
            f"the endpoint served {prov.served_model!r}, but the configured "
            f"model was {prov.requested_model!r}"))
    if prov.index_version and answer.index_version != prov.index_version:
        found.append(Violation(
            "wrong_index_version",
            f"the answer is stamped {answer.index_version!r} but was produced "
            f"against index {prov.index_version!r} — it could not be re-run"))
    if served and answer.model_version and answer.model_version != served:
        found.append(Violation(
            "wrong_model_version",
            f"the answer is stamped {answer.model_version!r} but was written "
            f"by {served!r}"))
    return found


# ── the service ─────────────────────────────────────────────────────────

class AIService:
    """Owns the tool registry and the model, and runs one investigation.

    Mirrors `SearchService` deliberately, down to the signal names: two panels
    on the same screen that queue background work in two different shapes is a
    screen with two sets of bugs.
    """

    def __init__(self, db_path: Path | str | None = None,
                 index_version: str | None = None, *,
                 model=None, registry: ToolRegistry | None = None,
                 searcher_factory=None) -> None:
        self.db_path = Path(db_path or config.DB_PATH)
        self.index_version = index_version or config.INDEX_VERSION
        self._registry = registry
        self._model = model
        self._model_tried = model is not None
        self._searcher_factory = searcher_factory
        self._pool = QThreadPool.globalInstance()
        self._cancel: CancelToken | None = None
        self.busy = False

    # ── the two lazily built halves ─────────────────────────────────────
    def registry(self) -> ToolRegistry:
        """Built once, on the worker thread, over its own read-only handle.

        The searcher is optional and its failure is contained: without one the
        task tool runs FTS5/BM25 over the same rows and says so in `source`,
        which is a narrower search rather than a wrong one.
        """
        if self._registry is None:
            searcher = None
            if self._searcher_factory is not None:
                try:
                    searcher = self._searcher_factory()
                except Exception:                            # noqa: BLE001
                    searcher = None
            self._registry = ToolRegistry(db_path=self.db_path,
                                          searcher=searcher)
        return self._registry

    def model(self):
        """The model service, or None. Probed once, exactly like the summariser.

        Every failure here — no configuration, an unreachable endpoint, an
        import that is not installed — resolves to None, because all of them
        mean the same thing to this screen.
        """
        if not self._model_tried:
            self._model_tried = True
            try:
                # The saved configuration, not a fresh default. Building
                # `LLMConfig()` here produced the default *Ollama* config
                # whatever the operator had configured, which is why the
                # application reported "not configured" while holding a
                # working Nemotron configuration.
                from ..llm import aiconfig
                self._model = aiconfig.service(self._settings_con())
            except Exception:                                # noqa: BLE001
                self._model = None
        return self._model

    def _settings_con(self):
        """A read-only connection for reading AI settings.

        Opened separately from the search connection and cached: the settings
        are read on the worker thread when a model is first needed, and this
        keeps that read off whatever connection the query is using.
        """
        if getattr(self, "_cfg_con", None) is None:
            import sqlite3
            try:
                self._cfg_con = sqlite3.connect(
                    f"file:{self.db_path.as_posix()}?mode=ro", uri=True,
                    timeout=5.0, check_same_thread=False)
            except sqlite3.Error:
                self._cfg_con = None
        return self._cfg_con

    def reload_configuration(self) -> None:
        """Drop the cached client so the next request uses new settings.

        Called when Admin saves. Without it a configuration change needed an
        application restart to take effect.
        """
        from ..llm import aiconfig
        aiconfig.invalidate()
        self._model = None
        self._model_tried = False

    def status(self) -> AIStatus:
        model = self.model()
        if model is None:
            return AIStatus(
                False,
                reason="no model is configured for this installation",
                index_version=self.index_version)
        try:
            health = model.health()
        except Exception as exc:                             # noqa: BLE001
            return AIStatus(False, reason=f"{type(exc).__name__}: {exc}",
                            index_version=self.index_version)
        identity = getattr(health, "identity", None) or ModelIdentity()
        return AIStatus(
            available=bool(getattr(health, "usable", health.ok)),
            reason=getattr(health, "reason", ""),
            identity=identity,
            endpoint=getattr(health, "endpoint", "") or identity.endpoint,
            provider=getattr(model, "provider", "") or identity.provider,
            index_version=self.index_version)

    # ── evidence ────────────────────────────────────────────────────────
    def gather(self, complaint: str, tail: str = "", user=None,
               cancel: CancelToken | None = None) -> list[ToolResult]:
        """Run the plan. A tool that refuses is a result, not an exception.

        `ToolError` — an unknown name, a denied permission, a bad argument — is
        caught and turned into a failed `ToolResult` rather than aborting the
        investigation. A model that may not read compliance data can still be
        asked about a symptom, and the panel shows which door was closed.
        """
        registry = self.registry()
        results: list[ToolResult] = []
        for name, arguments in plan_tools(complaint, tail):
            if cancel is not None and cancel.cancelled:
                break
            results.append(self._call(registry, name, arguments, user))

        ids = _case_ids(results)
        if ids and not (cancel is not None and cancel.cancelled):
            results.append(self._call(registry, "get_case_evidence",
                                      {"case_ids": ids}, user))
        # Effectivity is checked per candidate task rather than assumed from
        # the search result, so the answer's `effectivity_result` has a tool
        # call behind it. Bounded: the first three candidates are the ones an
        # engineer will actually look at.
        for number in _task_numbers(results)[:3]:
            if cancel is not None and cancel.cancelled:
                break
            arguments = {"task_number": number}
            if tail:
                arguments["tail"] = tail
            results.append(self._call(registry, "check_effectivity",
                                      arguments, user))
        # Declared contract, no data: whether the manual an engineer is about
        # to be sent to is approved for use. It answers "unknown", and that is
        # the answer Part-145 145.A.45 requires to be visible — a local
        # extraction is uncontrolled by definition, and the panel has to say so
        # rather than leave the question unasked.
        top = self._top_locator(results)
        if top is not None:
            results.append(self._call(
                registry, "check_document_authorization",
                {"manual_id": top[0], "task_number": top[1]}, user))
        return results

    @staticmethod
    def _top_locator(results: list[ToolResult]) -> tuple[int, str] | None:
        """`(manual_id, task_number)` of the first retrieved locator, if any."""
        for result in results:
            if not result.ok:
                continue
            for row in result.data.get("results") or ():
                if (isinstance(row, dict) and row.get("task_number")
                        and isinstance(row.get("manual_id"), int)):
                    return row["manual_id"], str(row["task_number"])
        return None

    @staticmethod
    def _call(registry: ToolRegistry, name: str, arguments: dict,
              user) -> ToolResult:
        try:
            return registry.call(name, arguments, user)
        except ToolError as exc:
            return ToolResult.failure(name, f"refused — {exc}")

    # ── one investigation ───────────────────────────────────────────────
    def run(self, complaint: str, *, tail: str = "", user=None,
            cancel: CancelToken | None = None) -> Investigation:
        """Blocking. Called on the worker thread, never on the UI one."""
        complaint = (complaint or "").strip()
        tail = (tail or "").strip()
        cancel = cancel or CancelToken()
        status = self.status()
        prov = Provenance(
            requested_model=status.identity.requested,
            served_model=status.identity.served,
            provider=status.provider,
            endpoint=status.endpoint,
            index_version=self.index_version)

        if not status.available:
            return Investigation(UNAVAILABLE, complaint=complaint, tail=tail,
                                 provenance=prov, reason=status.reason)

        results = self.gather(complaint, tail, user, cancel)
        prov = replace(prov, tools_called=tuple(r.tool for r in results))
        if cancel.cancelled:
            return Investigation(CANCELLED, complaint=complaint, tail=tail,
                                 tool_results=tuple(results), provenance=prov,
                                 reason="cancelled before the model was asked")

        # Nothing was retrieved, so there is no candidate set to hold an
        # answer to. Asking anyway would produce an answer grounded in nothing,
        # which `check_grounding` would reject one step later — with a message
        # about the model rather than about the corpus.
        if not _task_numbers(results) and not _case_ids(results):
            return Investigation(
                NO_EVIDENCE, complaint=complaint, tail=tail,
                tool_results=tuple(results), provenance=prov,
                reason="retrieval returned no task and no prior case for this "
                       "complaint, so there is nothing for the model to reason "
                       "over — the deterministic search on the left is the "
                       "answer here")

        model = self.model()
        prompt = build_prompt(
            complaint, tail, results, index_version=self.index_version,
            model_version=prov.served_model or prov.requested_model)
        try:
            generation = model.generate(prompt, system=SYSTEM,
                                        max_tokens=budget_for(ANSWER_TOKENS),
                                        cancel=cancel)
        except Cancelled:
            return Investigation(CANCELLED, complaint=complaint, tail=tail,
                                 tool_results=tuple(results), provenance=prov,
                                 reason="cancelled while the model was thinking")
        except Exception as exc:                             # noqa: BLE001
            return Investigation(FAILED, complaint=complaint, tail=tail,
                                 tool_results=tuple(results), provenance=prov,
                                 reason=f"{type(exc).__name__}: {exc}")

        identity = getattr(generation, "identity", None)
        if identity is not None and identity.served:
            prov = replace(prov, served_model=identity.served,
                           requested_model=identity.requested
                           or prov.requested_model)
        if cancel.cancelled:
            return Investigation(CANCELLED, complaint=complaint, tail=tail,
                                 tool_results=tuple(results), provenance=prov,
                                 reason="cancelled after the model answered")

        usage = getattr(generation, "usage", None)
        if usage is not None and getattr(usage, "truncated", False):
            # A truncated reasoning model returns a half-finished thought, not
            # a half-finished answer. Naming the budget is the only way anyone
            # will know to raise it.
            return Investigation(
                MALFORMED, complaint=complaint, tail=tail,
                tool_results=tuple(results), provenance=prov,
                reason="the model ran out of tokens before it finished — the "
                       "reasoning preamble consumed the budget")

        try:
            answer = parse_answer_json(generation.text)
        except SchemaError as exc:
            return Investigation(MALFORMED, complaint=complaint, tail=tail,
                                 tool_results=tuple(results), provenance=prov,
                                 reason=str(exc))

        report = check_grounding(answer, results)
        extra = _provenance_violations(answer, prov)
        if extra:
            report = GroundingReport(report.violations + tuple(extra))
        if not report.accepted:
            # `answer` is deliberately not carried into the rejected result.
            return Investigation(REJECTED, complaint=complaint, tail=tail,
                                 report=report, tool_results=tuple(results),
                                 provenance=prov,
                                 reason=report.describe())
        return Investigation(ACCEPTED, complaint=complaint, tail=tail,
                             answer=answer, report=report,
                             tool_results=tuple(results), provenance=prov)

    # ── async wrapper ───────────────────────────────────────────────────
    def submit(self, complaint: str, signals: "AISignals", **kw) -> bool:
        """Queue an investigation. Returns False when one is already running."""
        if self.busy:
            return False
        self.busy = True
        self._cancel = CancelToken()
        kw.setdefault("cancel", self._cancel)
        self._pool.start(_InvestigationJob(self, complaint, signals, kw))
        return True

    def cancel(self) -> None:
        """Ask the running investigation to stop.

        Cooperative, like everything else on this path: the job stops between
        stages and discards what it has, rather than a thread being killed
        mid-socket with a half-read response behind it.
        """
        if self._cancel is not None:
            self._cancel.cancel()

    def close(self) -> None:
        self.cancel()
        if self._registry is not None:
            self._registry.close()
            self._registry = None
        # The settings connection is separate from the search one and was
        # leaked on every close. Tolerates repeated calls: close() is called
        # from teardown paths that may run more than once.
        con = getattr(self, "_cfg_con", None)
        if con is not None:
            try:
                con.close()
            except Exception:                                    # noqa: BLE001
                pass
            self._cfg_con = None
class AISignals(QObject):
    """Signals cannot live on a QRunnable, so they get their own QObject."""

    done = Signal(object)          # the Investigation
    failed = Signal(str, str)      # complaint, message


class _InvestigationJob(QRunnable):
    def __init__(self, service: AIService, complaint: str,
                 signals: AISignals, kw: dict) -> None:
        super().__init__()
        self.service = service
        self.complaint = complaint
        self.signals = signals
        self.kw = kw

    def run(self) -> None:
        try:
            result = self.service.run(self.complaint, **self.kw)
        except Exception as exc:                             # noqa: BLE001
            # A terminal signal is a public statement that this job ended.
            # Publish the matching service state first; otherwise a fast slot
            # can receive ``failed`` while ``busy`` still says True.
            self.service.busy = False
            self.service._cancel = None
            self.signals.failed.emit(self.complaint,
                                     f"{type(exc).__name__}: {exc}")
        else:
            self.service.busy = False
            self.service._cancel = None
            self.signals.done.emit(result)
