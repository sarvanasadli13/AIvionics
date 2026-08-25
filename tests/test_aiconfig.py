"""The AI configuration seam.

Every test here uses a temporary database and injected fake transports. None
of them touches the network — that is asserted directly in
`test_no_test_in_this_module_opens_a_socket`.
"""
from __future__ import annotations

import sqlite3

import pytest

from aivionics import db
from aivionics.llm import aiconfig as A
from aivionics.llm.service import Generation, Health, ModelIdentity
from aivionics.ui import store

SECRET = "FAKE-TEST-CREDENTIAL-not-a-real-key-do-not-scan"


@pytest.fixture()
def con(tmp_path):
    c = sqlite3.connect(str(tmp_path / "ai.db"))
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    store.ensure_ui_tables(c)
    A.invalidate()
    yield c
    c.close()
    A.invalidate()


@pytest.fixture()
def no_key(monkeypatch):
    """No credential anywhere, whatever this machine actually has stored."""
    monkeypatch.setattr(A, "_keyring", lambda: None)
    monkeypatch.delenv(A.ENV_KEY, raising=False)


@pytest.fixture()
def with_key(monkeypatch):
    monkeypatch.setattr(A, "_keyring", lambda: None)
    monkeypatch.setenv(A.ENV_KEY, SECRET)


def _enable(con, **over):
    settings = A.load(con)
    values = dict(enabled=True, privacy_ack=True)
    values.update(over)
    A.save(con, type(settings)(**{**settings.__dict__, **values}))
    store.set_setting(con, "online_enabled", "1")


# ── the preset ───────────────────────────────────────────────────────────
def test_the_nim_preset_uses_the_exact_endpoint_and_model():
    preset = A.PRESETS["nim-nemotron"]
    assert preset["endpoint"] == "https://integrate.api.nvidia.com/v1"
    assert preset["model"] == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert preset["label"] == "NVIDIA NIM — Nemotron 3.5 Lightning"
    assert A.display_name(preset["model"]) == "NVIDIA Nemotron 3.5 Lightning"


def test_defaults_are_the_nim_preset(con):
    settings = A.load(con)
    assert settings.endpoint == "https://integrate.api.nvidia.com/v1"
    assert settings.model == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert settings.enabled is False, "AI must not be on before it is set up"


def test_applying_a_preset_sets_provider_endpoint_and_model(con):
    settings = A.apply_preset(A.load(con), "ollama-local")
    assert settings.endpoint.startswith("http://127.0.0.1")
    settings = A.apply_preset(settings, "nim-nemotron")
    assert settings.model == "nvidia/nemotron-3.5-lightning-30b-a3b"


@pytest.mark.parametrize("changes", [
    {"provider": "typo-provider"},
    {"endpoint": "file:///C:/Windows/win.ini"},
    {"endpoint": "https:///missing-host"},
    {"model": ""},
    {"timeout": 0.0},
    {"health_timeout": -1.0},
    {"temperature": 99.0},
])
def test_invalid_ai_settings_are_refused_before_they_are_saved(con, changes):
    from dataclasses import replace

    before = A.load(con)
    with pytest.raises(ValueError):
        A.save(con, replace(before, **changes))
    assert A.load(con) == before


# ── persistence ──────────────────────────────────────────────────────────
def test_non_secret_configuration_survives_a_restart(con, tmp_path):
    _enable(con, temperature=0.25, timeout=99.0)
    con.commit()
    con.close()
    reopened = sqlite3.connect(str(tmp_path / "ai.db"))
    reopened.row_factory = sqlite3.Row
    settings = A.load(reopened)
    assert settings.enabled is True
    assert settings.temperature == 0.25
    assert settings.timeout == 99.0
    reopened.close()


# ── the API key is never persisted or printed ────────────────────────────
def test_the_api_key_is_absent_from_sqlite(con, with_key):
    _enable(con)
    con.commit()
    dump = "\n".join(con.iterdump())
    assert SECRET not in dump
    rows = con.execute("SELECT key, value FROM settings").fetchall()
    for key, value in rows:
        assert SECRET not in str(value), f"key leaked into settings.{key}"
        assert "api_key" not in key


def test_the_key_never_appears_in_a_configuration_description(con, with_key):
    described = A.load(con).describe()
    assert SECRET not in repr(described)
    assert described["credential"] == f"{A.ENV_KEY} environment variable"


