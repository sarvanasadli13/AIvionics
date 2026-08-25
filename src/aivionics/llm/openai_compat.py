"""An OpenAI-compatible model provider — NVIDIA NIM, vLLM, TensorRT-LLM, SGLang.

The application is not tied to any of them. They all speak the same two
endpoints, so one client serves the lot and the choice becomes configuration:

    /v1/models            what this endpoint is actually serving
    /v1/chat/completions  generation, streaming or not

**Everything reaches the network through an injected `transport`**, so the
default is `urllib` and the tests are a `BytesIO`. Nothing in the suite opens a
socket, and nothing downloads a model.

Three behaviours here are not guesses — they came from probing
`nvidia/nemotron-3.5-lightning-30b-a3b` on NIM on 2026-08-22:

* **The served model is read back, not assumed.** The catalogue lists models
  the endpoint will not serve: `nvidia/nemotron-nano-3-30b-a3b` is listed and
  returns 404. `health()` reports what is actually there so Admin can say "you
  asked for X, the endpoint is serving Y".
* **A reasoning model's chain of thought is not the answer.** NVIDIA exposes
  it separately as ``reasoning_content`` when streaming.  If thinking is
  explicitly enabled, the client consumes those events but returns only final
  ``content``; private reasoning never reaches a screen or log.
* **Structured engineering answers use JSON mode without thinking.** NVIDIA's
  Nemotron 3.5 documentation recommends exactly that combination so the token
  budget is spent on the machine-validated JSON instead of a reasoning trace.

The API key is read from config and **never logged** — not in errors, not in
the Admin panel, not in the usage log.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Callable, Iterator

from .client import validate_endpoint
from .service import (Cancelled, CancelToken, Generation, Health,
                      LLMUnavailable, ModelIdentity, Stopwatch, Usage,
                      budget_for)

USER_AGENT = "AIvionics/0.1 (+offline maintenance decision support)"
DEFAULT_TIMEOUT = 120.0          # a reasoning model thinks before it answers
HEALTH_TIMEOUT = 8.0
DEFAULT_ANSWER_TOKENS = 400
NIM_TOP_P = 0.95
NIM_OUTPUT_BUDGET = 16_384       # NVIDIA's published Lightning example
DEFAULT_RETRIES = 1              # one bounded retry for transient transport only

Transport = Callable[..., object]


def _origin(url: str) -> tuple[str, str, int | None]:
    """Return the security origin used to decide whether auth may follow."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward an API-key-bearing request to a different origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            same_origin = _origin(req.full_url) == _origin(newurl)
        except ValueError:
            same_origin = False
        if not same_origin:
            raise urllib.error.URLError("cross-origin model redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_URL_OPENER = urllib.request.build_opener(_SameOriginRedirectHandler())


def urllib_transport(url: str, data: bytes | None, timeout: float,
                     headers: dict | None = None):
    request = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                 **(headers or {})})
    return _URL_OPENER.open(request, timeout=timeout)   # noqa: S310


class HTTPTransportError(LLMUnavailable):
    """A sanitized HTTP failure with machine-readable retry metadata."""

    def __init__(self, endpoint: str, status_code: int, *,
                 retry_after: float | None = None) -> None:
        self.status_code = int(status_code)
        self.retry_after = retry_after
        super().__init__(f"{endpoint} returned HTTP {self.status_code}")


def _retry_after(headers) -> float | None:
    """Return Retry-After delta seconds when the provider supplied them."""
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


