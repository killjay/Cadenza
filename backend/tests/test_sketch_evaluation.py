import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from cadenza_backend import sketch_evaluation as evaluation
from cadenza_backend.agents.model_client import ModelClient, AIError
from cadenza_backend.geometry_bridge import BuildOutcome
from cadenza_backend.sketch_intents import compile_intent
from cadenza_backend.agents.sketch_editor import SketchIntent

CORPUS = json.loads(evaluation.CORPUS_PATH.read_text())
CASES = {c["id"]: c for c in CORPUS["cases"]}


def evaluate(name, response=None, kernel=False):
    case = CASES[name]
    responses = [response if response is not None else case["response"]]
    if "followup_response" in case: responses.append(case["followup_response"])
    return asyncio.run(evaluation.evaluate_case(CORPUS["contexts"][case["context"]], case,
                                                evaluation.FixtureClient(responses), kernel=kernel))


def test_corpus_partition_and_context_expectations():
    assert len(CASES) == 60
    assert sum(c["split"] == "holdout" for c in CASES.values()) == 15
    assert all(not c["reviewed"] for c in CASES.values())
    assert {c["outcome"] for c in CASES.values()} == {"proposal", "clarification", "unsupported", "reject"}
    async def check():
        for context in CORPUS["contexts"].values():
            session = await evaluation.make_session(context)
            assert session.draft is not None
            if session.draft.state.solution:
                actual = evaluation.project_state(session.draft, session.draft.state, context["reference"])
                assert evaluation.equal(actual, context["reference"])
    asyncio.run(check())


def test_equivalent_driver_action_is_accepted():
    result = evaluate("resize", {"summary": "Equivalent edit", "actions": [
        {"kind": "driver", "parameter": "plate_width", "value": 140}]})
    assert result["passed"] and result["accepted_unchanged"] and result["draft_unchanged"]


@pytest.mark.parametrize("response", [
    {"summary": "Wrong size", "actions": [{"kind": "dimension", "entity": "ent_plate", "parameter": "width", "value": 130}]},
    {"summary": "Wrong entity", "actions": [{"kind": "dimension", "entity": "ent_bore", "parameter": "diameter", "value": 14}]},
    {"summary": "Unrelated extra change", "actions": [
        {"kind": "dimension", "entity": "ent_plate", "parameter": "width", "value": 140},
        {"kind": "dimension", "entity": "ent_bore", "parameter": "diameter", "value": 14}]},
])
def test_plausible_wrong_proposals_fail(response):
    result = evaluate("resize", response)
    assert not result["passed"] and result["outcome"] == "incorrect_proposal"
    if len(response["actions"]) == 2:
        assert "/entities/ent_bore" in result["unrelated_changes"]


def test_created_id_and_action_key_are_not_golden():
    response = copy.deepcopy(CASES["add"]["response"])
    response["actions"][0]["key"] = "another_key"
    assert evaluate("add", response)["passed"]
    response["actions"][0]["entity"]["diameter"] = 8
    assert not evaluate("add", response)["passed"]


def test_clarification_and_unsupported_never_accept_candidate():
    response = CASES["resize"]["response"]
    for name in ("ambiguous_size", "unsupported_curve"):
        result = evaluate(name, response)
        assert not result["passed"] and result["outcome"] == "unexpected_executable_proposal"
    result = evaluate("clarification_0")
    assert result["passed"] and result["turns"] == 2 and result["outcome"] == "resolved_clarification"
    assert evaluate("ambiguous_size")["question_review"] == "pending"


def test_schema_failure_is_not_successful_invalid_request_rejection():
    result = evaluate("invalid_region", {"unexpected": "output"})
    assert not result["passed"] and result["outcome"] == "schema_failure"
    assert evaluate("invalid_region")["outcome"] == "compiler_rejection"


def test_provider_failure_is_separate_and_preserves_state():
    class Failed(evaluation.FixtureClient):
        async def complete_json(self, *args, **kwargs): raise AIError("private provider payload")
    case = CASES["resize"]
    result = asyncio.run(evaluation.evaluate_case(CORPUS["contexts"]["plate"], case, Failed([]), kernel=False))
    assert not result["passed"] and result["outcome"] == "provider_or_schema_failure"
    assert result["accepted_unchanged"] and "private provider payload" not in json.dumps(result)


