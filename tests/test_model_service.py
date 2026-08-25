"""The model-service boundary — Phase 1.

Every network path is exercised through an injected transport. **Nothing here
opens a socket**, contacts NVIDIA, or downloads a model; the whole point of the
seam is that it can be tested without any of those.

The reasoning-model cases are not hypothetical. They are the behaviour measured
against `nvidia/nemotron-3.5-lightning-30b-a3b` on NVIDIA NIM on 2026-08-22:
a chain of thought before every answer that `/no_think` does not suppress, and
a token budget that runs out inside the thinking if it is sized for the answer.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from aivionics.llm import client as llm
from aivionics.llm import service
from aivionics.llm import openai_compat
from aivionics.llm.openai_compat import OpenAICompatClient


# ── a transport that never opens anything ───────────────────────────────

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def transport_for(payloads, record=None):
    """Answer each call from `payloads`, in order, recording what was sent."""
    queue = list(payloads)

    def transport(url, data, timeout, headers=None):
        if record is not None:
            record.append({
                "url": url, "timeout": timeout, "headers": headers or {},
                "body": json.loads(data.decode()) if data else None})
        body = queue.pop(0) if queue else "{}"
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        return FakeResponse(body.encode("utf-8"))
    return transport


MODELS = {"data": [{"id": "nvidia/nemotron-3.5-lightning-30b-a3b"},
                   {"id": "meta/llama-3.3-70b-instruct"}]}


def completion(text, *, finish="stop", prompt_tokens=40,
               completion_tokens=120, reasoning_tokens=0, model=None):
    return {
        "model": model or "nvidia/nemotron-3.5-lightning-30b-a3b",
        "choices": [{"message": {"role": "assistant", "content": text},
                     "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens,
                  "completion_tokens_details": {
                      "reasoning_tokens": reasoning_tokens}},
    }


def nim(payloads, record=None, model="nvidia/nemotron-3.5-lightning-30b-a3b"):
    return OpenAICompatClient(
        "https://example.invalid/v1", model, api_key="secret-key-do-not-log",
        transport=transport_for(payloads, record))


# ── the provider is configuration, not an assumption ────────────────────

def test_the_factory_builds_the_provider_the_config_names():
    assert type(llm.build_service(llm.LLMConfig())).__name__ == "OllamaClient"
    nim_config = llm.LLMConfig.for_nim("k")
    assert nim_config.is_openai_compatible
    assert type(llm.build_service(nim_config)).__name__ == "OpenAICompatClient"


def test_ollama_satisfies_the_same_generation_contract_as_nim():
    config = llm.LLMConfig(enabled=True)
    client = llm.build_service(config, transport=transport_for([
        {"models": [{"name": config.model}]},
        {"model": config.model, "response": "OK", "done_reason": "stop",
         "prompt_eval_count": 3, "eval_count": 2},
    ]))
    health = client.health()
    assert health.usable
    assert health.identity.matches_request

    token = service.CancelToken()
    result = client.generate("probe", max_tokens=17, cancel=token)
    assert isinstance(result, service.Generation)
    assert result.text == "OK"
    assert result.identity.matches_request
    assert result.usage.prompt_tokens == 3
    assert result.usage.completion_tokens == 2


@pytest.mark.parametrize("endpoint", [
    "file:///C:/Windows/win.ini",
    "ftp://example.com/model",
    "https:///missing-host",
    "not-a-url",
])
def test_model_endpoints_are_http_or_https_with_a_host(endpoint):
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        OpenAICompatClient(endpoint, "m")
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        llm.OllamaClient(llm.LLMConfig(endpoint=endpoint))


def test_the_memory_gate_does_not_apply_to_a_hosted_model():
    """The 16 GB floor exists to stop a small machine loading a model into its
    own memory. A hosted endpoint is not that case, and gating on it would
    disable the feature on exactly the machine it was meant to rescue."""
    gate = llm.ram_gate(llm.LLMConfig.for_nim("k"), ram_gb=4.0)
    assert gate.allowed and not gate.applies


def test_the_api_key_is_sent_and_never_returned(record=None):
    sent = []
    client = nim([MODELS], sent)
    health = client.health()
    assert sent[0]["headers"]["Authorization"].startswith("Bearer ")
    # and it appears nowhere a human or a log could read it
    assert "secret-key-do-not-log" not in repr(health)
    assert "secret-key-do-not-log" not in health.reason


def test_api_key_redirects_are_restricted_to_the_same_origin():
    handler = openai_compat._SameOriginRedirectHandler()
    request = openai_compat.urllib.request.Request(
        "https://integrate.api.nvidia.com/v1/models",
        headers={"Authorization": "Bearer do-not-forward"})

    accepted = handler.redirect_request(
        request, None, 307, "Temporary Redirect", {},
        "https://integrate.api.nvidia.com/v1/catalog/models")
    assert accepted.full_url.endswith("/v1/catalog/models")

    for target in (
        "https://attacker.invalid/collect",
        "http://integrate.api.nvidia.com/collect",
        "https://integrate.api.nvidia.com:444/collect",
    ):
        with pytest.raises(openai_compat.urllib.error.URLError,
                           match="cross-origin"):
            handler.redirect_request(
                request, None, 307, "Temporary Redirect", {}, target)


# ── health, identity, and the difference between them ───────────────────

def test_health_confirms_the_model_actually_served():
    health = nim([MODELS]).health()
    assert health.ok and health.model_present and health.usable
    assert health.identity.matches_request
    assert not health.serving_wrong_model
    assert health.latency_ms >= 0


def test_a_listed_model_is_not_a_served_model():
    """NVIDIA's catalogue lists models the endpoint will not serve —
    `nvidia/nemotron-nano-3-30b-a3b` is listed and answers 404. Admin has to be
    able to say so rather than implying the model is ready."""
    client = nim([MODELS], model="nvidia/nemotron-nano-3-30b-a3b")
    health = client.health()
    assert health.ok, "the endpoint answered"
    assert not health.model_present, "but it is not serving what we asked for"
    assert "not among" in health.reason
    assert not health.usable


def test_an_unreachable_endpoint_is_a_state_not_an_exception():
    def refuse(url, data, timeout, headers=None):
        raise OSError("connection refused")

    client = OpenAICompatClient("https://example.invalid", "m",
                                transport=refuse)
    health = client.health()
    assert not health.ok and "connection refused" in health.reason
    assert health.identity.requested == "m"


def test_http_health_failure_preserves_status_and_retry_after_but_not_body():
    reflected_secret = "server-reflected-secret-must-not-escape"
    attempts = {"n": 0}

    def rate_limited(url, data, timeout, headers=None):
        attempts["n"] += 1
        raise urllib.error.HTTPError(
            url, 429, "Too Many Requests", {"Retry-After": "2.5"},
            io.BytesIO(f"Authorization: Bearer {reflected_secret}".encode()))

    client = OpenAICompatClient("https://example.invalid", "m",
                                api_key=reflected_secret,
                                transport=rate_limited)
    health = client.health()
    assert attempts["n"] == 1
    assert not health.ok
    assert health.status_code == 429
    assert health.retry_after == 2.5
    assert reflected_secret not in health.reason
    assert reflected_secret not in repr(health)


def test_model_identity_reports_a_mismatch_rather_than_hiding_it():
    identity = service.ModelIdentity(requested="a/one", served="b/two")
    assert not identity.matches_request
    assert "requested a/one" in identity.describe()
    # a namespace or a tag is packaging, not a different model
    assert service.ModelIdentity(requested="nvidia/x", served="x:latest"
                                 ).matches_request


# ── generation, usage, truncation ───────────────────────────────────────

def test_generate_returns_the_text_and_what_it_cost():
    sent = []
    result = nim([completion("hello", prompt_tokens=51,
                             completion_tokens=300, reasoning_tokens=240)],
                 sent).generate("hi", system="be brief", max_tokens=500)
    assert result.ok and result.text == "hello"
    assert result.usage.prompt_tokens == 51
    assert result.usage.reasoning_tokens == 240
    assert result.usage.total_tokens == 351
    assert result.usage.latency_ms >= 0
    body = sent[0]["body"]
    assert body["max_tokens"] == 500
    assert body["messages"][0] == {"role": "system", "content": "be brief"}


def test_explicit_thinking_stream_returns_only_final_content():
    private = "private chain of thought must not escape"
    streamed = "\n".join([
        "data: " + json.dumps({
            "model": "nvidia/nemotron-3.5-lightning-30b-a3b",
            "choices": [{"delta": {"reasoning_content": private},
                         "finish_reason": None}]}),
        "data: " + json.dumps({
            "choices": [{"delta": {"content": '{\"ok\":true}'},
                         "finish_reason": "stop"}]}),
        "data: " + json.dumps({
            "choices": [], "usage": {"prompt_tokens": 9,
                                        "completion_tokens": 12,
                                        "completion_tokens_details": {
                                            "reasoning_tokens": 7}}}),
        "data: [DONE]",
    ])
    sent = []
    client = OpenAICompatClient(
        "https://integrate.api.nvidia.com/v1",
        "nvidia/nemotron-3.5-lightning-30b-a3b",
        api_key="secret", transport=transport_for([streamed], sent),
        top_p=0.95, enable_thinking=True, prefer_streaming=True)

    result = client.generate("diagnose", max_tokens=400)

    assert result.text == '{"ok":true}'
    assert private not in result.text and private not in result.raw
    assert result.identity.matches_request
    assert result.usage.reasoning_tokens == 7
    body = sent[0]["body"]
    assert body["stream"] is True
    assert body["top_p"] == 0.95
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert body["max_tokens"] == 16_384
    assert body["reasoning_budget"] == 16_384


def test_nim_factory_uses_documented_structured_json_mode_without_thinking():
    streamed = "\n".join([
        "data: " + json.dumps({
            "model": "nvidia/nemotron-3.5-lightning-30b-a3b",
            "choices": [{"delta": {"content": '{\"ok\":true}'},
                         "finish_reason": "stop"}]}),
        "data: [DONE]",
    ])
    sent = []
    config = llm.LLMConfig.for_nim("secret", enabled=True)
    client = llm.build_service(config, transport=transport_for([streamed], sent))

    assert client.generate("diagnose", max_tokens=4_000).text == '{"ok":true}'
    body = sent[0]["body"]
    assert body["stream"] is True
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 4_000
    assert body["response_format"] == {"type": "json_object"}
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_budget" not in body
    assert "top_p" not in body


def test_streamed_nim_generation_retries_429_once():
    attempts = []
    slept = []
    streamed = "data: " + json.dumps({
        "choices": [{"delta": {"content": "OK"},
                     "finish_reason": "stop"}]}) + "\ndata: [DONE]\n"

    def transport(url, data, timeout, headers=None):
        attempts.append(url)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(
                url, 429, "Too Many Requests", {"Retry-After": "0"}, None)
        return FakeResponse(streamed.encode())

    client = OpenAICompatClient(
        "https://example.invalid", "m", transport=transport,
        prefer_streaming=True, retries=1, sleep=slept.append)
    assert client.generate("x").text == "OK"
    assert len(attempts) == 2 and slept == [0.0]


def test_generation_never_retries_an_authentication_failure():
    attempts = []

    def transport(url, data, timeout, headers=None):
        attempts.append(url)
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    client = OpenAICompatClient(
        "https://example.invalid", "m", transport=transport,
        prefer_streaming=True, retries=1, sleep=lambda _seconds: None)
    with pytest.raises(service.LLMUnavailable, match="HTTP 401"):
        client.generate("x")
    assert len(attempts) == 1


def test_a_budget_that_ran_out_is_reported_as_truncated():
    """Measured live: a 300-token budget was consumed by the chain of thought
    and the JSON never arrived. Silently returning the thinking as the answer
    would be the worst possible outcome."""
    result = nim([completion("Here's a thinking process:\n1. ...",
                             finish="length")]).generate("x")
    assert result.usage.truncated
    assert result.usage.finish_reason == "length"
    assert "TRUNCATED" in result.usage.describe()


def test_a_non_json_answer_is_a_failed_fetch_not_a_crash():
    client = nim(["<html>502 Bad Gateway</html>"])
    with pytest.raises(service.LLMUnavailable, match="non-JSON"):
        client.generate("x")


def test_streaming_yields_fragments_and_stops_at_done():
    stream = "\n".join([
        "data: " + json.dumps({"choices": [{"delta": {"content": "one "}}]}),
        "",
        "data: not json at all",
        "data: " + json.dumps({"choices": [{"delta": {"content": "two"}}]}),
        "data: [DONE]",
        "data: " + json.dumps({"choices": [{"delta": {"content": "never"}}]}),
    ])
    client = nim([stream])
    assert "".join(client.stream("x")) == "one two"


def test_a_cancelled_generation_stops_streaming():
    token = service.CancelToken()
    payload = "".join(
        "data: " + json.dumps(
            {"choices": [{"delta": {"content": str(i)}}]}) + "\n"
        for i in range(10))
    client = nim([payload])
    out = []
    with pytest.raises(service.Cancelled):
        for fragment in client.stream("x", cancel=token):
            out.append(fragment)
            token.cancel()
    assert out == ["0"], "it stopped at the first chunk after the cancel"


def test_cancelling_before_a_generation_never_calls_out():
    called = []

    def transport(url, data, timeout, headers=None):
        called.append(url)
        return FakeResponse(b"{}")

    token = service.CancelToken()
    token.cancel()
    client = OpenAICompatClient("https://example.invalid", "m",
                                transport=transport)
    with pytest.raises(service.Cancelled):
        client.generate("x", cancel=token)
    assert called == []


# ── reasoning models: the answer is not the whole response ──────────────

def test_a_chain_of_thought_is_stripped_from_an_untagged_answer():
    """The shape Nemotron actually returns through an OpenAI-compatible
    endpoint. There is no delimiter, so the trailing JSON is the conclusion."""
    raw = ("Here's a thinking process:\n\n1.  **Analyze User Request:**\n"
           "   - They want JSON with keys a and b.\n"
           '   - I will use {"draft": 1} as a scratch value.\n\n'
           'Final answer:\n{"a": 1, "b": [2, 3]}')
    assert service.extract_json(raw) == {"a": 1, "b": [2, 3]}


def test_tagged_thinking_is_removed_too():
    raw = '<think>weighing it up</think>\n{"ok": true}'
    assert service.extract_json(raw) == {"ok": True}
    assert "weighing" not in service.strip_reasoning(raw)


def test_a_fenced_block_is_unwrapped():
    assert service.extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_an_answer_with_no_json_is_rejected_not_repaired():
    """An unparseable answer is refused. Repairing it into something that looks
    trustworthy is the failure this exists to prevent."""
    with pytest.raises(ValueError, match="no JSON"):
        service.extract_json("I think the answer is probably ATA 31.")
    with pytest.raises(ValueError):
        service.extract_json('{"unterminated": ')


def test_the_token_budget_leaves_room_to_think():
    assert service.budget_for(400) == 400 * service.REASONING_BUDGET_MULTIPLIER
    assert service.budget_for(0) >= 1


# ── usage recording, for the Admin screen ───────────────────────────────

def test_the_usage_log_keeps_the_recent_calls_and_forgets_the_rest():
    log = service.UsageLog(limit=3)
    for i in range(5):
        log.record("nim", "m", service.Usage(latency_ms=float(i * 100)))
    assert len(log.rows) == 3
    assert [u.latency_ms for *_r, u in log.recent()] == [400.0, 300.0, 200.0]
    assert log.mean_latency_ms() == 300.0


def test_an_empty_usage_log_has_no_opinion_about_latency():
    assert service.UsageLog().mean_latency_ms() == 0.0
