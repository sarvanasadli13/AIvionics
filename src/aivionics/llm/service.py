"""The model-service boundary — one interface, several providers.

Phase 1 of the Nemotron work. The existing `OllamaClient` was written when
Ollama was the only target, and it is a good client; what it is not is an
*interface*. This module is the seam: `ModelService` describes what the
application needs from a model, and the providers underneath it can be Ollama,
an OpenAI-compatible server (NVIDIA NIM, vLLM, TensorRT-LLM, SGLang), or a
fake in a test.

**Nothing here decides anything.** A model returns text; the caller validates
that text before a character of it reaches a screen. That rule predates this
module and survives it.

Three things in here exist because of what was measured against the real
Nemotron endpoint on 2026-08-22, not because they seemed like good ideas:

* **`strip_reasoning` and `extract_json`.** Nemotron 3.5 Lightning is a
  reasoning model and emits its chain of thought first — *every time*.
  `/no_think` in the system prompt does not suppress it, and neither does
  "output strict JSON only, no prose". A structured-output parser that assumes
  the response *is* the JSON gets a paragraph beginning "Here's a thinking
  process:" instead.
* **`Usage.reasoning_tokens` and the budget note.** Two structured-extraction
  probes hit their token ceiling inside the thinking block and never reached
  the JSON at all. A budget sized for the answer is a budget that returns
  nothing.
* **`ModelIdentity.matches_request`.** A hosted catalogue lists models it will
  not serve — `nvidia/nemotron-nano-3-30b-a3b` is listed and returns 404. The
  Admin screen has to be able to say "you asked for X and the endpoint is
  serving Y", which means the identity has to be read back rather than
  assumed.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Protocol, Sequence


class LLMUnavailable(RuntimeError):
    """The model could not be reached or did not answer.

    Never fatal. Every caller falls back to the deterministic path — the
    application is complete without a model and must stay that way.
    """


class Cancelled(RuntimeError):
    """A generation was cancelled by its caller."""


class CancelToken:
    """Cooperative cancellation, checked between streamed chunks.

    Deliberately not a thread kill: the UI needs to stop *waiting*, and a
    half-finished generation is discarded rather than interrupted mid-socket.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled("generation cancelled")


@dataclass(frozen=True)
class ModelIdentity:
    """What the endpoint says it is, as opposed to what was asked for."""

    requested: str = ""
    served: str = ""
    provider: str = ""
    runtime: str = ""
    endpoint: str = ""
    available: tuple[str, ...] = ()

    @property
    def matches_request(self) -> bool:
        """False when the endpoint is serving something else.

        A listed model is not a served model, and silently answering from a
        different one is the failure this exists to surface.
        """
        if not self.requested or not self.served:
            return False
        return _same_model(self.requested, self.served)

    def describe(self) -> str:
        if not self.served:
            return f"{self.requested or 'no model'} — not confirmed by the endpoint"
        if self.matches_request:
            return f"{self.served} (confirmed)"
        return f"⚠ requested {self.requested}, endpoint is serving {self.served}"


def _same_model(a: str, b: str) -> bool:
    """`gemma3:4b` vs `gemma3`, `nvidia/x` vs `x` — same model, different name.

    Providers disagree about tags and namespaces; this compares the part that
    identifies the model rather than the part that identifies the packaging.
    """
    def core(name: str) -> str:
        name = (name or "").strip().lower()
        name = name.split("/")[-1]
        return name.split(":")[0]
    return bool(a) and bool(b) and core(a) == core(b)