@pytest.mark.parametrize("built", [
    BuildOutcome(ok=False, stub=False, detail="fixture failure"),
    BuildOutcome(ok=True, stub=True, detail="stub"),
    BuildOutcome(ok=True, stub=False, detail="wrong volume", step=b"step", glb=b"glb", volume_mm3=1),
])
def test_kernel_failure_or_wrong_solid_cannot_pass(monkeypatch, built):
    async def build(*args): return built
    monkeypatch.setattr(evaluation, "build_design", build)
    result = evaluate("resize", kernel=True)
    assert not result["passed"] and result["outcome"] in ("geometry_failure", "geometry_mismatch")
    assert result["accepted_unchanged"]


def test_checker_catches_relationship_and_identity_corruption():
    async def setup():
        session = await evaluation.make_session(CORPUS["contexts"]["plate"])
        assert session.draft is not None
        candidate, _ = compile_intent(session.draft, SketchIntent.model_validate(CASES["resize"]["response"]))
        return session.draft, candidate
    draft, candidate = asyncio.run(setup())
    expected = evaluation.expected_state(CORPUS["contexts"]["plate"], CASES["resize"])
    candidate.relationships.bindings.pop()
    differences, _ = evaluation.check_candidate(draft, candidate, expected)
    assert "bindings" in differences
    draft, candidate = asyncio.run(setup())
    candidate.definition.constraints[-1].id = "con_replaced"
    differences, _ = evaluation.check_candidate(draft, candidate, expected)
    assert "constraint_identity" in differences


def key_data(**updates):
    return dict(limit=1, limit_remaining=.8, limit_reset=None, include_byok_in_limit=True, **updates)


@pytest.mark.parametrize("field,value", [
    ("limit", None), ("limit", 5), ("limit", float("nan")), ("limit_remaining", 0),
    ("limit_remaining", 2), ("limit_reset", "daily"), ("include_byok_in_limit", False),
])
def test_budget_verification_fails_closed(field, value):
    budget = evaluation.Budget("openrouter", "fixture", 2, 1)
    data = key_data(); data[field] = value
    with pytest.raises(evaluation.EvaluationLimit): budget.verify(data)
    with pytest.raises(evaluation.EvaluationLimit): budget.admit("openrouter", "fixture")


def test_budget_counts_actual_attempts_and_blocks_retry_or_routing():
    budget = evaluation.Budget("openrouter", "fixture", 1, 1)
    with pytest.raises(evaluation.EvaluationLimit): budget.admit("openrouter", "fixture")
    budget.verify(key_data())
    with pytest.raises(evaluation.EvaluationLimit): budget.admit("anthropic", "fixture")
    budget.admit("openrouter", "fixture")
    with pytest.raises(evaluation.EvaluationLimit): budget.admit("openrouter", "fixture")
    budget.verify(key_data())
    with pytest.raises(evaluation.EvaluationLimit): budget.admit("openrouter", "fixture")
    assert budget.calls == 1


@pytest.mark.parametrize("provider,model,calls,cap", [
    ("anthropic","fixture",1,1), ("openrouter","",1,1), ("openrouter","fixture",0,1),
    ("openrouter","fixture",1,float("inf")), ("openrouter","fixture",1,-1),
])
def test_invalid_live_configuration(provider, model, calls, cap):
    with pytest.raises(ValueError): evaluation.Budget(provider, model, calls, cap)


def test_refresh_verifies_only_official_endpoint(monkeypatch):
    original = httpx.AsyncClient
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": key_data()})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    client = ModelClient()
    client.s = client.s.model_copy(update={"openrouter_api_key": "fixture",
                                           "openrouter_base_url": "https://openrouter.ai/api/v1"})
    budget = evaluation.Budget("openrouter", "fixture", 1, 1)
    asyncio.run(budget.refresh(client))
    assert len(requests) == 1 and requests[0].url.path == "/api/v1/key"
    client.s = client.s.model_copy(update={"openrouter_base_url": "https://example.invalid"})
    with pytest.raises(evaluation.EvaluationLimit): asyncio.run(budget.refresh(client))
    assert len(requests) == 1