def test_the_key_never_appears_in_the_resolved_status(con, with_key):
    _enable(con)
    current = A.status(con)
    assert SECRET not in current.summary
    assert SECRET not in repr(current.settings)


def test_storing_a_key_is_refused_when_there_is_no_secure_store(no_key):
    """A key written to a file travels into backups, screenshots and support
    bundles. Refusing is the correct behaviour, not a limitation."""
    with pytest.raises(A.CredentialError, match=A.ENV_KEY):
        A.store_api_key(SECRET)


def test_a_credential_error_never_contains_the_key(no_key):
    try:
        A.store_api_key(SECRET)
    except A.CredentialError as exc:
        assert SECRET not in str(exc)
    else:
        pytest.fail("expected a refusal")


def test_an_empty_key_is_refused(no_key):
    with pytest.raises(A.CredentialError):
        A.store_api_key("   ")


# ── states ───────────────────────────────────────────────────────────────
def test_missing_credentials_produce_api_key_required(con, no_key):
    _enable(con)
    current = A.status(con)
    assert current.state is A.AIState.KEY_REQUIRED
    assert current.summary == "NVIDIA Nemotron 3.5 Lightning — API key required"
    assert current.can_request is False


def test_ai_disabled_produces_disabled(con, with_key):
    current = A.status(con)          # enabled defaults to False
    assert current.state is A.AIState.DISABLED
    assert current.can_request is False


def test_online_disabled_blocks_every_hosted_request(con, with_key):
    _enable(con)
    store.set_setting(con, "online_enabled", "0")
    current = A.status(con)
    assert current.state is A.AIState.ONLINE_DISABLED
    assert current.can_request is False
    assert A.service(con) is None, "no client may be built at all"


def test_turning_online_off_does_not_delete_the_configuration(con, with_key):
    _enable(con)
    store.set_setting(con, "online_enabled", "0")
    assert A.load(con).enabled is True
    assert A.load(con).model == "nvidia/nemotron-3.5-lightning-30b-a3b"


def test_a_configured_but_unverified_model_is_not_ready(con, with_key):
    _enable(con)
    current = A.status(con)
    assert current.state is A.AIState.NOT_VERIFIED
    assert current.summary == ("NVIDIA Nemotron 3.5 Lightning — configured, "
                               "not verified")


def test_privacy_acknowledgement_is_required_for_remote_inference(con, with_key):
    _enable(con, privacy_ack=False)
    assert A.status(con).state is A.AIState.PRIVACY_NOT_ACKNOWLEDGED


def test_a_served_model_mismatch_is_not_ready(con, with_key):
    _enable(con)
    A.record_result(con, state=A.AIState.READY, served_model="some/other-model")
    assert A.status(con).state is A.AIState.MODEL_MISMATCH


def test_a_successful_verification_becomes_ready(con, with_key):
    _enable(con)
    A.record_result(con, state=A.AIState.READY,
                    served_model="nvidia/nemotron-3.5-lightning-30b-a3b")
    current = A.status(con)
    assert current.state is A.AIState.READY
    assert current.can_request is True


# ── the serving test, with fakes ─────────────────────────────────────────
class FakeHealth:
    def __init__(self, ok=True, models=(), reason=""):
        self.ok, self.models, self.reason = ok, tuple(models), reason


class FakeReply:
    def __init__(self, text="OK", model="", finish_reason="stop"):
        self.text, self.model, self.finish_reason = text, model, finish_reason


class FakeService:
    """An injected transport. Nothing here opens a socket."""

    def __init__(self, health=None, reply=None, raises=None):
        self._health = health or FakeHealth(
            models=("nvidia/nemotron-3.5-lightning-30b-a3b",))
        self._reply = reply
        self._raises = raises

    def health(self):
        if isinstance(self._raises, Exception) and self._reply is None:
            raise self._raises
        return self._health

    def generate(self, prompt, max_tokens=None):
        if self._raises is not None:
            raise self._raises
        reply = self._reply or FakeReply(
            model="nvidia/nemotron-3.5-lightning-30b-a3b")
        return Generation(
            text=reply.text,
            identity=ModelIdentity(
                requested="nvidia/nemotron-3.5-lightning-30b-a3b",
                served=reply.model),
            usage=type("ProbeUsage", (), {
                "finish_reason": reply.finish_reason,
            })())


def _verify(con, service):
    return A.verify(con, service_factory=lambda: service)


def test_verification_confirms_the_served_model(con, with_key):
    _enable(con)
    result = _verify(con, FakeService())
    assert result.state is A.AIState.READY
    assert result.served_model == "nvidia/nemotron-3.5-lightning-30b-a3b"


