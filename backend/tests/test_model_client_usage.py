import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest

from cadenza_backend.agents.model_client import ModelClient, AIError, AIProviderError, normalize_usage
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


@pytest.mark.parametrize("mode", ["architect", "librarian", "assembler", "draftsman", "machinist",
    "feature_editor", "new_part", "assembly_editor", "sketch_editor"])
def test_anthropic_all_advertised_schemas(monkeypatch, mode):
    import copy
    import anthropic
    from cadenza_backend.agents.architect import ArchitectOutput
    from cadenza_backend.agents.librarian import LibrarianOutput
    from cadenza_backend.agents.assembler import AssemblerOutput
    from cadenza_backend.agents.draftsman import DRAFTSMAN_OUTPUT_SCHEMA
    from cadenza_backend.agents.machinist import MACHINIST_OUTPUT_SCHEMA
    from cadenza_backend.agents.design_intents import CARD as FEATURE
    from cadenza_backend.agents.assembly_intents import CARD as ASSEMBLY
    from cadenza_backend.agents.core import AgentCard
    from cadenza_backend.tools import registry
    schemas = dict(architect=ArchitectOutput.model_json_schema(), librarian=LibrarianOutput.model_json_schema(),
        assembler=AssemblerOutput.model_json_schema(), draftsman=DRAFTSMAN_OUTPUT_SCHEMA,
        machinist=MACHINIST_OUTPUT_SCHEMA, feature_editor=FEATURE.output_schema,
        new_part=FEATURE.output_schema, assembly_editor=ASSEMBLY.output_schema, sketch_editor=CARD.output_schema)
    names = ["lookup_hardware_dimensions", "math_sandbox"] if mode in ("draftsman", "machinist") else []
    if mode == "machinist": names.append("geometry_probe")
    originals = copy.deepcopy([schemas[mode]] + [registry.get(n).schema for n in names])

    def check_references(value, root):
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].startswith("#/")
                target = root
                for key in value["$ref"][2:].split("/"):
                    target = target[key.replace("~1", "/").replace("~0", "~")]
            for item in value.values(): check_references(item, root)
        elif isinstance(value, list):
            for item in value: check_references(item, root)

    async def create(**kwargs):
        assert [t["name"] for t in kwargs["tools"]] == ["emit"] + names
        for tool in kwargs["tools"]:
            schema = tool["input_schema"]
            assert schema.get("type") == "object"
            assert not any(key in schema for key in ("oneOf", "anyOf", "allOf")), tool["name"]
            check_references(schema, schema)
        output = {"result": {}} if "result" in kwargs["tools"][0]["input_schema"].get("properties", {}) else {}
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="emit", input=output)],
            stop_reason="tool_use", usage=None)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: SimpleNamespace(messages=SimpleNamespace(create=create)))
    card = AgentCard(name=mode, system_prompt="offline", output_schema=schemas[mode], tools=names)
    assert asyncio.run(client("anthropic").complete_json(stage="machinist", agent_card=card, user="offline")) == {}
    assert [schemas[mode]] + [registry.get(n).schema for n in names] == originals


def tool_block(name, value, identifier="tool-call"):
    data = dict(type="tool_use", id=identifier, name=name, input=value)
    return SimpleNamespace(**data, model_dump=lambda: data)


@pytest.mark.parametrize("union", ["oneOf", "anyOf", "allOf"])
def test_anthropic_registered_union_preserves_definitions_and_unwraps(monkeypatch, union):
    import copy
    import anthropic
    schema = {"type": "object", union: [{"$ref": "#/$defs/Args"}], "$defs": {
        "Args": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}}}
    original = copy.deepcopy(schema)
    execute = AsyncMock(return_value={"result": 2})
    tool = SimpleNamespace(name="fixture", schema=dict(name="fixture", description="offline", input_schema=schema),
        execute_async=execute)
    count = 0
    async def create(**kwargs):
        nonlocal count
        count += 1
        sent = kwargs["tools"][1]["input_schema"]
        assert sent["$defs"] == original["$defs"] and union not in sent
        assert sent["properties"]["input"] == {k: v for k, v in original.items() if k != "$defs"}
        block = tool_block("fixture", {"input": {"value": 2}}) if count == 1 else tool_block("emit", {})
        assert count <= 2
        return SimpleNamespace(content=[block], stop_reason="tool_use", usage=None)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: SimpleNamespace(messages=SimpleNamespace(create=create)))
    assert asyncio.run(client("anthropic")._anthropic("fixture", "system", "user", {"type": "object"},
        [tool], [], 1000, None, {"marker": "context"})) == {}
    execute.assert_awaited_once_with(context={"marker": "context"}, value=2)
    assert schema == original


