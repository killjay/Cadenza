import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cadenza_backend.agents.model_client import ModelClient, AIError, normalize_usage
from cadenza_backend.agents.sketch_editor import CARD


def client(provider="openrouter", **kwargs):
    c = ModelClient(**kwargs)
    c.s = c.s.model_copy(update={"anthropic_api_key": "fixture", "openrouter_api_key": "fixture",
        "deepseek_api_key": "fixture", "model_machinist": f"{provider}:fixture-model"})
    return c


def install_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def call(c):
    return asyncio.run(c.complete_json(stage="machinist", agent_card=CARD, user="synthetic"))


def test_compatible_usage_and_unchanged_return(monkeypatch):
    events = []
    install_http(monkeypatch, lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": '{"summary":"Need size","question":"What size?"}'}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 30},
                  "cost": .001}}))
    result = call(client(observe=events.append))
    assert result == {"summary": "Need size", "question": "What size?"}
    assert len(events) == 1 and events[0]["outcome"] == "response"
    assert events[0]["usage"] == dict(input_tokens=100, output_tokens=20, cache_read_tokens=30,
                                    cache_write_tokens=None, reported_cost_usd=.001)
    assert "synthetic" not in str(events) and "fixture" not in str(events[0]["usage"])


@pytest.mark.parametrize("raw", [None, {}, {"prompt_tokens": -1, "completion_tokens": True, "cost": float("nan")}])
def test_missing_or_invalid_usage_is_not_zero(raw):
    usage = normalize_usage("openrouter", raw)
    assert usage["input_tokens"] is None and usage["output_tokens"] is None
    assert usage["reported_cost_usd"] is None


def test_provider_cache_accounting():
    assert normalize_usage("anthropic", dict(input_tokens=100, output_tokens=20,
        cache_read_input_tokens=40, cache_creation_input_tokens=10)) == dict(
            input_tokens=150, output_tokens=20, cache_read_tokens=40, cache_write_tokens=10, reported_cost_usd=None)
    assert normalize_usage("anthropic", dict(input_tokens=100))["input_tokens"] is None
    assert normalize_usage("deepseek", dict(prompt_tokens=100, completion_tokens=0,
        prompt_cache_hit_tokens=80))["cache_read_tokens"] == 80
    assert normalize_usage("openrouter", dict(prompt_tokens=0, completion_tokens=0, cost=0))["reported_cost_usd"] == 0


@pytest.mark.parametrize("failure", ["http", "timeout", "invalid_json"])
def test_failure_observation_and_no_retry(monkeypatch, failure):
    events, attempted = [], []
    def respond(request):
        attempted.append(request)
        if failure == "timeout": raise httpx.ReadTimeout("fixture timeout")
        if failure == "http": return httpx.Response(429, text="limited")
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
    install_http(monkeypatch, respond)
    with pytest.raises((AIError, httpx.ReadTimeout)):
        call(client(observe=events.append))
    assert len(attempted) == len(events) == 1
    assert events[0]["usage"] is None or events[0]["usage"]["input_tokens"] is None


def test_guard_runs_before_network(monkeypatch):
    def forbidden(request): pytest.fail("Network attempted")
    install_http(monkeypatch, forbidden)
    def guard(provider, model): raise RuntimeError("no budget")
    with pytest.raises(RuntimeError, match="no budget"): call(client(before_request=guard))


def test_truncated_compatible_response_cannot_be_accepted_even_if_json_is_valid(monkeypatch):
    install_http(monkeypatch, lambda request: httpx.Response(200, json={
        "choices": [{"finish_reason": "length", "message": {"content": '{"summary":"partial","actions":[]}'}}]}))
    with pytest.raises(AIError, match="truncated"):
        call(client())