def test_a_listed_but_unserved_model_is_not_ready(con, with_key):
    """A hosted catalogue lists models it will not serve — the completion is
    what decides, not `/v1/models`."""
    _enable(con)
    result = _verify(con, FakeService(
        raises=RuntimeError("HTTP 404 model not found")))
    assert result.state is A.AIState.MODEL_UNAVAILABLE
    assert result.ok is False


def test_a_model_absent_from_the_catalogue_is_reported(con, with_key):
    _enable(con)
    result = _verify(con, FakeService(health=FakeHealth(models=("other/model",))))
    assert result.state is A.AIState.MODEL_UNAVAILABLE


def test_a_completion_from_a_different_model_is_rejected(con, with_key):
    _enable(con)
    result = _verify(con, FakeService(
        reply=FakeReply(model="nvidia/nemotron-nano-3-30b-a3b")))
    assert result.state is A.AIState.MODEL_MISMATCH
    assert result.ok is False


def test_a_truncated_reply_is_rejected(con, with_key):
    _enable(con)
    result = _verify(con, FakeService(
        reply=FakeReply(model="nvidia/nemotron-3.5-lightning-30b-a3b",
                        finish_reason="length")))
    assert result.state is A.AIState.TRUNCATED


def test_an_empty_reply_is_malformed(con, with_key):
    _enable(con)
    result = _verify(con, FakeService(
        reply=FakeReply(text="", model="nvidia/nemotron-3.5-lightning-30b-a3b")))
    assert result.state is A.AIState.MALFORMED


@pytest.mark.parametrize("message,expected", [
    ("HTTP 401 Unauthorized", A.AIState.AUTH_REJECTED),
    ("HTTP 403 Forbidden", A.AIState.AUTH_REJECTED),
    ("HTTP 429 Too Many Requests", A.AIState.RATE_LIMITED),
    ("HTTP 404 Not Found", A.AIState.MODEL_UNAVAILABLE),
    ("HTTP 500 Internal Server Error", A.AIState.SERVICE_UNAVAILABLE),
    ("HTTP 503 Service Unavailable", A.AIState.SERVICE_UNAVAILABLE),
    ("request timed out", A.AIState.TIMEOUT),
    ("connection refused", A.AIState.UNREACHABLE),
])
def test_transport_failures_map_to_distinct_states(con, with_key, message,
                                                   expected):
    _enable(con)
    result = _verify(con, FakeService(raises=RuntimeError(message)))
    assert result.state is expected, f"{message} -> {result.state}"


def test_a_failure_message_never_carries_the_key(con, with_key):
    _enable(con)
    result = _verify(con, FakeService(
        raises=RuntimeError(f"HTTP 401 Unauthorized: Bearer {SECRET}")))
    assert SECRET not in result.detail
    assert SECRET not in result.label


def test_recovery_after_a_transient_failure_needs_no_restart(con, with_key):
    _enable(con)
    A.record_result(con, state=_verify(
        con, FakeService(raises=RuntimeError("HTTP 503"))).state)
    assert A.status(con).state is A.AIState.SERVICE_UNAVAILABLE
    good = _verify(con, FakeService())
    A.record_result(con, state=good.state, served_model=good.served_model)
    assert A.status(con).state is A.AIState.READY


def test_verification_is_refused_when_a_request_may_not_be_made(con, no_key):
    _enable(con)
    result = A.verify(con, service_factory=lambda: FakeService())
    assert result.state is A.AIState.KEY_REQUIRED


# ── the shared seam ──────────────────────────────────────────────────────
def test_changing_configuration_invalidates_the_cached_client(con, with_key):
    _enable(con)
    A._CACHE["key"], A._CACHE["service"] = ("stale",), object()
    A.save(con, A.load(con))
    assert A._CACHE["service"] is None, "saving must drop the cached client"


def test_every_consumer_reads_the_same_seam():
    """Nothing may construct its own default configuration again."""
    import inspect
    from aivionics.ui import aiservice, searchservice
    from aivionics.ui.pages import about

    for module in (aiservice, searchservice, about):
        source = inspect.getsource(module)
        assert "build_service(LLMConfig())" not in source, (
            f"{module.__name__} builds its own default configuration")
        assert "aiconfig" in source, f"{module.__name__} bypasses the seam"


