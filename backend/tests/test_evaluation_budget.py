import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cadenza_backend.evaluation_budget import AnthropicBudget, Budget, EvaluationLimit
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.sketch_evaluation import Budget as SketchBudget


def test_existing_sketch_import_is_compatible():
    assert SketchBudget is Budget


@pytest.mark.parametrize("failure", ["timeout", "revoked", "malformed", "missing", "exhausted"])
def test_failed_refresh_revokes_previous_permission(monkeypatch, failure):
    original = httpx.AsyncClient
    def respond(request):
        if failure == "timeout": raise httpx.ReadTimeout("private failure")
        if failure == "revoked": return httpx.Response(401)
        if failure == "malformed": return httpx.Response(200, text="not json")
        return httpx.Response(200, json={"data": {} if failure == "missing" else {
            "limit": 1, "limit_remaining": 0, "limit_reset": None, "include_byok_in_limit": True}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    budget = Budget("openrouter", "fixture", 2, 1)
    budget.verify(dict(limit=1, limit_remaining=1, limit_reset=None, include_byok_in_limit=True))
    client = ModelClient()
    client.s = client.s.model_copy(update=dict(openrouter_base_url="https://openrouter.ai/api/v1", openrouter_api_key="synthetic"))
    with pytest.raises(EvaluationLimit): asyncio.run(budget.refresh(client))
    with pytest.raises(EvaluationLimit): budget.admit("openrouter", "fixture")
    assert budget.calls == 0 and budget.verification_requests == 1


def test_route_guard_rejects_image_fallback_and_second_attempt():
    budget = Budget("openrouter", "vision", 2, 1)
    budget.verify(dict(limit=1, limit_remaining=1, limit_reset=None, include_byok_in_limit=True))
    with pytest.raises(EvaluationLimit): budget.admit("anthropic", "fallback")
    budget.admit("openrouter", "vision")
    with pytest.raises(EvaluationLimit): budget.admit("openrouter", "vision")
    assert budget.calls == 1


def anthropic_budget(**updates):
    args = dict(model="claude-sonnet-5-5", max_calls=2, cap_usd=1, workspace_id="wrkspc_synthetic",
        workspace_cap_usd=6, workspace_usage_usd=0, workspace_month=datetime.now(timezone.utc).strftime("%Y-%m"), workspace_confirmed=True)
    return AnthropicBudget(**{**args, **updates})


def payload(model="claude-sonnet-5-5") -> dict:
    return dict(model=model, max_tokens=8000, system=[dict(type="text", text="system", cache_control=dict(type="ephemeral"))],
        messages=[dict(role="user", content="synthetic")], output_config=dict(effort="medium"),
        tools=[dict(name="emit", description="output", input_schema={})], tool_choice=dict(type="auto"))


def sdk(tokens=1000):
    return SimpleNamespace(base_url="https://api.anthropic.com/", messages=SimpleNamespace(
        count_tokens=AsyncMock(return_value=SimpleNamespace(input_tokens=tokens))))


def ready(budget):
    c = ModelClient()
    c.s = c.s.model_copy(update=dict(anthropic_api_key="synthetic"))
    asyncio.run(budget.refresh(c))


@pytest.mark.parametrize("updates", [dict(workspace_confirmed=False), dict(workspace_id="bad"),
    dict(workspace_month="2000-01"), dict(workspace_cap_usd=7), dict(cap_usd=float("nan")),
    dict(cap_usd=True), dict(workspace_usage_usd=6), dict(model="unknown"), dict(max_calls=23), dict(max_calls=True)])
def test_anthropic_invalid_attestation_or_limits(updates):
    with pytest.raises(ValueError): anthropic_budget(**updates)


@pytest.mark.parametrize("model,reserved", [("claude-sonnet-5-5", "0.08403"), ("claude-opus-5-5", "0.16806")])
def test_anthropic_exact_count_payload_and_reservation(model, reserved):
    budget = anthropic_budget(model=model)
    ready(budget)
    client, request = sdk(), payload(model)
    asyncio.run(budget.prepare(client, request))
    counted = client.messages.count_tokens.call_args.kwargs
    assert counted == {**{k: v for k, v in request.items() if k != "max_tokens"}, "timeout": 15}
    assert budget.calls == 0 and budget.reserved == 0
    budget.admit("anthropic", model)
    assert budget.reserved == Decimal(reserved)
    with pytest.raises(EvaluationLimit): budget.admit("anthropic", model)
    with pytest.raises(EvaluationLimit): asyncio.run(budget.prepare(client, request))
    assert budget.token_count_requests == budget.calls == 1


@pytest.mark.parametrize("tokens", [None, -1, True, float("nan"), 1.5, 1_000_001])
def test_anthropic_bad_count_cannot_admit(tokens):
    budget = anthropic_budget(); ready(budget)
    with pytest.raises(EvaluationLimit): asyncio.run(budget.prepare(sdk(tokens), payload()))
    with pytest.raises(EvaluationLimit): budget.admit("anthropic", budget.model)
    assert budget.calls == 0


@pytest.mark.parametrize("failure", ["endpoint", "timeout", "cap", "model", "fee", "cache", "output"])
def test_anthropic_preflight_fails_closed(failure):
    budget = anthropic_budget(cap_usd=.01 if failure == "cap" else 1); ready(budget)
    client, request = sdk(), payload()
    if failure == "endpoint": client.base_url = "https://example.invalid/"
    if failure == "timeout": client.messages.count_tokens.side_effect = TimeoutError("private")
    if failure == "model": request["model"] = "other"
    if failure == "fee": request["service_tier"] = "priority"
    if failure == "cache": request["system"][0]["cache_control"]["ttl"] = "1h"
    if failure == "output": request["max_tokens"] = 9000
    with pytest.raises(EvaluationLimit): asyncio.run(budget.prepare(client, request))
    with pytest.raises(EvaluationLimit): budget.admit("anthropic", budget.model)
    assert budget.calls == 0
    if failure not in ("timeout", "cap"): assert client.messages.count_tokens.call_count == 0


def test_anthropic_usage_accounting_and_no_refund():
    budget = anthropic_budget(); ready(budget)
    asyncio.run(budget.prepare(sdk(), payload())); budget.admit("anthropic", budget.model)
    attempt: dict = dict(outcome="response", usage=dict(input_tokens=1000, output_tokens=100, cache_write_tokens=200, cache_read_tokens=300))
    budget.observe(attempt)
    assert Decimal(attempt["estimated_cost_usd"]) == Decimal(".00253")
    assert budget.reserved == Decimal(".08403")
    ready(budget)
    asyncio.run(budget.prepare(sdk(), payload())); budget.admit("anthropic", budget.model)
    budget.observe(dict(outcome="interrupted_or_failed", usage=None))
    assert budget.summary()["estimated_cost_usd"] is None
    assert budget.reserved == Decimal(".16806") and budget.stop_reason == "provider_failure"
    with pytest.raises(EvaluationLimit): ready(budget)


def test_anthropic_usage_exceeding_reservation_stops_run():
    budget = anthropic_budget(); ready(budget)
    asyncio.run(budget.prepare(sdk(), payload())); budget.admit("anthropic", budget.model)
    with pytest.raises(EvaluationLimit):
        budget.observe(dict(outcome="response", usage=dict(input_tokens=1_000_000, output_tokens=100, cache_write_tokens=0, cache_read_tokens=0)))
    assert budget.stop_reason == "observed_usage_exceeds_reservation"


def shared_budget(**updates):
    args = dict(allow_shared_key=True, workspace_id=None, workspace_cap_usd=None,
        workspace_usage_usd=None, workspace_month=None, workspace_confirmed=False, max_calls=1, cap_usd=.25)
    return anthropic_budget(**{**args, **updates})


@pytest.mark.parametrize("updates", [dict(allow_shared_key=False), dict(allow_shared_key=1),
    dict(workspace_confirmed=True), dict(workspace_id="wrkspc_synthetic"), dict(workspace_cap_usd=6),
    dict(workspace_usage_usd=0), dict(workspace_month="2026-10"), dict(cap_usd=7),
    dict(cap_usd=0), dict(cap_usd=True), dict(cap_usd=float("nan")), dict(max_calls=23), dict(model="unknown")])
def test_shared_key_requires_explicit_valid_limits_without_workspace_claims(updates):
    with pytest.raises(ValueError): shared_budget(**updates)


@pytest.mark.parametrize("failure", [None, "cap", "provider"])
def test_shared_key_keeps_reservations_and_attempt_guards(failure):
    budget = shared_budget(cap_usd=.01 if failure == "cap" else .25)
    ready(budget)
    if failure == "cap":
        with pytest.raises(EvaluationLimit): asyncio.run(budget.prepare(sdk(), payload()))
        with pytest.raises(EvaluationLimit): budget.admit("anthropic", budget.model)
        assert budget.calls == 0
    else:
        asyncio.run(budget.prepare(sdk(), payload())); budget.admit("anthropic", budget.model)
        budget.observe(dict(outcome="interrupted_or_failed" if failure else "response", usage=None))
        with pytest.raises(EvaluationLimit): ready(budget)
        assert budget.reserved == Decimal(".08403") and budget.calls == 1
    assert budget.summary()["workspace_cap"] == dict(method="shared_key_no_provider_cap",
        workspace_id=None, cap_usd=None, starting_usage_usd=None, month=None)