@dataclass(frozen=True)
class Usage:
    """What one call cost, in tokens and in wall-clock time."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: float = 0.0
    truncated: bool = False
    finish_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def describe(self) -> str:
        parts = [f"{self.latency_ms:.0f} ms"]
        if self.total_tokens:
            parts.append(f"{self.prompt_tokens}+{self.completion_tokens} tokens")
        if self.reasoning_tokens:
            parts.append(f"{self.reasoning_tokens} of them reasoning")
        if self.truncated:
            parts.append("TRUNCATED — the budget ran out before the answer")
        return " · ".join(parts)


@dataclass(frozen=True)
class Generation:
    """One completed generation, and everything the audit log needs about it."""

    text: str = ""
    identity: ModelIdentity = field(default_factory=ModelIdentity)
    usage: Usage = field(default_factory=Usage)
    raw: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


@dataclass(frozen=True)
class Health:
    """Whether the endpoint is answering, and what it is serving.

    Kept structurally compatible with the older Ollama-only `Health` so the
    Admin screen and its tests do not have to change in the same step.
    """

    ok: bool
    reason: str = ""
    models: tuple[str, ...] = ()
    model_present: bool = False
    endpoint: str = ""
    identity: ModelIdentity = field(default_factory=ModelIdentity)
    latency_ms: float = 0.0
    checked_at: float = 0.0
    # Machine-readable transport metadata.  Keeping it separate from provider
    # prose lets callers classify and retry without retaining an error body
    # that could reflect request headers.
    status_code: int | None = None
    retry_after: float | None = None

    @property
    def usable(self) -> bool:
        return self.ok and self.model_present

    @property
    def serving_wrong_model(self) -> bool:
        return bool(self.ok and self.identity.served
                    and not self.identity.matches_request)


class ModelService(Protocol):
    """What the application needs from a model. Providers implement this."""

    @property
    def provider(self) -> str: ...

    def health(self) -> Health: ...

    def identity(self) -> ModelIdentity: ...

    def generate(self, prompt: str, *, system: str | None = None,
                 max_tokens: int | None = None,
                 cancel: CancelToken | None = None) -> Generation: ...

    def stream(self, prompt: str, *, system: str | None = None,
               max_tokens: int | None = None,
               cancel: CancelToken | None = None) -> Iterator[str]: ...


# ── reasoning-model output ──────────────────────────────────────────────
#
# Measured against nvidia/nemotron-3.5-lightning-30b-a3b through NVIDIA NIM.
# Every reply opened with a chain of thought, whatever the system prompt said.

_THINK_TAGS = re.compile(
    r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)

_THINK_PREAMBLE = re.compile(
    r"^\s*(here'?s?\s+(?:a|my|the)\s+thinking\s+process|"
    r"let me think|thinking:|reasoning:|thought process)\b", re.IGNORECASE)

# A budget sized for the JSON alone is a budget the answer never reaches.
REASONING_BUDGET_MULTIPLIER = 5


def strip_reasoning(text: str) -> str:
    """Remove a reasoning preamble, keeping the answer.

    Handles both shapes seen in the wild: explicit `<think>` tags, and the
    untagged "Here's a thinking process: 1. ..." prose Nemotron emits through
    an OpenAI-compatible endpoint. When the preamble is untagged there is no
    delimiter to cut on, so the *last* JSON or fenced block wins — which is
    where a reasoning model puts its conclusion.
    """
    cleaned = _THINK_TAGS.sub("", text or "")
    if not _THINK_PREAMBLE.search(cleaned):
        return cleaned.strip()
    tail = _last_json_block(cleaned)
    return (tail or cleaned).strip()


def _last_json_block(text: str) -> str:
    """The last balanced {...} or [...] in the text, or ""."""
    best = ""
    for opener, closer in (("{", "}"), ("[", "]")):
        depth = 0
        start = -1
        in_string = False
        escape = False
        for i, ch in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == opener:
                if depth == 0:
                    start = i
                depth += 1
            elif ch == closer and depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start:i + 1]
                    if len(candidate) > len(best):
                        best = candidate
    return best


def extract_json(text: str):
    """Parse the JSON a model meant to return, or raise `ValueError`.

    Tolerates: a reasoning preamble, a ```json fence, and trailing prose. Does
    **not** tolerate invalid JSON — an answer that cannot be parsed is rejected
    rather than repaired into something that looks trustworthy.
    """
    candidate = strip_reasoning(text or "")
    fenced = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    candidate = candidate.strip()
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    block = _last_json_block(candidate)
    if not block:
        raise ValueError("no JSON object or array in the model's answer")
    return json.loads(block)


def budget_for(expected_answer_tokens: int) -> int:
    """Token budget that leaves room for a reasoning model to think first.

    Not a guess: two structured-extraction probes against Nemotron ran out of
    budget inside the thinking block and returned no JSON at all.
    """
    return max(1, int(expected_answer_tokens)) * REASONING_BUDGET_MULTIPLIER


# ── usage recording ─────────────────────────────────────────────────────

@dataclass
class UsageLog:
    """The last N calls, in memory, for the Admin screen.

    In memory and per session on purpose: this is an operator's view of the
    running application, not a second audit trail competing with the
    hash-chained one in `aivionics.audit`.
    """

    limit: int = 50
    rows: list[tuple[float, str, str, Usage]] = field(default_factory=list)

    def record(self, provider: str, model: str, usage: Usage) -> None:
        self.rows.append((time.time(), provider, model, usage))
        if len(self.rows) > self.limit:
            del self.rows[:-self.limit]

    def recent(self, count: int = 10) -> Sequence[tuple[float, str, str, Usage]]:
        return tuple(self.rows[-count:][::-1])

    def mean_latency_ms(self) -> float:
        if not self.rows:
            return 0.0
        return sum(u.latency_ms for *_rest, u in self.rows) / len(self.rows)


class Stopwatch:
    """Milliseconds around a call, without every provider reinventing it."""

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *_exc):
        self.ms = (time.monotonic() - self._start) * 1000.0
        return False
