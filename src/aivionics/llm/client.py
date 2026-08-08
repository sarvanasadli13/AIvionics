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
from dataclasses import dataclass, replace
from typing import Callable, Iterator, Sequence
from urllib.parse import urlparse

# Gemma 3 4B at Q4_K_M is ~2.5 GB on disk and comfortably the largest model
# worth running on an office workstation.
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma3:4b"
MIN_RAM_GB = 16.0

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

    @property
    def is_local(self) -> bool:
        return is_local_endpoint(self.endpoint)

    def with_endpoint(self, endpoint: str) -> "LLMConfig":
        return replace(self, endpoint=endpoint)


@dataclass(frozen=True)
class Health:
    ok: bool
    reason: str = ""
    models: tuple[str, ...] = ()
    model_present: bool = False
    endpoint: str = ""

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
        self.config = config or LLMConfig()
        self.transport = transport or urllib_transport

    # ── plumbing ─────────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return f"{self.config.endpoint.rstrip('/')}{path}"

    def _open(self, path: str, payload: dict | None, timeout: float):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            return self.transport(self._url(path), data, timeout)
        except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
            raise LLMUnavailable(f"{self.config.endpoint} — {exc}") from exc

    def _options(self) -> dict:
        return {"temperature": self.config.temperature,
                "num_predict": self.config.num_predict}

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
        return Health(True, reason, names, present, self.config.endpoint)

    # ── generation ───────────────────────────────────────────────────────
    def generate(self, prompt: str, *, system: str | None = None) -> str:
        payload = {"model": self.config.model, "prompt": prompt,
                   "stream": False, "options": self._options()}
        if system:
            payload["system"] = system
        with self._open("/api/generate", payload, self.config.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return str(json.loads(raw).get("response", ""))
        except ValueError as exc:
            raise LLMUnavailable(f"non-JSON answer from the model — {exc}") from exc

    def stream(self, prompt: str, *, system: str | None = None) -> Iterator[str]:
        """Yield response fragments as they arrive (Ollama emits NDJSON).

        A malformed line is skipped rather than aborting the stream: half a
        summary that the validator then judges is more useful than none, and
        the validator is what stands between this text and the screen anyway.
        """
        payload = {"model": self.config.model, "prompt": prompt,
                   "stream": True, "options": self._options()}
        if system:
            payload["system"] = system
        with self._open("/api/generate", payload, self.config.timeout) as resp:
            for line in resp:
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
        return lambda prompt: self.generate(prompt)


def build_reranker(client: OllamaClient | None):
    """Wire the client into the Phase 2 LLM reranker, or return the no-op.

    Returning `NullReranker` rather than None keeps 'no LLM' a configuration of
    the same code path instead of a second one.
    """
    from ..retrieval.rerank import LLMReranker, NullReranker

    if client is None or not client.config.enabled:
        return NullReranker()
    return LLMReranker(client.as_callable())


def describe(config: LLMConfig, health: Health | None,
             gate: RamGate | None) -> Sequence[tuple[str, str]]:
    """Rows for the Admin panel: what is configured and what is actually true."""
    rows = [
        ("Endpoint", config.endpoint +
         ("  (this machine)" if config.is_local else "  (LAN host)")),
        ("Model", config.model),
        ("Enabled", "yes" if config.enabled else "no — the app is complete without it"),
    ]
    if gate is not None:
        rows.append(("Installed memory",
                     f"{gate.ram_gb:.1f} GB" if gate.ram_gb else "not detected"))
        rows.append(("Memory gate", gate.reason))
    if health is not None:
        rows.append(("Health", health.reason if health.ok
                     else f"unreachable — {health.reason}"))
        if health.ok:
            rows.append(("Models present", ", ".join(health.models) or "none"))
    return rows