@pytest.mark.parametrize("case", ["valid", "missing", "extra", "nonobject", "invalid_count", "script"])
def test_anthropic_math_tool_round_trip(monkeypatch, case):
    import copy
    import json
    import anthropic
    from cadenza_backend.agents.core import AgentCard
    from cadenza_backend.tools import registry
    tool = registry.get("math_sandbox"); original = copy.deepcopy(tool.schema)
    execute = AsyncMock(wraps=tool.execute_async)
    monkeypatch.setattr(tool, "execute_async", execute)
    args: dict[str, Any] = dict(operation="bolt_circle", arguments=dict(radius=10, count=4))
    value: dict[str, Any] = {"input": args}
    if case == "missing": value = args
    if case == "extra": value["extra"] = True
    if case == "nonobject": value = {"input": []}
    if case == "invalid_count": args["arguments"]["count"] = 0
    if case == "script": args = dict(script="never execute"); value = {"input": args}
    calls = []
    async def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) == 1:
            sent = kwargs["tools"][1]["input_schema"]
            assert sent["properties"]["input"] == original["input_schema"]
            return SimpleNamespace(content=[tool_block(tool.name, value)], stop_reason="tool_use", usage=None)
        assert len(calls) == 2
        assert kwargs["messages"][-2]["content"][0]["input"] == value
        result = kwargs["messages"][-1]["content"][0]
        assert result["tool_use_id"] == "tool-call"
        data = json.loads(result["content"])
        if case == "valid":
            assert len(data["result"]) == 4
            assert data["result"][0] == dict(x=10, y=0)
        else: assert "error" in data
        return SimpleNamespace(content=[tool_block("emit", {"summary": "done"})], stop_reason="tool_use", usage=None)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: SimpleNamespace(messages=SimpleNamespace(create=create)))
    card = AgentCard(name="fixture", system_prompt="offline", output_schema={"type": "object"}, tools=[tool.name])
    assert asyncio.run(client("anthropic").complete_json(stage="machinist", agent_card=card, user="offline")) == {"summary": "done"}
    assert execute.await_count == (0 if case in ("missing", "extra", "nonobject") else 1)
    assert tool.schema == original


@pytest.mark.parametrize("provider", ["deepseek", "openrouter"])
def test_compatible_registered_tools_keep_original_schemas_and_round_trip(monkeypatch, provider):
    import copy
    import json
    from cadenza_backend.tools import registry
    from cadenza_backend.agents.core import AgentCard
    names = list(registry._tools)
    original = copy.deepcopy([registry.get(n).schema for n in names])
    bodies = []
    def respond(request):
        body = json.loads(request.content); bodies.append(body)
        for sent, expected in zip(body["tools"], original):
            assert sent["function"]["parameters"] == expected["input_schema"]
        if len(bodies) == 1:
            message = dict(role="assistant", content=None, tool_calls=[dict(id="math", type="function",
                function=dict(name="math_sandbox", arguments=json.dumps(dict(operation="gear_ratio",
                    arguments=dict(driving_teeth=20, driven_teeth=40)))))])
        else:
            assert len(bodies) == 2
            assert body["messages"][-1]["tool_call_id"] == "math"
            assert json.loads(body["messages"][-1]["content"]) == {"result": 2}
            message = dict(content='{"summary":"done"}')
        return httpx.Response(200, json={"choices": [{"message": message}]})
    install_http(monkeypatch, respond)
    card = AgentCard(name="fixture", system_prompt="offline", output_schema={"type": "object"}, tools=names)
    assert asyncio.run(client(provider).complete_json(stage="machinist", agent_card=card, user="offline")) == {"summary": "done"}
    assert [registry.get(n).schema for n in names] == original


@pytest.mark.parametrize("failure", ["connection", "timeout", 400, 401, 429, 500])
@pytest.mark.parametrize("provider", ["anthropic", "deepseek", "openrouter"])
def test_provider_failures_have_safe_classification_and_diagnostics(monkeypatch, provider, failure):
    import anthropic
    events = []
    request = httpx.Request("POST", "https://provider.invalid/?private=secret")
    response = httpx.Response(failure if isinstance(failure, int) else 500, request=request)
    if provider == "anthropic":
        # Response-shaped fixtures also work with SDK versions using httpx2 internally.
        if failure == "connection": exc = anthropic.APIConnectionError(request=cast(Any, request))
        elif failure == "timeout": exc = anthropic.APITimeoutError(request=cast(Any, request))
        else: exc = anthropic.APIStatusError("private response secret", response=cast(Any, response), body={"secret": "private"})
        async def create(**kwargs):
            raise exc from OSError("private socket detail")
        monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: SimpleNamespace(messages=SimpleNamespace(create=create)))
    else:
        def respond(req):
            if failure == "connection": raise httpx.ConnectError("private URL secret") from OSError("private socket detail")
            if failure == "timeout": raise httpx.ReadTimeout("private URL secret") from OSError("private socket detail")
            return httpx.Response(failure, text="private response secret")
        install_http(monkeypatch, respond)
    with pytest.raises(AIProviderError) as error:
        call(client(provider, observe=events.append))
    assert error.value.code.value == ("AGENT_TIMEOUT" if failure == "timeout" else "AGENT_UNAVAILABLE")
    assert "private" not in str(error.value) and "secret" not in str(error.value)
    assert len(events) == 1 and events[0]["error_type"]
    if isinstance(failure, int): assert events[0]["http_status"] == failure
    if failure in ("connection", "timeout"): assert events[0]["cause_types"] == ["OSError"]
    assert "private" not in str(events) and "secret" not in str(events)