class OpenAICompatClient:
    """One client for every OpenAI-compatible runtime.

    It never *decides* anything. It is asked for text; the caller validates
    that text before a character of it reaches a screen.
    """

    def __init__(self, endpoint: str, model: str, *, api_key: str = "",
                 timeout: float = DEFAULT_TIMEOUT,
                 health_timeout: float = HEALTH_TIMEOUT,
                 temperature: float = 0.0,
                 provider: str = "openai-compatible",
                 transport: Transport | None = None,
                 top_p: float | None = None,
                 enable_thinking: bool = False,
                 json_mode: bool = False,
                 prefer_streaming: bool = False,
                 retries: int = DEFAULT_RETRIES,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.endpoint = validate_endpoint(endpoint)
        self.model = model
        self._api_key = api_key or ""
        self.timeout = timeout
        self.health_timeout = health_timeout
        self.temperature = temperature
        self._provider = provider
        self.transport = transport or urllib_transport
        self.top_p = top_p
        self.enable_thinking = bool(enable_thinking)
        self.json_mode = bool(json_mode)
        self.prefer_streaming = bool(prefer_streaming)
        self.retries = max(0, int(retries))
        self.sleep = sleep

    # ── plumbing ──────────────────────────────────────────────────────
    @property
    def provider(self) -> str:
        return self._provider

    def _headers(self) -> dict:
        # The key goes here and nowhere else. It is never put in an error
        # message, a log row or an Admin field.
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _url(self, path: str) -> str:
        base = self.endpoint
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return f"{base}{path}"

    def _open(self, path: str, payload: dict | None, timeout: float):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            return self.transport(self._url(path), data, timeout, self._headers())
        except urllib.error.HTTPError as exc:
            # Never include the response body. Providers and reverse proxies
            # can reflect request headers there, including Authorization.
            raise HTTPTransportError(
                self.endpoint, exc.code,
                retry_after=_retry_after(getattr(exc, "headers", None))) from exc
        except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
            raise LLMUnavailable(f"{self.endpoint} — {exc}") from exc

    # ── identity and health ───────────────────────────────────────────
    def identity(self) -> ModelIdentity:
        """What the endpoint says it serves. Never raises."""
        try:
            with self._open("/models", None, self.health_timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (LLMUnavailable, ValueError, AttributeError):
            return ModelIdentity(requested=self.model, provider=self._provider,
                                 endpoint=self.endpoint)
        names = tuple(str(row.get("id", "")) for row in (payload.get("data") or [])
                      if isinstance(row, dict))
        served = next((n for n in names if n == self.model), "")
        if not served:
            # A namespaced or tagged name is the same model under another label.
            from .service import _same_model
            served = next((n for n in names if _same_model(n, self.model)), "")
        return ModelIdentity(requested=self.model, served=served,
                             provider=self._provider, endpoint=self.endpoint,
                             available=names)

    def health(self) -> Health:
        """Probe the endpoint. Never raises — an unreachable model is a state
        the Admin screen reports, not an error the application propagates."""
        with Stopwatch() as watch:
            try:
                with self._open("/models", None, self.health_timeout) as response:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
            except HTTPTransportError as exc:
                return Health(False, str(exc), endpoint=self.endpoint,
                               latency_ms=0.0, checked_at=time.time(),
                               identity=ModelIdentity(
                                   requested=self.model, provider=self._provider,
                                   endpoint=self.endpoint),
                               status_code=exc.status_code,
                               retry_after=exc.retry_after)
            except LLMUnavailable as exc:
                return Health(False, str(exc), endpoint=self.endpoint,
                              latency_ms=0.0, checked_at=time.time(),
                              identity=ModelIdentity(
                                  requested=self.model, provider=self._provider,
                                  endpoint=self.endpoint))
            except (ValueError, AttributeError) as exc:
                return Health(False, f"unexpected answer — {exc}",
                              endpoint=self.endpoint, checked_at=time.time())

        names = tuple(str(row.get("id", "")) for row in (payload.get("data") or [])
                      if isinstance(row, dict))
        from .service import _same_model
        served = next((n for n in names if _same_model(n, self.model)), "")
        identity = ModelIdentity(requested=self.model, served=served,
                                 provider=self._provider, endpoint=self.endpoint,
                                 available=names)
        if served:
            reason = "reachable"
        elif names:
            # Being listed is not being served — a catalogue can advertise a
            # model that answers 404. Say so rather than implying it is ready.
            reason = (f"reachable, but {self.model} is not among the "
                      f"{len(names)} models this endpoint lists")
        else:
            reason = "reachable, but the endpoint lists no models"
        return Health(True, reason, names, bool(served), self.endpoint,
                      identity, watch.ms, time.time())

    # ── generation ────────────────────────────────────────────────────
    def _payload(self, prompt: str, system: str | None, max_tokens: int | None,
                 stream: bool) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        budget = max_tokens or budget_for(DEFAULT_ANSWER_TOKENS)
        if self.enable_thinking:
            # Measured live on the bounded 6-task/8-case maintenance packet:
            # 4,000 tokens ended inside reasoning with no final JSON. NVIDIA's
            # current Lightning example uses 16,384 for both fields. This is a
            # ceiling, not a reservation or a claim that every call uses it.
            budget = max(budget, NIM_OUTPUT_BUDGET)
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": budget,
            "stream": stream,
        }
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if self.enable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": True}
            payload["reasoning_budget"] = budget
        return payload

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, HTTPTransportError):
            return exc.status_code in (408, 429, 500, 502, 503, 504)
        cause = exc.__cause__
        return isinstance(cause, (urllib.error.URLError, socket.timeout,
                                  TimeoutError, ConnectionError, OSError))

    def _with_retry(self, call, cancel: CancelToken | None):
        """Retry one transient request, honouring Retry-After when supplied."""
        for attempt in range(self.retries + 1):
            if cancel is not None:
                cancel.raise_if_cancelled()
            try:
                return call()
            except (HTTPTransportError, LLMUnavailable) as exc:
                if attempt >= self.retries or not self._retryable(exc):
                    raise
                wait = getattr(exc, "retry_after", None)
                self.sleep(min(8.0, max(0.0, float(wait)))
                           if wait is not None else min(2.0 ** attempt, 8.0))
        raise RuntimeError("model retry loop exhausted")

    def generate(self, prompt: str, *, system: str | None = None,
                 max_tokens: int | None = None,
                 cancel: CancelToken | None = None) -> Generation:
        def request():
            if self.prefer_streaming:
                return self._generate_streamed(prompt, system, max_tokens, cancel)
            return self._generate_buffered(prompt, system, max_tokens, cancel)

        return self._with_retry(request, cancel)

    def _generate_buffered(self, prompt: str, system: str | None,
                           max_tokens: int | None,
                           cancel: CancelToken | None) -> Generation:
        payload = self._payload(prompt, system, max_tokens, stream=False)
        with Stopwatch() as watch:
            with self._open("/chat/completions", payload, self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        if cancel is not None and cancel.cancelled:
            raise Cancelled("generation cancelled")
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise LLMUnavailable(f"non-JSON answer from the endpoint — {exc}") from exc

        choices = body.get("choices") or []
        first = choices[0] if choices else {}
        message = first.get("message") or {}
        text = str(message.get("content") or "")
        finish = str(first.get("finish_reason") or "")
        counts = body.get("usage") or {}
        details = counts.get("completion_tokens_details") or {}
        usage = Usage(
            prompt_tokens=int(counts.get("prompt_tokens") or 0),
            completion_tokens=int(counts.get("completion_tokens") or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            latency_ms=watch.ms,
            # `length` means the budget ran out. For a reasoning model that
            # usually means it ran out mid-thought and the answer never came.
            truncated=finish == "length",
            finish_reason=finish)
        served = str(body.get("model") or self.model)
        identity = ModelIdentity(requested=self.model, served=served,
                                 provider=self._provider, endpoint=self.endpoint)
        return Generation(text=text, identity=identity, usage=usage, raw=raw)

    def _generate_streamed(self, prompt: str, system: str | None,
                           max_tokens: int | None,
                           cancel: CancelToken | None) -> Generation:
        """Return final content while consuming—but never retaining—reasoning.

        A streamed response resets the socket read timeout whenever NVIDIA
        emits a reasoning event.  Long thought can therefore take longer than
        ``timeout`` in total without looking like a dead connection, while a
        genuinely silent connection remains bounded.
        """
        payload = self._payload(prompt, system, max_tokens, stream=True)
        fragments: list[str] = []
        served = self.model
        finish = ""
        counts: dict = {}
        with Stopwatch() as watch:
            with self._open("/chat/completions", payload, self.timeout) as response:
                for line in response:
                    if cancel is not None and cancel.cancelled:
                        raise Cancelled("generation cancelled")
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", "replace")
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    served = str(chunk.get("model") or served)
                    if isinstance(chunk.get("usage"), dict):
                        counts = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    first = choices[0]
                    if first.get("finish_reason") is not None:
                        finish = str(first.get("finish_reason") or "")
                    delta = first.get("delta") or {}
                    # Do not append reasoning_content.  It is private working,
                    # not evidence, and must never reach Generation.raw.
                    content = delta.get("content")
                    if content:
                        fragments.append(str(content))
        details = counts.get("completion_tokens_details") or {}
        usage = Usage(
            prompt_tokens=int(counts.get("prompt_tokens") or 0),
            completion_tokens=int(counts.get("completion_tokens") or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            latency_ms=watch.ms,
            truncated=finish in ("length", "max_tokens"),
            finish_reason=finish)
        identity = ModelIdentity(requested=self.model, served=served,
                                 provider=self._provider, endpoint=self.endpoint)
        return Generation(text="".join(fragments), identity=identity,
                          usage=usage, raw="")

    def stream(self, prompt: str, *, system: str | None = None,
               max_tokens: int | None = None,
               cancel: CancelToken | None = None) -> Iterator[str]:
        """Yield fragments as they arrive. Server-sent events, one JSON per line.

        A malformed line is skipped rather than aborting the stream: a partial
        answer that the validator then judges is more useful than none, and the
        validator is what stands between this text and the screen anyway.
        """
        payload = self._payload(prompt, system, max_tokens, stream=True)
        with self._open("/chat/completions", payload, self.timeout) as response:
            for line in response:
                if cancel is not None and cancel.cancelled:
                    raise Cancelled("generation cancelled")
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                fragment = delta.get("content")
                if fragment:
                    yield str(fragment)

    def as_callable(self) -> Callable[[str], str]:
        """Adapter for `retrieval.rerank.LLMReranker`, which takes any
        `llm(prompt) -> str` and falls back to dense order on anything it
        cannot parse."""
        return lambda prompt: self.generate(prompt).text