@pytest.mark.parametrize("guarded", [False, True])
def test_anthropic_usage_and_guarded_sdk_retries(monkeypatch, guarded):
    import anthropic
    events, options = [], []
    block = SimpleNamespace(type="tool_use", name="emit", input={"question": "Size?", "summary": "Need size"})
    usage = SimpleNamespace(model_dump=lambda: dict(input_tokens=10, output_tokens=4,
        cache_read_input_tokens=0, cache_creation_input_tokens=5))
    async def create(**kwargs):
        return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)
    def sdk(**kwargs):
        options.append(kwargs)
        return SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(anthropic, "AsyncAnthropic", sdk)
    assert call(client("anthropic", observe=events.append,
                       before_request=(lambda p,m: None) if guarded else None))["question"] == "Size?"
    assert options[0]["max_retries"] == 0
    assert events[0]["usage"]["input_tokens"] == 15


@pytest.mark.parametrize("failure", [None, "preflight", "create", "cancel", "cleanup"])
def test_anthropic_final_payload_hook_order_and_cleanup(monkeypatch, failure):
    import anthropic
    order, prepared, created, options = [], [], [], []
    block = SimpleNamespace(type="tool_use", name="emit", input={"question": "Size?", "summary": "Need size"})
    async def create(**kwargs):
        order.append("create"); created.append(kwargs)
        if failure in ("create", "cleanup"): raise RuntimeError("original failure")
        if failure == "cancel": raise asyncio.CancelledError()
        return SimpleNamespace(content=[block], stop_reason="tool_use", usage=None)
    close = AsyncMock(side_effect=OSError("cleanup failure") if failure == "cleanup" else None)
    def sdk(**kwargs):
        options.append(kwargs)
        return SimpleNamespace(messages=SimpleNamespace(create=create), close=close)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", sdk)
    async def prepare(sdk_client, payload):
        order.append("prepare"); prepared.append(payload.copy())
        if failure == "preflight": raise RuntimeError("original failure")
    def admit(provider, model):
        order.append("admit")
        assert provider == "anthropic"
    c = client("anthropic", before_request=admit, before_anthropic_request=prepare)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError): call(c)
    elif failure:
        with pytest.raises(RuntimeError, match="original failure"): call(c)
    else: assert call(c)["question"] == "Size?"
    assert order == (["prepare"] if failure == "preflight" else ["prepare", "admit", "create"])
    if created: assert prepared == created
    assert "input_schema" in prepared[0]["tools"][0]
    assert options[0]["max_retries"] == 0
    close.assert_awaited_once()


@pytest.mark.parametrize("union", ["oneOf", "anyOf", "allOf"])
@pytest.mark.parametrize("invalid", [None, "missing", "extra", "nonobject"])
def test_anthropic_union_tool_root_and_result_unwrap(monkeypatch, union, invalid):
    import anthropic
    schema = {union: [{"$ref": "#/$defs/Result"}], "$defs": {"Result": {"type": "object"}}}
    original = {union: [{"$ref": "#/$defs/Result"}], "$defs": {"Result": {"type": "object"}}}
    result: dict = {"result": {"question": "Size?"}}
    if invalid == "missing": result = {"question": "Size?"}
    if invalid == "extra": result["extra"] = True
    if invalid == "nonobject": result = {"result": []}
    async def create(**kwargs):
        sent = kwargs["tools"][0]["input_schema"]
        assert sent["type"] == "object" and sent["required"] == ["result"]
        assert sent["additionalProperties"] is False and union not in sent
        assert sent["$defs"] == schema["$defs"]
        assert sent["properties"]["result"] == {union: [{"$ref": "#/$defs/Result"}]}
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="emit", input=result)],
            stop_reason="tool_use", usage=None)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: SimpleNamespace(
        messages=SimpleNamespace(create=create), close=AsyncMock()))
    c = client("anthropic", before_anthropic_request=AsyncMock())
    async def run(): return await c._anthropic("fixture", "system", "user", schema, [], [], 8000, "medium", None)
    if invalid:
        with pytest.raises(AIError, match="envelope"): asyncio.run(run())
    else:
        assert asyncio.run(run()) == {"question": "Size?"}
    assert schema == original