def test_maintenance_versions_reports_the_configured_model(con, with_key):
    from aivionics.admin import maintenance
    _enable(con)
    line = [l for l in maintenance.versions(con).lines() if l.startswith("LLM")]
    assert line and "Nemotron" in line[0]
    assert "not configured" not in line[0]
    assert SECRET not in line[0]


def test_the_reranker_is_not_enabled_by_configuring_a_model(con, with_key):
    """Held-out evaluation showed LLM reranking did not improve retrieval, so
    it must stay off until it is separately and deliberately enabled."""
    _enable(con)
    assert A.load(con).rerank_enabled is False


# ── no network ───────────────────────────────────────────────────────────
def test_no_test_in_this_module_opens_a_socket(con, with_key, monkeypatch):
    import socket

    def refuse(*_a, **_kw):
        raise AssertionError("the test suite must not reach the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    _enable(con)
    assert A.status(con).state is A.AIState.NOT_VERIFIED
    assert _verify(con, FakeService()).state is A.AIState.READY


# ── FINDING 1: no SQLite connection crosses a thread ─────────────────────
def test_verification_on_a_worker_thread_sees_the_real_configuration(con,
                                                                     with_key):
    """Admin used to hand its UI connection to a QRunnable. SQLite's
    `check_same_thread` default made every settings read on that thread
    return the module defaults *silently*, so verification resolved to
    Disabled instead of testing the configured provider.

    The snapshot is taken on the calling thread and carries no connection.
    """
    import threading

    _enable(con)
    current, api_key = A.snapshot(con)
    assert current.state is not A.AIState.DISABLED

    out = {}

    def worker():
        try:
            out["result"] = A.verify_settings(
                current.settings, api_key,
                service_factory=lambda: FakeService())
        except Exception as exc:                                 # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=10)

    assert "error" not in out, out.get("error")
    result = out["result"]
    assert result.state is A.AIState.READY, (
        f"worker resolved to {result.state} — a cross-thread SQLite read "
        f"silently returns defaults and gives Disabled")
    assert result.served_model == "nvidia/nemotron-3.5-lightning-30b-a3b"


def test_verify_settings_needs_no_database_connection():
    """The signature is the guarantee: it cannot read settings at all."""
    import inspect
    params = inspect.signature(A.verify_settings).parameters
    assert "con" not in params
    assert list(params)[:2] == ["settings", "api_key"]


def test_admin_passes_a_snapshot_to_the_worker_not_a_connection():
    import inspect
    from aivionics.ui.pages.admin import AdminPage
    source = inspect.getsource(AdminPage.test_ai_connection)
    assert "aiconfig.snapshot(con)" in source
    assert "verify_settings" in source
    assert "aiconfig.verify(con" not in source


# ── FINDING 2: a verification describes one configuration ────────────────
def _verified(con):
    _enable(con)
    A.record_result(con, state=A.AIState.READY,
                    served_model="nvidia/nemotron-3.5-lightning-30b-a3b")
    assert A.status(con).state is A.AIState.READY


@pytest.mark.parametrize("field,value", [
    ("endpoint", "https://someone-elses-endpoint/v1"),
    ("provider", "ollama"),
    ("model", "nvidia/nemotron-nano-3-30b-a3b"),
])
def test_changing_the_identity_clears_the_verification(con, with_key, field,
                                                       value):
    from dataclasses import replace
    _verified(con)
    A.save(con, replace(A.load(con), **{field: value}))
    settings = A.load(con)
    assert settings.last_ok_at == ""
    assert settings.last_served_model == ""
    assert settings.last_error == ""
    assert A.status(con).state is not A.AIState.READY


def test_changing_the_endpoint_reports_configured_not_verified(con, with_key):
    from dataclasses import replace
    _verified(con)
    A.save(con, replace(A.load(con), endpoint="https://elsewhere/v1"))
    assert A.status(con).summary == ("NVIDIA Nemotron 3.5 Lightning — "
                                     "configured, not verified")


def test_removing_the_credential_clears_the_verification(con, with_key,
                                                         monkeypatch):
    _verified(con)
    monkeypatch.setattr(A, "_keyring", lambda: None)
    A.remove_api_key(con)
    assert A.load(con).last_ok_at == ""


def test_replacing_the_credential_itself_clears_the_verification(
        con, monkeypatch):
    class WinVaultKeyring:
        pass

    class MemoryKeyring:
        backend = WinVaultKeyring()
        value = "old-key"

        @classmethod
        def get_keyring(cls):
            return cls.backend

        @classmethod
        def get_password(cls, _service, _account):
            return cls.value

        @classmethod
        def set_password(cls, _service, _account, value):
            cls.value = value

    monkeypatch.setattr(A, "_keyring", lambda: MemoryKeyring)
    monkeypatch.delenv(A.ENV_KEY, raising=False)
    _verified(con)
    A.store_api_key("replacement-key", con=con)
    assert A.load(con).last_ok_at == ""
    assert A.load(con).last_served_model == ""
    assert A.status(con).state is A.AIState.NOT_VERIFIED


def test_admin_gives_the_connection_to_credential_replacement():
    import inspect
    from aivionics.ui.pages.admin import AdminPage

    source = inspect.getsource(AdminPage.save_ai_configuration)
    assert "store_api_key(key, con=con)" in source


def test_a_display_only_change_keeps_the_verification(con, with_key):
    """Temperature does not change which endpoint serves which model."""
    from dataclasses import replace
    _verified(con)
    A.save(con, replace(A.load(con), temperature=0.4))
    assert A.status(con).state is A.AIState.READY
    assert A.load(con).last_served_model


# ── FINDING 4: persistence is atomic ─────────────────────────────────────
def test_a_failure_midway_through_saving_changes_nothing(con, with_key):
    from dataclasses import replace
    _enable(con)
    before = A.load(con)
    calls = {"n": 0}
    real = con.execute

    class Wrapper:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **kw):
            if str(sql).lstrip().upper().startswith("INSERT INTO SETTINGS"):
                calls["n"] += 1
                if calls["n"] == 4:
                    raise sqlite3.OperationalError("disk full")
            return real(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    with pytest.raises(sqlite3.OperationalError):
        A.save(Wrapper(con), replace(before, model="broken/model",
                                     endpoint="https://broken/v1"))
    after = A.load(con)
    assert after.model == before.model, "a failed save must change nothing"
    assert after.endpoint == before.endpoint


def test_the_cache_is_only_invalidated_after_a_successful_commit():
    import inspect
    source = inspect.getsource(A.save)
    commit_at = source.index("con.commit()")
    invalidate_at = source.index("invalidate()", commit_at)
    assert invalidate_at > commit_at


# ── FINDING 6: reachable is not the same as serving ──────────────────────
class _Health:
    def __init__(self, ok=True, model_present=True, usable=True):
        self.ok, self.model_present, self.usable = ok, model_present, usable


class _Client:
    def __init__(self, health):
        self._health = health

    def health(self):
        return self._health


def test_a_reachable_endpoint_without_the_model_is_refused():
    from aivionics.ui.searchservice import _usable
    assert _usable(_Client(_Health(ok=True, model_present=False,
                                   usable=False))) is False
    assert _usable(None) is False


def test_a_reachable_endpoint_serving_the_model_is_accepted():
    from aivionics.ui.searchservice import _usable
    assert _usable(_Client(_Health())) is True


def test_the_real_model_service_generate_interface_is_used():
    requested = "nvidia/nemotron-3.5-lightning-30b-a3b"

    class GenerateOnlyService:
        def health(self):
            return Health(True, "reachable", models=(requested,),
                          model_present=True)

        def generate(self, _prompt, **_kwargs):
            return Generation(
                text="OK",
                identity=ModelIdentity(requested=requested, served=requested))

    settings = A.AISettings(enabled=True, privacy_ack=True, model=requested)
    result = A.verify_settings(
        settings, SECRET, service_factory=GenerateOnlyService)
    assert result.state is A.AIState.READY, result
    assert result.served_model == requested


def test_unsuccessful_health_results_are_classified_and_retried():
    requested = "nvidia/nemotron-3.5-lightning-30b-a3b"

    class FlakyHealthService:
        attempts = 0

        def health(self):
            self.attempts += 1
            if self.attempts < 3:
                return Health(False, "HTTP 429", status_code=429,
                              retry_after=0.0)
            return Health(True, "reachable", models=(requested,),
                          model_present=True)

        def generate(self, _prompt, **_kwargs):
            return Generation(
                text="OK",
                identity=ModelIdentity(requested=requested, served=requested))

    service = FlakyHealthService()
    settings = A.AISettings(enabled=True, privacy_ack=True, model=requested)
    result = A.verify_settings(
        settings, SECRET, service_factory=lambda: service)
    assert service.attempts == A.RETRY_ATTEMPTS
    assert result.state is A.AIState.READY, result


def test_an_unreachable_endpoint_is_refused():
    from aivionics.ui.searchservice import _usable
    assert _usable(_Client(_Health(ok=False))) is False


# ── FINDING 5: connections are released ──────────────────────────────────
def test_the_ai_service_closes_its_settings_connection(tmp_path):
    from aivionics.ui.aiservice import AIService

    service = AIService(tmp_path / "none.db")
    service._settings_con()
    service.close()
    assert getattr(service, "_cfg_con", None) is None
    service.close()
    service.close()


def test_reloading_configuration_drops_the_cached_client(tmp_path):
    from aivionics.ui.aiservice import AIService

    service = AIService(tmp_path / "none.db")
    service._model, service._model_tried = object(), True
    service.reload_configuration()
    assert service._model is None and service._model_tried is False
    service.close()


def test_repeated_construction_is_safe(tmp_path):
    from aivionics.ui.aiservice import AIService
    for _ in range(3):
        service = AIService(tmp_path / "none.db")
        service._settings_con()
        service.close()


# ── FINDING 7: retry and cancellation ────────────────────────────────────
def test_transient_failures_are_retried_and_can_succeed():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("HTTP 503 Service Unavailable")
        return "ok"

    assert A._with_retry(flaky, sleep=lambda _s: None) == "ok"
    assert attempts["n"] == 3


@pytest.mark.parametrize("message", ["HTTP 401 Unauthorized",
                                     "HTTP 403 Forbidden",
                                     "HTTP 404 Not Found"])
def test_permanent_failures_are_never_retried(message):
    attempts = {"n": 0}

    def always():
        attempts["n"] += 1
        raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        A._with_retry(always, sleep=lambda _s: None)
    assert attempts["n"] == 1, "a wrong key is wrong on the fourth attempt too"


def test_retry_is_bounded():
    attempts = {"n": 0}

    def always():
        attempts["n"] += 1
        raise RuntimeError("HTTP 503")

    with pytest.raises(RuntimeError):
        A._with_retry(always, sleep=lambda _s: None)
    assert attempts["n"] == A.RETRY_ATTEMPTS


def test_retry_after_is_respected():
    slept = []

    class WithHeader(RuntimeError):
        retry_after = 2.5

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise WithHeader("HTTP 429 rate limited")
        return "ok"

    A._with_retry(flaky, sleep=slept.append)
    assert slept == [2.5]


def test_cancelling_stops_between_stages_and_records_nothing(con, with_key):
    _enable(con)
    current, api_key = A.snapshot(con)
    result = A.verify_settings(current.settings, api_key,
                               service_factory=lambda: FakeService(),
                               should_cancel=lambda: True)
    assert result.state is A.AIState.CHECKING
    assert result.ok is False
    assert result.detail == "cancelled"


def test_a_cancelled_test_leaves_the_previous_state_untouched(con, with_key):
    _verified(con)
    before = A.load(con).last_served_model
    current, api_key = A.snapshot(con)
    A.verify_settings(current.settings, api_key,
                      service_factory=lambda: FakeService(),
                      should_cancel=lambda: True)
    assert A.load(con).last_served_model == before


# ── FINDING 3: keyring is a declared dependency ──────────────────────────
def test_keyring_is_declared_as_a_runtime_dependency():
    import tomllib
    from pathlib import Path
    data = tomllib.loads((Path(__file__).resolve().parents[1] /
                          "pyproject.toml").read_text(encoding="utf-8"))
    deps = " ".join(data["project"]["dependencies"])
    assert "keyring" in deps, "secure key storage must not rely on a globally "\
                              "installed development package"


def test_an_insecure_backend_is_not_accepted(monkeypatch):
    """`keyring` falls back to a `fail.Keyring` when no real store exists.
    Accepting it would report a credential store that cannot store."""
    class FailKeyring:
        pass

    FailKeyring.__name__ = "fail.Keyring"

    class FakeModule:
        @staticmethod
        def get_keyring():
            return FailKeyring()

    monkeypatch.setattr(A, "_keyring", lambda: FakeModule())
    assert A.credential_backend() == ""
    with pytest.raises(A.CredentialError, match=A.ENV_KEY):
        A.store_api_key("anything")


def test_missing_keyring_reports_secure_storage_unavailable(monkeypatch):
    monkeypatch.setattr(A, "_keyring", lambda: None)
    assert A.credential_backend() == ""
    with pytest.raises(A.CredentialError, match="will not write an API key"):
        A.store_api_key("anything")