def test_offline_cli_run_has_no_network_and_missing_usage_is_unknown(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs): pytest.fail("Offline mode called network")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    args = SimpleNamespace(corpus=evaluation.CORPUS_PATH, split="holdout", live=False, repeat=1, skip_kernel=True)
    report = asyncio.run(evaluation.run(args))
    assert report["summary"]["passed"] == 15 and report["summary"]["network_calls"] == 0
    assert report["summary"]["interpretation_quality"] == "unmeasured"
    assert report["summary"]["usage"]["input_tokens"]["reported_total"] is None
    evaluation.write_report(tmp_path, report)
    assert (tmp_path / "results.json").exists() and (tmp_path / "results.md").exists()


def test_live_runner_uses_real_client_but_mock_network_and_stops_at_cap(monkeypatch, tmp_path):
    from cadenza_backend.agents import model_client
    settings = model_client.get_settings().model_copy(update={"openrouter_api_key": "fixture",
        "openrouter_base_url": "https://openrouter.ai/api/v1"})
    monkeypatch.setattr(model_client, "get_settings", lambda: settings)
    corpus = copy.deepcopy(CORPUS); corpus["cases"] = [CASES["resize"]]
    path = tmp_path / "cases.json"; path.write_text(json.dumps(corpus))
    requests = []
    original = httpx.AsyncClient
    def respond(request):
        requests.append(request)
        if request.method == "GET": return httpx.Response(200, json={"data": key_data()})
        body = json.loads(request.content)
        assert body["model"] == "fixture-model"
        assert "followup_response" not in str(body) and "label_status" not in str(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(CASES["resize"]["response"])}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 50, "cost": .001}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    args = SimpleNamespace(corpus=path, split="all", live=True, provider="openrouter", model="fixture-model",
        max_calls=1, cap_usd=1, repeat=2, skip_kernel=True)
    report = asyncio.run(evaluation.run(args))
    assert [r["outcome"] for r in report["results"]] == ["correct_first_attempt", "budget_or_call_limit"]
    assert [r.method for r in requests] == ["GET", "POST"]
    assert report["budget"]["admitted_calls"] == report["summary"]["network_calls"] == 1
    assert report["summary"]["usage"]["input_tokens"]["reported_total"] == 200
    assert "fixture" not in json.dumps(report["calls"][0]["usage"])


def test_refreshed_invalid_cap_revokes_admission():
    budget = evaluation.Budget("openrouter", "fixture", 2, 1)
    budget.verify(key_data())
    with pytest.raises(evaluation.EvaluationLimit): budget.verify({})
    with pytest.raises(evaluation.EvaluationLimit): budget.admit("openrouter", "fixture")


def test_missing_call_usage_is_not_a_complete_total():
    result = evaluate("resize")
    summary = evaluation.report_summary([result], [
        {"duration_ms": 1, "usage": {"input_tokens": 10, "reported_cost_usd": .1}},
        {"duration_ms": 2, "usage": None}], "live")
    assert summary["usage"]["input_tokens"] == {"reported_total": 10, "missing_calls": 1}
    assert summary["usage"]["output_tokens"] == {"reported_total": None, "missing_calls": 2}
    assert summary["provider_latency_ms"]["sample_count"] == 2


@pytest.mark.parametrize("mutation", ["draft", "history", "artifact"])
def test_evaluator_detects_state_mutation(monkeypatch, mutation):
    original = evaluation.handle_sketch_prompt
    async def corrupt(session, *args, **kwargs):
        await original(session, *args, **kwargs)
        if mutation == "draft": session.draft = None
        elif mutation == "history": session.history.append(session.snapshot())
        else: session.artifacts.put(session.revision, "step", "application/step", b"unaccepted")
    monkeypatch.setattr(evaluation, "handle_sketch_prompt", corrupt)
    result = evaluate("resize")
    assert not result["passed"] and result["outcome"] == "state_mutation"
