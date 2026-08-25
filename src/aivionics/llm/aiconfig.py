"""The one place AI configuration is decided. No Qt in this module.

Every AI consumer — Diagnose, the case summariser, Admin, About, support
information — loads its configuration through `load()` here. Before this
module existed each of them called `build_service(LLMConfig())`, which builds
the *default* Ollama configuration regardless of what the operator had saved,
so the application reported "not configured" while holding a configuration.

Two separations are load-bearing:

* **Non-secret settings live in the `settings` table; the API key never
  does.** The key goes to Windows Credential Manager. If that is
  unavailable this module refuses to persist it rather than falling back to
  a file, because a key in a file is a key in a backup, a screenshot and a
  support bundle.
* **"Configured" and "working" are different facts.** A model named in
  settings means configured. Only a completion that came back from the
  requested model means ready — see `AIState`.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum

from .client import (DEFAULT_ENDPOINT, DEFAULT_MODEL, NEMOTRON_MODEL,
                     NIM_ENDPOINT, PROVIDER_OLLAMA, PROVIDER_OPENAI, PROVIDERS,
                     LLMConfig, validate_endpoint)

# Windows Credential Manager entry. Specific to this product and provider so
# it cannot collide with another application's NVIDIA credential.
CREDENTIAL_SERVICE = "AIvionics/NVIDIA-NIM"
CREDENTIAL_ACCOUNT = "api-key"
ENV_KEY = "NVIDIA_API_KEY"

DISPLAY_NAMES = {
    NEMOTRON_MODEL: "NVIDIA Nemotron 3.5 Lightning",
}

PRESETS = {
    "nim-nemotron": {
        "label": "NVIDIA NIM — Nemotron 3.5 Lightning",
        "provider": PROVIDER_OPENAI,
        "endpoint": NIM_ENDPOINT,
        "model": NEMOTRON_MODEL,
        "remote": True,
    },
    "ollama-local": {
        "label": "Local Ollama",
        "provider": PROVIDER_OLLAMA,
        "endpoint": DEFAULT_ENDPOINT,
        "model": DEFAULT_MODEL,
        "remote": False,
    },
}

# Non-secret settings keys. The API key is deliberately not in this list.
SETTINGS = {
    "ai_enabled": "0",
    "ai_provider": PROVIDER_OPENAI,
    "ai_endpoint": NIM_ENDPOINT,
    "ai_model": NEMOTRON_MODEL,
    "ai_temperature": "0.0",
    "ai_timeout": "120",
    "ai_health_timeout": "8",
    "ai_last_ok_at": "",
    "ai_last_served_model": "",
    "ai_privacy_ack": "0",
    "ai_rerank_enabled": "0",     # experimental; never on by configuring a model
    "ai_last_error": "",
}


class AIState(str, Enum):
    """What is actually true about the AI layer right now.

    "Not configured" used to stand for every one of these, which made the
    difference between *no endpoint* and *wrong key* invisible to the person
    who had to fix it.
    """

    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"
    KEY_REQUIRED = "key_required"
    ONLINE_DISABLED = "online_disabled"
    PRIVACY_NOT_ACKNOWLEDGED = "privacy_not_acknowledged"
    NOT_VERIFIED = "not_verified"
    CHECKING = "checking"
    READY = "ready"
    UNREACHABLE = "unreachable"
    AUTH_REJECTED = "auth_rejected"
    RATE_LIMITED = "rate_limited"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_MISMATCH = "model_mismatch"
    TIMEOUT = "timeout"
    SERVICE_UNAVAILABLE = "service_unavailable"
    MALFORMED = "malformed"
    TRUNCATED = "truncated"


STATE_LABELS = {
    AIState.NOT_CONFIGURED: "not configured",
    AIState.DISABLED: "disabled",
    AIState.KEY_REQUIRED: "API key required",
    AIState.ONLINE_DISABLED: "online access disabled",
    AIState.PRIVACY_NOT_ACKNOWLEDGED: "remote inference not acknowledged",
    AIState.NOT_VERIFIED: "configured, not verified",
    AIState.CHECKING: "checking",
    AIState.READY: "ready",
    AIState.UNREACHABLE: "endpoint unreachable",
    AIState.AUTH_REJECTED: "authentication rejected",
    AIState.RATE_LIMITED: "rate limited",
    AIState.MODEL_UNAVAILABLE: "requested model unavailable",
    AIState.MODEL_MISMATCH: "served-model mismatch",
    AIState.TIMEOUT: "request timed out",
    AIState.SERVICE_UNAVAILABLE: "service temporarily unavailable",
    AIState.MALFORMED: "response malformed",
    AIState.TRUNCATED: "response truncated",
}

# States in which no request may be made.
BLOCKING = {AIState.NOT_CONFIGURED, AIState.DISABLED, AIState.KEY_REQUIRED,
            AIState.ONLINE_DISABLED, AIState.PRIVACY_NOT_ACKNOWLEDGED}


def display_name(model: str) -> str:
    return DISPLAY_NAMES.get(model, model or "not configured")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── credential storage ───────────────────────────────────────────────────
class CredentialError(RuntimeError):
    """Secure storage is unavailable and a key was asked to be stored."""


def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def credential_backend() -> str:
    kr = _keyring()
    if kr is None:
        return ""
    try:
        backend = kr.get_keyring()
    except Exception:                                            # noqa: BLE001
        return ""
    name = backend.__class__.__name__
    return "" if "fail" in name.lower() else name


def get_api_key() -> str:
    """The key, from Credential Manager or the environment. Never persisted
    anywhere this function does not read from."""
    kr = _keyring()
    if kr is not None:
        try:
            stored = kr.get_password(CREDENTIAL_SERVICE, CREDENTIAL_ACCOUNT)
            if stored:
                return stored
        except Exception:                                        # noqa: BLE001
            pass
    return os.environ.get(ENV_KEY, "") or ""


def has_api_key() -> bool:
    return bool(get_api_key())


def key_source() -> str:
    """Where the key came from, for display. Never the key itself."""
    kr = _keyring()
    if kr is not None:
        try:
            if kr.get_password(CREDENTIAL_SERVICE, CREDENTIAL_ACCOUNT):
                return "Windows Credential Manager"
        except Exception:                                        # noqa: BLE001
            pass
    if os.environ.get(ENV_KEY):
        return f"{ENV_KEY} environment variable"
    return ""


def store_api_key(key: str, *, con: sqlite3.Connection | None = None) -> str:
    """Save the key to Credential Manager. Returns where it went.

    Refuses rather than degrading: a key written to a file would travel into
    backups, screenshots and support bundles, which is precisely what this
    function exists to prevent.
    """
    key = (key or "").strip()
    if not key:
        raise CredentialError("No API key was supplied.")
    kr = _keyring()
    if kr is None or not credential_backend():
        raise CredentialError(
            "Secure credential storage is not available on this machine, and "
            "AIvionics will not write an API key to disk. Set the "
            f"{ENV_KEY} environment variable instead, or install a keyring "
            "backend, then reopen this panel.")
    try:
        kr.set_password(CREDENTIAL_SERVICE, CREDENTIAL_ACCOUNT, key)
    except Exception as exc:                                     # noqa: BLE001
        # The message never carries the key.
        raise CredentialError(
            f"The credential store refused to save the key: "
            f"{type(exc).__name__}") from None
    # A successful verification belongs to the credential that performed it.
    # The credential value is intentionally not fingerprinted or persisted, so
    # replacement must explicitly invalidate that proof.
    clear_verification(con)
    invalidate()
    return credential_backend()


def remove_api_key(con: sqlite3.Connection | None = None) -> bool:
    """Delete the stored key, and drop any verification that relied on it."""
    kr = _keyring()
    removed = False
    if kr is not None:
        try:
            kr.delete_password(CREDENTIAL_SERVICE, CREDENTIAL_ACCOUNT)
            removed = True
        except Exception:                                        # noqa: BLE001
            removed = False
    clear_verification(con)
    invalidate()
    return removed


def clear_verification(con: sqlite3.Connection | None) -> None:
    """Forget that the configuration was ever verified.

    Called whenever the credential changes: a successful test proves what one
    key could do, and a replaced key has not been tested at all.
    """
    if con is None:
        return
    current = load(con)
    if current.last_ok_at or current.last_served_model or current.last_error:
        save(con, replace(current, last_ok_at="", last_served_model="",
                          last_error=""), previous=current)


# ── the configuration itself ─────────────────────────────────────────────
@dataclass(frozen=True)
class AISettings:
    """Everything non-secret, as saved. The key is never a field here."""

    enabled: bool = False
    provider: str = PROVIDER_OPENAI
    endpoint: str = NIM_ENDPOINT
    model: str = NEMOTRON_MODEL
    temperature: float = 0.0
    timeout: float = 120.0
    health_timeout: float = 8.0
    last_ok_at: str = ""
    last_served_model: str = ""
    privacy_ack: bool = False
    rerank_enabled: bool = False
    last_error: str = ""

    @property
    def is_remote(self) -> bool:
        from .client import is_local_endpoint
        return not is_local_endpoint(self.endpoint)

    @property
    def display(self) -> str:
        return display_name(self.model)

    def to_llm_config(self, api_key: str = "") -> LLMConfig:
        """The runtime configuration every AI consumer receives."""
        return LLMConfig(
            endpoint=self.endpoint, model=self.model, enabled=self.enabled,
            timeout=self.timeout, health_timeout=self.health_timeout,
            temperature=self.temperature, provider=self.provider,
            api_key=api_key)

    def describe(self) -> dict:
        """A safe description. There is no branch here that can emit a key."""
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "requested_model": self.model,
            "display_name": self.display,
            "enabled": self.enabled,
            "last_verified": self.last_ok_at or "never",
            "served_model": self.last_served_model or "not confirmed",
            "credential": key_source() or "none",
        }


def validate_settings(settings: AISettings) -> None:
    """Reject malformed configuration before it reaches storage or a socket."""
    if settings.provider not in PROVIDERS:
        raise ValueError(f"unsupported AI provider {settings.provider!r}")
    validate_endpoint(settings.endpoint)
    if not (settings.model or "").strip():
        raise ValueError("AI model name is required")
    if not 0.0 <= float(settings.temperature) <= 2.0:
        raise ValueError("AI temperature must be between 0 and 2")
    if float(settings.timeout) <= 0.0 or float(settings.health_timeout) <= 0.0:
        raise ValueError("AI timeouts must be greater than zero")


def _get(con, key: str) -> str:
    from ..ui import store
    return store.get_setting(con, key, SETTINGS.get(key, "")) or ""


def load(con: sqlite3.Connection | None) -> AISettings:
    """The saved configuration. Defaults are the NIM preset, disabled."""
    if con is None:
        return AISettings()

    def as_float(key: str, fallback: float) -> float:
        try:
            return float(_get(con, key))
        except (TypeError, ValueError):
            return fallback

    return AISettings(
        enabled=_get(con, "ai_enabled") in ("1", "true", "True"),
        provider=_get(con, "ai_provider") or PROVIDER_OPENAI,
        endpoint=_get(con, "ai_endpoint") or NIM_ENDPOINT,
        model=_get(con, "ai_model") or NEMOTRON_MODEL,
        temperature=as_float("ai_temperature", 0.0),
        timeout=as_float("ai_timeout", 120.0),
        health_timeout=as_float("ai_health_timeout", 8.0),
        last_ok_at=_get(con, "ai_last_ok_at"),
        last_served_model=_get(con, "ai_last_served_model"),
        privacy_ack=_get(con, "ai_privacy_ack") in ("1", "true", "True"),
        rerank_enabled=_get(con, "ai_rerank_enabled") in ("1", "true", "True"),
        last_error=_get(con, "ai_last_error"))


def identity(settings: AISettings, *, credential: str | None = None) -> tuple:
    """What a verification result is a statement *about*.

    A successful test proves that one endpoint served one model for one
    credential. Change any of those and the result describes a configuration
    that no longer exists, so it must not survive — see `save`.
    """
    if credential is None:
        credential = key_source()
    return (settings.provider, settings.endpoint, settings.model, credential)


def save(con: sqlite3.Connection, settings: AISettings, *,
         previous: AISettings | None = None) -> None:
    """Persist the non-secret configuration, atomically. Never the key.

    Written in one transaction: twelve separate committing writes could leave
    the endpoint changed and the model not, which is a configuration that was
    never chosen by anyone.

    If the identity changed, the previous verification is cleared in the same
    transaction. Leaving `last_ok_at` in place would report Ready on the
    strength of a different endpoint's successful test.
    """
    validate_settings(settings)
    if previous is None:
        previous = load(con)
    if identity(previous) != identity(settings):
        settings = replace(settings, last_ok_at="", last_served_model="",
                           last_error="")
    values = {
        "ai_enabled": "1" if settings.enabled else "0",
        "ai_provider": settings.provider,
        "ai_endpoint": settings.endpoint,
        "ai_model": settings.model,
        "ai_temperature": str(settings.temperature),
        "ai_timeout": str(settings.timeout),
        "ai_health_timeout": str(settings.health_timeout),
        "ai_last_ok_at": settings.last_ok_at,
        "ai_last_served_model": settings.last_served_model,
        "ai_privacy_ack": "1" if settings.privacy_ack else "0",
        "ai_rerank_enabled": "1" if settings.rerank_enabled else "0",
        "ai_last_error": settings.last_error,
    }
    outermost = not con.in_transaction
    if outermost:
        con.execute("BEGIN IMMEDIATE")
    try:
        for key, value in values.items():
            con.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
    except Exception:
        if outermost:
            con.rollback()
        raise
    if outermost:
        con.commit()
    # Only after the write is durable: invalidating first would leave the
    # cache rebuilt from a configuration that failed to save.
    invalidate()


def apply_preset(settings: AISettings, name: str) -> AISettings:
    preset = PRESETS.get(name)
    if preset is None:
        raise ValueError(f"unknown preset {name!r}")
    return replace(settings, provider=preset["provider"],
                   endpoint=preset["endpoint"], model=preset["model"])


# ── resolved state ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class AIStatus:
    state: AIState
    settings: AISettings
    detail: str = ""

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state.value)

    @property
    def summary(self) -> str:
        """What Admin and About both print. One sentence, no key, no path."""
        if self.state is AIState.NOT_CONFIGURED:
            return "not configured"
        return f"{self.settings.display} — {self.label}"

    @property
    def can_request(self) -> bool:
        return self.state not in BLOCKING


def status(con: sqlite3.Connection | None, *,
           online_enabled: bool | None = None) -> AIStatus:
    """Resolve the current state, without touching the network.

    Ordered so the *actionable* reason wins: telling somebody the endpoint is
    unreachable when they have not entered a key sends them to the wrong
    problem.
    """
    settings = load(con)
    if not settings.model or not settings.endpoint:
        return AIStatus(AIState.NOT_CONFIGURED, settings)
    if not settings.enabled:
        return AIStatus(AIState.DISABLED, settings)
    if settings.is_remote:
        if online_enabled is None and con is not None:
            from ..ui import store
            online_enabled = store.online_enabled(con)
        if online_enabled is False:
            return AIStatus(AIState.ONLINE_DISABLED, settings)
        if not has_api_key():
            return AIStatus(AIState.KEY_REQUIRED, settings)
        if not settings.privacy_ack:
            return AIStatus(AIState.PRIVACY_NOT_ACKNOWLEDGED, settings)
    if settings.last_error:
        try:
            return AIStatus(AIState(settings.last_error), settings,
                            settings.last_error)
        except ValueError:
            pass
    if not settings.last_ok_at or not settings.last_served_model:
        # A model name in settings is not evidence that anything serves it.
        return AIStatus(AIState.NOT_VERIFIED, settings)
    if settings.last_served_model != settings.model:
        return AIStatus(AIState.MODEL_MISMATCH, settings,
                        f"served {settings.last_served_model}")
    return AIStatus(AIState.READY, settings)


# ── the shared client ────────────────────────────────────────────────────
_CACHE: dict = {"key": None, "service": None}


def invalidate() -> None:
    """Drop the cached client so the next request rebuilds from settings."""
    _CACHE["key"] = None
    _CACHE["service"] = None


def service(con: sqlite3.Connection | None, *, online_enabled: bool | None = None):
    """The shared model client, or None when no request may be made.

    Cached on the configuration's own identity, so changing the endpoint,
    model or credential rebuilds it without an application restart — and a
    blocked state returns None rather than a client that will fail later.
    """
    current = status(con, online_enabled=online_enabled)
    if not current.can_request:
        return None
    api_key = get_api_key() if current.settings.is_remote else ""
    identity = (current.settings.provider, current.settings.endpoint,
                current.settings.model, bool(api_key),
                current.settings.timeout)
    if _CACHE["key"] == identity and _CACHE["service"] is not None:
        return _CACHE["service"]
    from .client import build_service
    built = build_service(current.settings.to_llm_config(api_key))
    _CACHE["key"], _CACHE["service"] = identity, built
    return built


def record_result(con: sqlite3.Connection, *, state: AIState,
                  served_model: str = "") -> None:
    """Remember the outcome of a verification, so status() can report it."""
    settings = load(con)
    if state is AIState.READY:
        settings = replace(settings, last_ok_at=utcnow(),
                           last_served_model=served_model, last_error="")
    else:
        settings = replace(settings, last_error=state.value)
    save(con, settings)


# ── the real serving test ────────────────────────────────────────────────
PROBE_PROMPT = "Reply with the single word: OK"
PROBE_TOKENS = 2048          # a reasoning model emits thought before the word


@dataclass(frozen=True)
class VerifyResult:
    state: AIState
    served_model: str = ""
    detail: str = ""
    listed: tuple = ()

    @property
    def ok(self) -> bool:
        return self.state is AIState.READY

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state.value)


def _classify(exc: Exception) -> tuple:
    """Map a transport failure onto a state, without leaking the key.

    The message is rebuilt from the exception *type* and status code rather
    than passed through, because a provider error body can echo the request
    headers back — including the Authorization header.
    """
    status_code = getattr(exc, "status_code", None)
    by_status = {
        401: AIState.AUTH_REJECTED,
        403: AIState.AUTH_REJECTED,
        404: AIState.MODEL_UNAVAILABLE,
        408: AIState.TIMEOUT,
        429: AIState.RATE_LIMITED,
        500: AIState.SERVICE_UNAVAILABLE,
        502: AIState.SERVICE_UNAVAILABLE,
        503: AIState.SERVICE_UNAVAILABLE,
        504: AIState.SERVICE_UNAVAILABLE,
    }
    if status_code in by_status:
        return by_status[status_code], f"{type(exc).__name__} ({status_code})"

    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    for needle, state in (("401", AIState.AUTH_REJECTED),
                          ("403", AIState.AUTH_REJECTED),
                          ("unauthor", AIState.AUTH_REJECTED),
                          ("429", AIState.RATE_LIMITED),
                          ("rate limit", AIState.RATE_LIMITED),
                          ("404", AIState.MODEL_UNAVAILABLE),
                          ("408", AIState.TIMEOUT),
                          ("timed out", AIState.TIMEOUT),
                          ("timeout", AIState.TIMEOUT),
                          ("500", AIState.SERVICE_UNAVAILABLE),
                          ("502", AIState.SERVICE_UNAVAILABLE),
                          ("503", AIState.SERVICE_UNAVAILABLE),
                          ("504", AIState.SERVICE_UNAVAILABLE)):
        if needle in lowered:
            return state, f"{type(exc).__name__} ({needle})"
    return AIState.UNREACHABLE, type(exc).__name__


class _HealthFailure(RuntimeError):
    """Turn a failed Health value into a typed internal retry signal."""

    def __init__(self, health) -> None:
        self.status_code = getattr(health, "status_code", None)
        self.retry_after = getattr(health, "retry_after", None)
        self.models = tuple(getattr(health, "models", ()) or ())
        # The real provider supplies a sanitized reason. This exception is
        # nevertheless never shown verbatim; _classify rebuilds the detail.
        super().__init__(getattr(health, "reason", "") or "health check failed")


def verify_settings(settings: AISettings, api_key: str, *,
                    service_factory=None, should_cancel=None) -> VerifyResult:
    """Run the serving test against an immutable snapshot.

    Takes no database connection **by design**. `AdminPage` used to hand its
    UI connection to a worker thread; SQLite's `check_same_thread` default
    made every settings read on that thread return the module defaults
    silently, so verification reported Disabled instead of testing the
    configured provider. A snapshot cannot be misread across a thread.
    """
    built = (service_factory() if service_factory is not None
             else build_from(settings, api_key))
    if built is None:
        return VerifyResult(AIState.NOT_CONFIGURED)

    def cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    if cancelled():
        return VerifyResult(AIState.CHECKING, detail="cancelled")

    # A. endpoint + auth, B. catalogue, C. is the model listed
    def checked_health():
        health = built.health()
        if not getattr(health, "ok", False):
            raise _HealthFailure(health)
        return health

    try:
        health = _with_retry(checked_health, should_cancel=should_cancel)
    except _Cancelled:
        return VerifyResult(AIState.CHECKING, detail="cancelled")
    except Exception as exc:                                     # noqa: BLE001
        state, detail = _classify(exc)
        return VerifyResult(state, detail=detail,
                            listed=tuple(getattr(exc, "models", ()) or ()))
    listed = tuple(getattr(health, "models", ()) or ())
    if listed and settings.model not in listed:
        return VerifyResult(AIState.MODEL_UNAVAILABLE, listed=listed,
                            detail="not in the endpoint's catalogue")

    # Cancellation is offered between the stages, so a long completion can be
    # abandoned without recording an outcome that never happened.
    if cancelled():
        return VerifyResult(AIState.CHECKING, detail="cancelled")

    # D. a small real completion, E. identity, F. completeness
    try:
        reply = _with_retry(
            lambda: built.generate(PROBE_PROMPT, max_tokens=PROBE_TOKENS),
            should_cancel=should_cancel)
    except _Cancelled:
        return VerifyResult(AIState.CHECKING, detail="cancelled")
    except Exception as exc:                                     # noqa: BLE001
        state, detail = _classify(exc)
        return VerifyResult(state, detail=detail, listed=listed)

    identity = getattr(reply, "identity", None)
    served = (getattr(identity, "served", "") or "").strip()
    text = (getattr(reply, "text", "") or "").strip()
    usage = getattr(reply, "usage", None)
    finish = (getattr(usage, "finish_reason", "") or "").lower()

    if finish in ("length", "max_tokens"):
        return VerifyResult(AIState.TRUNCATED, served_model=served,
                            listed=listed, detail="reply hit the token budget")
    if not text:
        return VerifyResult(AIState.MALFORMED, served_model=served,
                            listed=listed, detail="empty reply")
    from .service import _same_model
    if served and not _same_model(served, settings.model):
        return VerifyResult(AIState.MODEL_MISMATCH, served_model=served,
                            listed=listed,
                            detail=f"requested {settings.model}")
    return VerifyResult(AIState.READY, served_model=served or settings.model,
                        listed=listed)


def build_from(settings: AISettings, api_key: str):
    from .client import build_service
    validate_settings(settings)
    return build_service(settings.to_llm_config(api_key))


def snapshot(con, *, online_enabled: bool | None = None) -> tuple:
    """Everything a worker needs, read on the calling thread."""
    current = status(con, online_enabled=online_enabled)
    key = get_api_key() if current.settings.is_remote else ""
    return current, key


def verify(con, *, service_factory=None, online_enabled: bool | None = None,
           should_cancel=None) -> VerifyResult:
    """Prove the endpoint actually serves the requested model.

    `/v1/models` alone is not sufficient: a hosted catalogue lists models it
    will not serve — `nvidia/nemotron-nano-3-30b-a3b` is listed and returns
    404 on completion. So this runs the catalogue check *and* a small real
    completion, and confirms the identity that came back.

    Never called automatically at startup; the operator presses Test.
    """
    current, api_key = snapshot(con, online_enabled=online_enabled)
    if not current.can_request:
        return VerifyResult(current.state, detail=current.label)
    return verify_settings(current.settings, api_key,
                           service_factory=service_factory,
                           should_cancel=should_cancel)




# ── retry (finding 7) ────────────────────────────────────────────────────
RETRY_ATTEMPTS = 3
RETRY_BASE = 0.6
RETRY_CAP = 8.0

# Transient: the same request may succeed shortly. Everything else is a
# statement about the configuration, and repeating it only wastes the
# operator's time — a wrong key is wrong on the fourth attempt too.
RETRYABLE = {AIState.RATE_LIMITED, AIState.SERVICE_UNAVAILABLE,
             AIState.TIMEOUT, AIState.UNREACHABLE}
NEVER_RETRY = {AIState.AUTH_REJECTED, AIState.MODEL_UNAVAILABLE,
               AIState.MODEL_MISMATCH, AIState.MALFORMED,
               AIState.NOT_CONFIGURED}


class _Cancelled(Exception):
    """Raised inside the retry loop when the operator pressed Cancel."""


def _retry_after(exc) -> float | None:
    """Honour a provider's Retry-After when it gives one."""
    for attr in ("retry_after", "headers"):
        value = getattr(exc, attr, None)
        if value is None:
            continue
        if isinstance(value, dict):
            value = value.get("Retry-After") or value.get("retry-after")
        try:
            if value is not None:
                return max(0.0, min(float(value), RETRY_CAP))
        except (TypeError, ValueError):
            continue
    return None


def _with_retry(call, *, should_cancel=None, attempts: int = RETRY_ATTEMPTS,
                sleep=None):
    """Bounded retry with exponential backoff and full jitter."""
    import random
    import time
    sleep = sleep or time.sleep
    last = None
    for attempt in range(attempts):
        if should_cancel and should_cancel():
            raise _Cancelled()
        try:
            return call()
        except _Cancelled:
            raise
        except Exception as exc:                                 # noqa: BLE001
            last = exc
            state, _detail = _classify(exc)
            if state in NEVER_RETRY or state not in RETRYABLE:
                raise
            if attempt == attempts - 1:
                raise
            wait = _retry_after(exc)
            if wait is None:
                # Full jitter: a fixed backoff synchronises every client that
                # failed at the same moment into the same retry.
                wait = random.uniform(0.0, min(RETRY_BASE * 2 ** attempt,
                                               RETRY_CAP))
            if should_cancel and should_cancel():
                raise _Cancelled()
            sleep(wait)
    raise last if last else RuntimeError("retry exhausted")
