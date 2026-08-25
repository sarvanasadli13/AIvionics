"""Ollama client (PLAN Phase 5) — optional by design.

The endpoint is configurable because that single setting solves two problems at
once: an 8 GB office PC cannot host a model, and corporate IT may not allow the
install. Point the client at one LAN host that does have the RAM and the whole
department gets the feature without a single local install.

Which is why the RAM gate is **conditional on the endpoint being local**. A
machine with 8 GB talking to a server with 64 GB is not the case the gate
exists to catch; a machine with 8 GB trying to load a 2.5 GB model into its own
memory is.

Everything reaches the network through an injected `transport`, so the default
is `urllib` and the tests are a `BytesIO`. Nothing in the suite opens a socket
or downloads a model.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field as dataclass_field, replace
from typing import Callable, Iterator, Sequence
from urllib.parse import urlparse

from .service import (Cancelled, CancelToken, Generation, ModelIdentity,
                      Stopwatch, Usage)

# Gemma 3 4B at Q4_K_M is ~2.5 GB on disk and comfortably the largest model
# worth running on an office workstation.
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma3:4b"
MIN_RAM_GB = 16.0

# Providers this application can talk to. `ollama` is the local development
# runtime it started on; `openai` is every OpenAI-compatible server — NVIDIA
# NIM, vLLM, TensorRT-LLM, SGLang — which is how the Nemotron work is served.
PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENAI = "openai"
PROVIDERS = (PROVIDER_OLLAMA, PROVIDER_OPENAI)

# NVIDIA's hosted catalogue, and the model selected for this project. Reachable
# without local hardware, which is what makes the AI work startable at all on a
# 4 GB laptop — but hosted NIM has no availability guarantee and is a
# development and evaluation endpoint, not a production dependency.
NIM_ENDPOINT = "https://integrate.api.nvidia.com/v1"
NEMOTRON_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"

# Hard timeouts, both of them. A hung generate must not become a hung UI, and
# the health probe runs on a screen the user is waiting on.
DEFAULT_TIMEOUT = 30.0
HEALTH_TIMEOUT = 3.0

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})

_USER_AGENT = "AIvionics/0.1 (+offline maintenance decision support)"


class LLMUnavailable(RuntimeError):
    """The model could not be reached or did not answer. Never fatal: every
    caller falls back to the deterministic path."""


@dataclass(frozen=True)
class LLMConfig:
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    enabled: bool = False
    timeout: float = DEFAULT_TIMEOUT
    health_timeout: float = HEALTH_TIMEOUT
    min_ram_gb: float = MIN_RAM_GB
    temperature: float = 0.0
    num_predict: int = 400
    provider: str = PROVIDER_OLLAMA
    # Never rendered, never logged, never put in an error message.
    api_key: str = ""

    @property
    def is_local(self) -> bool:
        return is_local_endpoint(self.endpoint)

    @property
    def is_openai_compatible(self) -> bool:
        return self.provider == PROVIDER_OPENAI

    @classmethod
    def for_nim(cls, api_key: str = "", *, enabled: bool = False,
                model: str = NEMOTRON_MODEL) -> "LLMConfig":
        """A configuration pointed at NVIDIA's hosted catalogue.

        The timeout is generous on purpose: a reasoning model emits its chain
        of thought before the answer, and a budget sized for the answer alone
        times out inside the thinking.
        """
        return cls(endpoint=NIM_ENDPOINT, model=model, enabled=enabled,
                   provider=PROVIDER_OPENAI, api_key=api_key, timeout=120.0,
                   health_timeout=8.0)

    def with_endpoint(self, endpoint: str) -> "LLMConfig":
        return replace(self, endpoint=endpoint)


@dataclass(frozen=True)
class Health:
    ok: bool
    reason: str = ""
    models: tuple[str, ...] = ()
    model_present: bool = False
    endpoint: str = ""
    identity: ModelIdentity = dataclass_field(default_factory=ModelIdentity)

    @property
    def usable(self) -> bool:
        return self.ok and self.model_present


@dataclass(frozen=True)
class RamGate:
    """Whether local memory permits running the model here, and why."""

    allowed: bool
    reason: str
    ram_gb: float | None = None
    applies: bool = True


def is_local_endpoint(endpoint: str) -> bool:
    host = (urlparse(endpoint or "").hostname or "").lower()
    return host in LOCAL_HOSTS


def validate_endpoint(endpoint: str) -> str:
    """Accept only an HTTP(S) model endpoint with an explicit host."""
    value = (endpoint or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("model endpoint must use HTTP or HTTPS and include a host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("model endpoint must not contain credentials, query, or fragment")
    return value


def total_ram_gb() -> float | None:
    """Physical RAM in GB, or None when the platform will not say.

    Windows goes through `GlobalMemoryStatusEx`; POSIX through `sysconf`.
    Neither adds a dependency, and an unknown answer is reported as unknown
    rather than guessed — a guessed 8 would disable the feature on a machine
    that can run it.
    """
    try:                                            # Windows
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        kernel32 = getattr(ctypes, "windll", None)
        if kernel32 is not None:
            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if kernel32.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys / (1024 ** 3)
    except Exception:                               # noqa: BLE001 — probe only
        pass
    try:                                            # POSIX
        import os

        return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / (1024 ** 3)
    except (AttributeError, ValueError, OSError):
        return None


def ram_gate(config: LLMConfig, ram_gb: float | None = None) -> RamGate:
    """Apply the 16 GB floor — but only to a locally hosted model."""
    if config.is_openai_compatible:
        return RamGate(True, "the model runs on the configured server, not on "
                             "this machine", ram_gb, applies=False)
    if not config.is_local:
        return RamGate(True, f"model is served by {config.endpoint} — "
                             f"this machine's memory is not the constraint",
                       ram_gb, applies=False)
    detected = total_ram_gb() if ram_gb is None else ram_gb
    if detected is None:
        return RamGate(False,
                       "installed memory could not be detected — the local "
                       "model stays disabled rather than being started blind",
                       None)
    if detected < config.min_ram_gb:
        return RamGate(False,
                       f"{detected:.1f} GB installed, {config.min_ram_gb:.0f} GB "
                       f"needed to host the model locally — point the endpoint "
                       f"at a LAN host instead",
                       detected)
    return RamGate(True, f"{detected:.1f} GB installed", detected)


# ── transport ───────────────────────────────────────────────────────────

def urllib_transport(url: str, data: bytes | None, timeout: float):
    """Default transport. Returns a context-managed, line-iterable response."""
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
        method="POST" if data is not None else "GET")
    return urllib.request.urlopen(request, timeout=timeout)   # noqa: S310


Transport = Callable[[str, bytes | None, float], object]


class OllamaClient:
    """Thin client over the two Ollama endpoints this product needs.

    It never *decides* anything. It is asked for text, and the caller validates
    that text before a single character of it reaches a screen.
    """

    def __init__(self, config: LLMConfig | None = None,
                 transport: Transport | None = None) -> None:
        resolved = config or LLMConfig()
        self.config = replace(resolved,
                              endpoint=validate_endpoint(resolved.endpoint))
        self.transport = transport or urllib_transport

    @property
    def provider(self) -> str:
        return PROVIDER_OLLAMA

    # ── plumbing ─────────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return f"{self.config.endpoint.rstrip('/')}{path}"

    def _open(self, path: str, payload: dict | None, timeout: float):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            return self.transport(self._url(path), data, timeout)
        except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
            raise LLMUnavailable(f"{self.config.endpoint} — {exc}") from exc

    def _options(self, max_tokens: int | None = None) -> dict:
        return {"temperature": self.config.temperature,
                "num_predict": int(max_tokens or self.config.num_predict)}

    # ── health ───────────────────────────────────────────────────────────
    def health(self) -> Health:
        """Probe the endpoint. Never raises — an unreachable model is a state
        the Admin screen reports, not an error the app propagates."""
        try:
            with self._open("/api/tags", None, self.config.health_timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except LLMUnavailable as exc:
            return Health(False, str(exc), endpoint=self.config.endpoint)
        except (ValueError, AttributeError) as exc:
            return Health(False, f"unexpected answer — {exc}",
                          endpoint=self.config.endpoint)
        names = tuple(
            str(m.get("name", "")) for m in (payload.get("models") or [])
            if isinstance(m, dict))
        present = any(n == self.config.model or n.split(":")[0] ==
                      self.config.model.split(":")[0] for n in names)
        reason = "reachable" if present else (
            f"reachable, but {self.config.model} is not pulled "
            f"— run: ollama pull {self.config.model}")
        identity = ModelIdentity(
            requested=self.config.model,
            served=self.config.model if present else "",
            provider=self.provider,
            endpoint=self.config.endpoint,
            available=names)
        return Health(True, reason, names, present, self.config.endpoint,
                      identity)

    def identity(self) -> ModelIdentity:
        return self.health().identity

    # ── generation ───────────────────────────────────────────────────────
    def generate(self, prompt: str, *, system: str | None = None,
                 max_tokens: int | None = None,
                 cancel: CancelToken | None = None) -> Generation:
        if cancel is not None:
            cancel.raise_if_cancelled()
        payload = {"model": self.config.model, "prompt": prompt,
                   "stream": False, "options": self._options(max_tokens)}
        if system:
            payload["system"] = system
        with Stopwatch() as watch:
            with self._open("/api/generate", payload, self.config.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        if cancel is not None and cancel.cancelled:
            raise Cancelled("generation cancelled")
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise LLMUnavailable(f"non-JSON answer from the model — {exc}") from exc
        text = str(body.get("response", ""))
        served = str(body.get("model") or self.config.model)
        finish = str(body.get("done_reason") or "")
        identity = ModelIdentity(
            requested=self.config.model, served=served, provider=self.provider,
            endpoint=self.config.endpoint)
        usage = Usage(
            prompt_tokens=int(body.get("prompt_eval_count") or 0),
            completion_tokens=int(body.get("eval_count") or 0),
            latency_ms=watch.ms,
            truncated=finish == "length",
            finish_reason=finish)
        return Generation(text=text, identity=identity, usage=usage, raw=raw)

    def stream(self, prompt: str, *, system: str | None = None,
               max_tokens: int | None = None,
               cancel: CancelToken | None = None) -> Iterator[str]:
        """Yield response fragments as they arrive (Ollama emits NDJSON).

        A malformed line is skipped rather than aborting the stream: half a
        summary that the validator then judges is more useful than none, and
        the validator is what stands between this text and the screen anyway.
        """
        payload = {"model": self.config.model, "prompt": prompt,
                   "stream": True, "options": self._options(max_tokens)}
        if system:
            payload["system"] = system
        with self._open("/api/generate", payload, self.config.timeout) as resp:
            for line in resp:
                if cancel is not None and cancel.cancelled:
                    raise Cancelled("generation cancelled")
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                fragment = chunk.get("response")
                if fragment:
                    yield str(fragment)
                if chunk.get("done"):
                    break

    def as_callable(self) -> Callable[[str], str]:
        """Adapter for `retrieval.rerank.LLMReranker`, which takes any
        `llm(prompt) -> str` and falls back to dense order on anything it
        cannot parse."""
        return lambda prompt: self.generate(prompt).text


def build_service(config: LLMConfig | None = None, transport=None):
    """The client for `config.provider`. One seam, two implementations.

    Returns something satisfying `llm.service.ModelService` in both cases, so
    callers never branch on which runtime is behind the endpoint.
    """
    config = config or LLMConfig()
    if config.is_openai_compatible:
        from .openai_compat import OpenAICompatClient
        is_nvidia_nemotron = (
            "nvidia.com" in config.endpoint
            and config.model == NEMOTRON_MODEL)
        return OpenAICompatClient(
            config.endpoint, config.model, api_key=config.api_key,
            timeout=config.timeout, health_timeout=config.health_timeout,
            temperature=config.temperature,
            provider="nvidia-nim" if "nvidia.com" in config.endpoint
            else "openai-compatible",
            transport=transport,
            # Nemotron's structured-output guidance uses deterministic JSON
            # mode with thinking disabled.  The maintenance answer is checked
            # against a strict schema and grounding gate; a private reasoning
            # trace only consumes the budget and can truncate the JSON.
            json_mode=is_nvidia_nemotron,
            enable_thinking=False,
            prefer_streaming=is_nvidia_nemotron)
    return OllamaClient(config, transport)


def build_reranker(client: OllamaClient | None):
    """Wire the client into the Phase 2 LLM reranker, or return the no-op.

    Returning `NullReranker` rather than None keeps 'no LLM' a configuration of
    the same code path instead of a second one.
    """
    from ..retrieval.rerank import LLMReranker, NullReranker

    if client is None or not client.config.enabled:
        return NullReranker()
    return LLMReranker(client.as_callable())


def _where(config: LLMConfig) -> str:
    if config.is_openai_compatible:
        return "  (remote server)" if not config.is_local else "  (this machine)"
    return "  (this machine)" if config.is_local else "  (LAN host)"


def describe(config: LLMConfig, health=None, gate: RamGate | None = None
             ) -> Sequence[tuple[str, str]]:
    """Rows for the Admin panel: what is configured, and what is actually true.

    The two are different questions and only the second one matters. A hosted
    catalogue lists models it will not serve, so a configured model name is not
    evidence that anything is answering — the served identity is read back and
    a mismatch is stated rather than smoothed over.

    The API key is never a row here, in any form.
    """
    rows = [
        ("Provider", {PROVIDER_OLLAMA: "Ollama",
                      PROVIDER_OPENAI: "OpenAI-compatible server"}
         .get(config.provider, config.provider)),
        ("Endpoint", config.endpoint + _where(config)),
        ("Model requested", config.model),
        ("AI features", "enabled" if config.enabled else
         "disabled — every screen works without a model"),
        ("Without the model", "search, manuals, statistics and locator "
                              "printing all continue"),
    ]
    if config.is_openai_compatible:
        rows.append(("Credential",
                     "an API key is configured" if config.api_key
                     else "none — the endpoint must allow anonymous access"))
    if gate is not None and gate.applies:
        rows.append(("Installed memory",
                     f"{gate.ram_gb:.1f} GB" if gate.ram_gb else "not detected"))
        rows.append(("Memory gate", gate.reason))

    if health is None:
        return rows

    rows.append(("Health", health.reason if health.ok
                 else f"unreachable — {health.reason}"))
    identity = getattr(health, "identity", None)
    if identity is not None and (identity.served or identity.requested):
        rows.append(("Model served", identity.describe()))
        if getattr(health, "serving_wrong_model", False):
            rows.append((
                "⚠ Mismatch",
                f"this endpoint is answering as {identity.served}, not "
                f"{identity.requested} — anything it returns came from a "
                f"different model than the one configured"))
    latency = getattr(health, "latency_ms", 0.0)
    if latency:
        rows.append(("Last check", f"{latency:.0f} ms"))
    checked = getattr(health, "checked_at", 0.0)
    if checked:
        from datetime import datetime, timezone
        rows.append(("Checked at", datetime.fromtimestamp(
            checked, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")))
    if health.ok and health.models:
        shown = ", ".join(health.models[:6])
        if len(health.models) > 6:
            shown += f", and {len(health.models) - 6} more"
        rows.append(("Models this endpoint lists", shown))
    return rows
