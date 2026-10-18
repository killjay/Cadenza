"""Independent oracle and isolation checks for the offline 3D evaluator."""
import asyncio
import copy
import json
from types import SimpleNamespace
from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from cadenza_backend import design_evaluation as evaluation
from cadenza_backend.agents.model_client import ModelClient


def anthropic_sdk(monkeypatch, response=None, failure=None, tokens=1000):
    import anthropic
    requests, counts, clients = [], [], []
    response = response or dict(kind="clarification", question="Which size?")
    async def count_tokens(**kwargs):
        counts.append(copy.deepcopy(kwargs))
        if failure == "count": raise TimeoutError("private count detail")
        return SimpleNamespace(input_tokens=tokens)
    async def create(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        if failure == "timeout": raise TimeoutError("private provider detail")
        if failure == "cancel": raise asyncio.CancelledError()
        if failure == "auth":
            # This is a response-shaped test double; SDK 1.0 uses httpx2 internally.
            raise anthropic.AuthenticationError("private provider detail", response=cast(Any, httpx.Response(401,
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))), body={})
        schema = kwargs["tools"][0]["input_schema"]
        for tool in kwargs["tools"]:
            assert not any(key in tool["input_schema"] for key in ("oneOf", "allOf", "anyOf")), "Provider rejects root schema unions"
        result = {"result": response} if "result" in schema.get("properties", {}) else response
        data = dict(type="tool_use", id="synthetic", name="unexpected" if failure == "extra" else "emit", input=result)
        block = SimpleNamespace(**data, model_dump=lambda: data)
        usage = dict(input_tokens=500, output_tokens=100, cache_read_input_tokens=300, cache_creation_input_tokens=200)
        return SimpleNamespace(content=[block], stop_reason="tool_use",
            usage=SimpleNamespace(model_dump=lambda: usage) if failure != "missing_usage" else None)
    def sdk(**options):
        assert options["api_key"] == "synthetic-anthropic" and options["max_retries"] == 0
        result = SimpleNamespace(base_url="https://api.anthropic.com/", messages=SimpleNamespace(
            count_tokens=count_tokens, create=create), close=AsyncMock())
        clients.append(result)
        return result
    monkeypatch.setattr(anthropic, "AsyncAnthropic", sdk)
    def factory(**kwargs):
        result = ModelClient(**kwargs)
        result.s = result.s.model_copy(update=dict(anthropic_api_key="synthetic-anthropic", deepseek_api_key="", openrouter_api_key=""))
        return result
    monkeypatch.setattr(evaluation, "ModelClient", factory)
    return requests, counts, clients


def anthropic_run(output, **changes):
    args = dict(provider="anthropic", model="claude-sonnet-5-5", max_calls=1, cap_usd=1,
        case_id="NP03", workspace_id="wrkspc_synthetic", workspace_cap_usd=6, workspace_usage_usd=0,
        workspace_month=datetime.now(timezone.utc).strftime("%Y-%m"), workspace_confirmed=True)
    return asyncio.run(evaluation.evaluate_live(output, **{**args, **changes}))


@pytest.mark.parametrize("wrong_name", [False, True])
def test_as09_explicit_names_native_cancel_and_strict_failure(tmp_path, monkeypatch, wrong_name):
    _, _, responses = evaluation.load()
    response = copy.deepcopy(responses["AS09/base"][0])
    if wrong_name:
        # Minimal offline reproduction of the historical occurrence-name mismatch.
        response["operations"][0]["instance_name"] = "New module"
    requests, counts, _ = anthropic_sdk(monkeypatch, response=response)
    output = tmp_path / "names"
    report = anthropic_run(output, case_id="AS09", observe_view=True)
    assert len(requests) == len(counts) == 1
    pilot = json.loads((evaluation.FIXTURES / "live_pilot.json").read_text(encoding="utf-8"))
    request = next(t for t in pilot["tasks"] if t["case_id"] == "AS09")["turns"][0]["request"]
    sent = json.loads(requests[0]["messages"][0]["content"][-1]["text"])
    assert sent["request"] == request["prompt"] and sent["target"] == request["target"]
    assert 'exact instance name "New module instance"' in sent["request"]
    assert "Preserve explicitly quoted names exactly" in requests[0]["system"][0]["text"]
    directory = output / "AS09/0-0"
    before = json.loads((directory / "context.json").read_text())
    accepted = json.loads((directory / "accepted.json").read_text())
    assert accepted["ledgers"] == before["ledgers"]
    candidate = json.loads((directory / "candidate.json").read_text())
    assert candidate["ledgers"] != accepted["ledgers"] and (directory / "candidate.step").exists()
    manifest = json.loads((output / "observer/manifest.json").read_text())
    decisions = [c for c in manifest["checkpoints"] if c["phase"] == "decision"]
    row = report["results"][0]
    if wrong_name:
        assert row["status"] == "failed" and row["classification"] == "candidate_or_geometry_mismatch"
        assert "New components identity" in row["steps"][0]["check_failure"]
        assert row["steps"][0]["state_preserved"] and not decisions
    else:
        # The evaluator checks complete state/per-solid geometry, Cancel state and duplicate decisions.
        assert row["status"] == "passed"
        assert row["steps"][0]["measurement"]["solids"] == 9
        assert len(decisions) == 1 and decisions[0]["detail"] == "Recorded action: cancel"
        assert decisions[0]["accepted"] == decisions[0]["before"]
        assert decisions[0]["proposal"] != decisions[0]["accepted"]


@pytest.mark.parametrize("case", ["AS07", "SP01"])
def test_scalar_assumptions_reach_provider_schema_and_native_checks(tmp_path, monkeypatch, case):
    if case == "AS07":
        response = json.loads((evaluation.FIXTURES.parent / "assembly_assumption_response.json").read_text(encoding="utf-8"))
        response["assumptions"][0]["value"] = "(35,0,0)"  # Explicit offline variant of captured metadata.
    else:
        response = dict(kind="proposal", description="Set height", operations=[dict(op="set_dimension",
            feature_id="feat_stock", parameter="height", value=10)], assumptions=[
                dict(field="units", value="mm", basis="Project units", confidence=1)])
    requests, counts, clients = anthropic_sdk(monkeypatch, response=response)
    report = anthropic_run(tmp_path / "offline-sdk", case_id=case)
    assert report["results"][0]["status"] == "passed"
    assert len(requests) == len(counts) == 1
    schema = requests[0]["tools"][0]["input_schema"]
    reference = schema["$defs"]["Proposal"]["properties"]["assumptions"]["items"]["$ref"].split("/")[-1]
    scalar = schema["$defs"][reference]["properties"]["value"]["anyOf"]
    assert {item["type"] for item in scalar} == {"string", "integer", "number", "boolean", "null"}
    assert counts[0]["tools"] == requests[0]["tools"]
    assert report["results"][0]["steps"][0]["model_response"]["assumptions"] == response["assumptions"]
    clients[0].close.assert_awaited_once()


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5"])
def test_anthropic_live_capture_and_accounting(monkeypatch, tmp_path, model):
    requests, counts, clients = anthropic_sdk(monkeypatch)
    output = tmp_path / "live"
    report = anthropic_run(output, model=model, case_id="IMG03")
    row = report["results"][0]
    assert row["status"] == "passed" and report["provider_cost"] is None
    assert report["budget"]["admitted_calls"] == report["budget"]["token_count_requests"] == 1
    assert report["budget"]["verification_requests"] == 0
    assert report["budget"]["workspace_cap"]["method"] == "user_confirmed_not_provider_verified"
    assert report["budget"]["estimated_cost_usd"] is not None
    assert requests[0]["output_config"] == {"effort": "medium"}
    assert counts[0] == {**{k: v for k, v in requests[0].items() if k != "max_tokens"}, "timeout": 15}
    assert "oracle_step" not in json.dumps(requests) and "expected" not in json.dumps(requests)
    evidence = row["steps"][0]["evidence"]
    assert set(evidence) >= {"request.json", "response.json", "context.json"}
    saved = json.loads((output / evidence["request.json"]).read_text())
    assert saved["request"]["tools"][0]["input_schema"] == requests[0]["tools"][0]["input_schema"]
    assert "sha256_base64" in json.dumps(saved)
    assert "synthetic-anthropic" not in json.dumps(saved) + json.dumps(report)
    clients[0].close.assert_awaited_once()


@pytest.mark.parametrize("failure", ["count", "timeout", "auth", "extra", "cancel"])
def test_anthropic_live_failures_stop_and_keep_evidence(monkeypatch, tmp_path, failure):
    requests, counts, clients = anthropic_sdk(monkeypatch, failure=failure)
    output = tmp_path / "live"
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError): anthropic_run(output, case_id=None, max_calls=18, cap_usd=3)
    else: anthropic_run(output, case_id=None, max_calls=18, cap_usd=3)
    report = json.loads((output / "results.json").read_text())
    row = report["results"][0]
    assert row["classification"] == ("interrupted" if failure == "cancel" else "budget_stop")
    assert all(r["status"] == "unattempted" for r in report["results"][1:])
    assert len(requests) == (0 if failure == "count" else 1)
    assert len(counts) == 1
    if failure == "auth":
        assert report["attempts"][0]["error_type"] == "AuthenticationError"
        assert report["attempts"][0]["http_status"] == 401
    if failure == "timeout": assert report["attempts"][0]["error_type"] == "TimeoutError"
    if failure == "count": assert report["budget"]["counts"][0]["error_type"] == "TimeoutError"
    if failure == "extra":
        evidence = row["steps"][0]["evidence"]
        assert "extra-request.json" in evidence and "request.json" in evidence
        assert len(json.loads((output / evidence["request.json"]).read_text())["request"]["messages"]) == 1
    assert "private provider detail" not in json.dumps(report)
    clients[0].close.assert_awaited_once()


def test_anthropic_live_missing_usage_remains_unknown(monkeypatch, tmp_path):
    anthropic_sdk(monkeypatch, failure="missing_usage")
    report = anthropic_run(tmp_path / "live")
    assert report["results"][0]["status"] == "passed"
    assert report["budget"]["estimated_cost_usd"] is None
    assert float(report["budget"]["reserved_usd"]) > 0


def test_anthropic_live_geometry_and_budget_exhaustion(monkeypatch, tmp_path):
    response = dict(kind="proposal", description="Change height", assumptions=[], operations=[
        dict(op="set_dimension", feature_id="feat_stock", parameter="height", value=10)])
    requests, _, _ = anthropic_sdk(monkeypatch, response=response)
    report = anthropic_run(tmp_path / "good", case_id="SP01")
    assert report["results"][0]["status"] == "passed"
    assert (tmp_path / "good/SP01/0-0/candidate.step").exists()
    candidate = json.loads((tmp_path / "good/SP01/0-0/candidate.json").read_text())
    accepted = json.loads((tmp_path / "good/SP01/0-0/accepted.json").read_text())
    assert candidate["ledgers"] == accepted["ledgers"] and accepted["requested_decision"] == "apply"
    report = anthropic_run(tmp_path / "blocked", case_id="SP01", cap_usd=.01)
    assert report["results"][0]["classification"] == "budget_stop"
    assert len(requests) == 1  # Exhausted run made no generation request.
    before = json.loads((tmp_path / "blocked/SP01/0-0/context.json").read_text())
    after = json.loads((tmp_path / "blocked/SP01/0-0/accepted.json").read_text())
    assert before["ledgers"] == after["ledgers"]


def test_failed_live_candidate_retained_before_oracle_rejection(monkeypatch, tmp_path):
    response = dict(kind="proposal", description="Change height", assumptions=[], operations=[
        dict(op="set_dimension", feature_id="feat_stock", parameter="height", value=10)])
    anthropic_sdk(monkeypatch, response=response)
    def reject_candidate(*args): raise AssertionError("Injected candidate check failure")
    monkeypatch.setattr(evaluation, "canonical", reject_candidate)
    output = tmp_path / "live"
    report = anthropic_run(output, case_id="SP01")
    assert report["results"][0]["classification"] == "candidate_or_geometry_mismatch"
    assert (output / "SP01/0-0/candidate.step").exists()
    candidate = json.loads((output / "SP01/0-0/candidate.json").read_text())
    before = json.loads((output / "SP01/0-0/context.json").read_text())
    after = json.loads((output / "SP01/0-0/accepted.json").read_text())
    assert candidate["ledgers"] != before["ledgers"] == after["ledgers"]


def test_anthropic_evidence_redaction_and_invalid_json(tmp_path):
    logger = evaluation.EvaluationLogger(); logger.directory = tmp_path
    logger.log("evaluation.request", api_key="never-record", headers={"Authorization": "never-record"}, schema={"type": "object"})
    assert "never-record" not in (tmp_path / "request.json").read_text()
    logger.log("llm_response", content=[dict(type="thinking", thinking="hidden"), dict(value=float("nan"))])
    assert json.loads((tmp_path / "response.json").read_text())["omitted"]


def test_anthropic_attestation_required_before_client(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "ModelClient", lambda **kw: pytest.fail("Client constructed before attestation"))
    with pytest.raises(ValueError): anthropic_run(tmp_path / "live", workspace_confirmed=False)


@pytest.mark.parametrize("failure", [None, "count", "timeout"])
def test_shared_anthropic_capture_and_failures(monkeypatch, tmp_path, failure):
    requests, counts, clients = anthropic_sdk(monkeypatch, failure=failure)
    report = asyncio.run(evaluation.evaluate_live(tmp_path / "live", provider="anthropic",
        model="claude-sonnet-5-5", max_calls=1, cap_usd=.25, case_id="NP03", allow_shared_key=True))
    assert report["budget"]["workspace_cap"]["method"] == "shared_key_no_provider_cap"
    assert report["budget"]["workspace_cap"]["cap_usd"] is None
    assert len(counts) == 1 and len(requests) == (0 if failure == "count" else 1)
    assert report["results"][0]["status"] == ("failed" if failure else "passed")
    assert "request.json" in report["results"][0]["steps"][0]["evidence"]
    clients[0].close.assert_awaited_once()


def test_shared_key_option_rejected_for_other_provider_before_client(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "ModelClient", lambda **kw: pytest.fail("Unexpected client"))
    with pytest.raises(AssertionError, match="only to Anthropic"):
        asyncio.run(evaluation.evaluate_live(tmp_path / "live", provider="openrouter",
            model="fixture", max_calls=1, cap_usd=.25, case_id="NP03", allow_shared_key=True))
    assert not (tmp_path / "live").exists()


def test_shared_key_cli_requires_live_mode(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["evaluation", "--allow-shared-key", "--output", str(tmp_path / "live")])
    assert evaluation.main() == 1
    assert not (tmp_path / "live").exists()


def live_http(monkeypatch, response=None, failure=None):
    original = httpx.AsyncClient
    requests = []
    def respond(request):
        requests.append(request)
        if request.method == "GET":
            if failure == "cap": return httpx.Response(401)
            return httpx.Response(200, json={"data": dict(limit=1, limit_remaining=1,
                limit_reset=None, include_byok_in_limit=True)})
        if failure == "timeout": raise httpx.ReadTimeout("private provider detail")
        if failure == "http": return httpx.Response(429, text="private provider detail")
        content = "not json" if failure == "json" else json.dumps(response or dict(kind="clarification", question="Which size?"))
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    def factory(**kwargs):
        client = ModelClient(**kwargs)
        client.s = client.s.model_copy(update=dict(openrouter_api_key="synthetic-only",
            openrouter_base_url="https://openrouter.ai/api/v1", anthropic_api_key="", deepseek_api_key=""))
        return client
    monkeypatch.setattr(evaluation, "ModelClient", factory)
    return requests


def live_run(output, case_id: str | None = "NP03", max_calls=1):
    return asyncio.run(evaluation.evaluate_live(output, provider="openrouter", model="fixture-model",
        max_calls=max_calls, cap_usd=1, case_id=case_id))


def test_live_manifest_counts_and_oracle_separation(monkeypatch, tmp_path):
    selected, _ = evaluation.load_pilot()
    assert len(selected) == 12
    assert sum(len(task.turns) for _, task in selected) == 16
    assert sum(bool(turn.followup) for _, task in selected for turn in task.turns) == 2
    requests = live_http(monkeypatch)
    original = evaluation.Path.read_text
    def read(path, *args, **kwargs):
        assert path.name != "responses.json", "Live evaluation loaded offline responses"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(evaluation.Path, "read_text", read)
    report = live_run(tmp_path / "live")
    assert report["results"][0]["status"] == "passed"
    assert report["provider_cost"] is None
    assert report["budget"]["admitted_calls"] == report["budget"]["verification_requests"] == 1
    assert [r.method for r in requests] == ["GET", "POST"]
    payload = json.loads(requests[-1].content)
    assert payload["model"] == "fixture-model"
    assert "oracle_step" not in str(payload) and "expected" not in str(payload)
    assert "synthetic-only" not in json.dumps(report)
    assert (tmp_path / "live/human_review.md").exists()


@pytest.mark.parametrize("failure,response,classification", [
    ("timeout", None, "provider_error"), ("http", None, "provider_error"),
    ("json", None, "malformed_response"),
    (None, dict(kind="proposal", operations=[]), "invalid_schema"),
    (None, dict(kind="unsupported", reason="Cannot do that"), "wrong_intent"),
])
def test_live_failure_accounting(monkeypatch, tmp_path, failure, response, classification):
    requests = live_http(monkeypatch, response, failure)
    report = live_run(tmp_path / "live")
    row = report["results"][0]
    assert row["status"] == "failed" and row["classification"] == classification
    assert row["steps"][0]["state_preserved"]
    assert report["budget"]["admitted_calls"] == 1
    assert len([r for r in requests if r.method == "POST"]) == len(report["attempts"]) == 1
    assert "private provider detail" not in json.dumps(report)


def test_live_cap_failure_stops_all_tasks(monkeypatch, tmp_path):
    requests = live_http(monkeypatch, failure="cap")
    report = live_run(tmp_path / "live", case_id=None, max_calls=18)
    assert report["run_status"] == "stopped"
    assert report["results"][0]["classification"] == "budget_stop"
    assert all(r["status"] == "unattempted" for r in report["results"][1:])
    assert report["budget"]["admitted_calls"] == 0
    assert [r.method for r in requests] == ["GET"]


def test_live_followup_is_bounded(monkeypatch, tmp_path):
    requests = live_http(monkeypatch)
    report = live_run(tmp_path / "live", case_id="NP01", max_calls=6)
    row = report["results"][0]
    assert row["classification"] == "wrong_intent"
    assert [s["status"] for s in row["steps"]] == ["clarification_followup", "failed"]
    assert row["unattempted_turns"] == [1, 2, 3, 4]
    assert len([r for r in requests if r.method == "POST"]) == 2


@pytest.mark.parametrize("interrupt", [False, True])
def test_live_partial_report_and_cleanup_on_safety_or_interruption(monkeypatch, tmp_path, interrupt):
    live_http(monkeypatch)
    closed = []
    monkeypatch.setattr(evaluation.runner, "close", lambda: closed.append(True))
    async def broken(session, *args, **kwargs):
        if interrupt: raise asyncio.CancelledError()
        session.primary_ledger_id = "unexpected"
        raise AssertionError("Unsafe mutation")
    monkeypatch.setattr(evaluation, "run_step", broken)
    output = tmp_path / "live"
    if interrupt:
        with pytest.raises(asyncio.CancelledError): live_run(output, case_id=None, max_calls=18)
    else: live_run(output, case_id=None, max_calls=18)
    report = json.loads((output / "results.json").read_text())
    assert report["results"][0]["classification"] == ("interrupted" if interrupt else "accepted_state_safety_failure")
    assert all(r["status"] == "unattempted" for r in report["results"][1:])
    assert closed == [True]


def test_live_image_bytes_reach_explicit_route(monkeypatch, tmp_path):
    requests = live_http(monkeypatch)
    report = live_run(tmp_path / "live", case_id="IMG03")
    assert report["results"][0]["status"] == "passed"
    payload = json.loads(requests[-1].content)
    assert payload["model"] == "fixture-model"
    assert "data:image/png;base64," in json.dumps(payload)


@pytest.mark.parametrize("invalid_parameter", [False, True])
def test_live_proposal_uses_real_checker_and_classifies_safe_rejection(monkeypatch, tmp_path, invalid_parameter):
    response = dict(kind="proposal", description="Change selected height", assumptions=[], operations=[
        dict(op="set_dimension", feature_id="feat_stock", parameter="diameter" if invalid_parameter else "height", value=10)])
    live_http(monkeypatch, response)
    report = live_run(tmp_path / "live", case_id="SP01")
    row = report["results"][0]
    if invalid_parameter:
        assert row["classification"] == "safe_application_rejection"
        assert row["steps"][0]["state_preserved"]
    else:
        assert row["status"] == "passed"
        assert (tmp_path / "live/SP01/0-0/candidate.step").exists()


def test_live_manifest_rejects_injected_oracle_fields():
    raw = json.loads((evaluation.FIXTURES / "live_pilot.json").read_text())
    raw["tasks"][0]["turns"][0]["expected"] = {"secret_oracle": True}
    with pytest.raises(ValidationError): evaluation.Pilot.model_validate(raw)


def test_live_nonfinite_response_still_writes_failure_report(monkeypatch, tmp_path):
    response = dict(kind="proposal", description="Invalid dimension", assumptions=[], operations=[
        dict(op="set_dimension", feature_id="feat_stock", parameter="height", value=float("nan"))])
    live_http(monkeypatch, response)
    output = tmp_path / "live"
    report = live_run(output, case_id="SP01")
    assert report["results"][0]["classification"] == "invalid_schema"
    assert json.loads((output / "results.json").read_text())["results"][0]["status"] == "failed"
    assert "NaN" not in (output / "results.json").read_text()


def test_frozen_corpus_counts_and_turn_bindings():
    corpus, contexts, responses = evaluation.load()
    assert len(corpus.cases) == 30
    assert len({c.id for c in corpus.cases}) == 24
    assert len({c.id for c in corpus.cases if c.split == "holdout"}) == 6
    assert len({c.id for c in corpus.cases if c.split == "development"}) == 18
    assert {s.route for c in corpus.cases for s in c.steps} == {"part", "source", "assembly"}
    assert sum(s.image is not None for c in corpus.cases for s in c.steps) == 4
    assert set(responses) == {f"{c.id}/{c.variant}" for c in corpus.cases}


@pytest.mark.parametrize("change", [dict(version=2), dict(extra=True), dict(cases=[])])
def test_invalid_corpus(change):
    raw = json.loads((evaluation.FIXTURES / "corpus.json").read_text())
    with pytest.raises(ValidationError): evaluation.Corpus.model_validate({**raw, **change})


def test_missing_geometry_oracle_rejected():
    with pytest.raises(ValidationError):
        evaluation.Step.model_validate(dict(route="part", request={}, outcome="proposal"))


@pytest.mark.parametrize("field,value", [("volume", 10.0), ("minimum", [1.0, 0.0, 0.0]),
    ("maximum", [2.0, 1.0, 1.0]), ("solids", 2)])
def test_geometry_checker_rejects_wrong_volume_position_extent_count(field, value):
    expected = evaluation.Geometry(volume=1.0, minimum=[0.0]*3, maximum=[1.0]*3, solids=1)
    measured = dict(volume=1.0, minimum=[0.0]*3, maximum=[1.0]*3, solids=1)
    measured[field] = value
    with pytest.raises(AssertionError): evaluation.check_geometry(measured, expected)


@pytest.mark.parametrize("left,right", [(True, 1), (float("nan"), float("nan")), ([1, 2], [2, 1]),
    ({"id": "old"}, {"id": "new"}), ({"a": 1}, {"a": 1, "b": 2})])
def test_equal_preserves_types_identity_order_and_keys(left, right):
    assert not evaluation.equal(left, right)


@pytest.mark.parametrize("corruption", ["wrong_instance", "axis", "source", "joint", "orphan", "count", "identity", "order"])
def test_complete_state_oracle_rejects_unrelated_changes(corruption):
    corpus, contexts, _ = evaluation.load()
    case = next(c for c in corpus.cases if c.id == "AS03" and c.variant == "base")
    expected = case.steps[0].expected
    assert expected is not None
    candidate = copy.deepcopy(expected)
    module = candidate["module"]["assembly"]
    if corruption == "wrong_instance": module["components"][3]["placement"]["origin"][0] += 1
    if corruption == "axis": module["components"][2]["placement"]["origin"][1] += 1
    if corruption == "source": candidate["source_0"]["features"][0]["parameters"]["height"] += 1
    if corruption == "joint": module["joints"][0]["component_b"] = "comp_c"
    if corruption == "orphan": candidate["orphan"] = copy.deepcopy(candidate["source_0"])
    if corruption == "count": module["components"].pop()
    if corruption == "identity": module["components"][0]["id"] = "comp_changed"
    if corruption == "order": module["components"].reverse()
    try:
        actual = evaluation.canonical(candidate, contexts[case.context]["ledgers"], expected)
    except AssertionError:
        return
    assert not evaluation.equal(actual, expected)


def test_new_identity_is_mapped_but_other_fields_are_not_masked():
    corpus, contexts, _ = evaluation.load()
    case = next(c for c in corpus.cases if c.id == "NP01")
    expected = case.steps[0].expected
    assert expected is not None
    candidate = copy.deepcopy(expected)
    candidate["actual"] = candidate.pop("created")
    candidate["actual"]["project_id"] = "actual"
    candidate["actual"]["features"][0]["id"] = "feat_actual"
    assert evaluation.equal(evaluation.canonical(candidate, contexts[case.context]["ledgers"], expected), expected)
    candidate["actual"]["features"][0]["parameters"]["height"] += 1
    assert not evaluation.equal(evaluation.canonical(candidate, contexts[case.context]["ledgers"], expected), expected)


def test_fixture_client_cannot_initialize_sdk_and_forbids_extra_calls(monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("Network/SDK forbidden")
    monkeypatch.setattr(ModelClient, "__init__", forbidden)
    monkeypatch.setattr(ModelClient, "complete_json", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-use")
    client = evaluation.FixtureClient(dict(kind="clarification", question="Which dimensions?"), "part", [])
    assert asyncio.run(client.complete_json(images=[]))["kind"] == "clarification"
    with pytest.raises(AssertionError): asyncio.run(client.complete_json(images=[]))


def test_images_must_arrive_as_exact_bytes():
    client = evaluation.FixtureClient(dict(kind="clarification", question="Which dimensions?"), "part", [("image/png", b"expected")])
    with pytest.raises(AssertionError): asyncio.run(client.complete_json(images=[("image/png", b"wrong")]))


def test_malformed_response_is_not_valid_rejection():
    client = evaluation.FixtureClient(dict(kind="proposal", operations=[]), "part", [])
    with pytest.raises(ValidationError): asyncio.run(client.complete_json(images=[]))
    assert not client.schema_valid


@pytest.mark.parametrize("kwargs", [dict(case_id="missing"), dict(repeat=0), dict(repeat=True), dict(split="invalid")])
def test_invalid_selection_does_not_create_output(tmp_path, kwargs):
    path = tmp_path / "run"
    with pytest.raises(AssertionError): asyncio.run(evaluation.evaluate(path, **kwargs))
    assert not path.exists()


def test_existing_output_never_overwritten(tmp_path):
    with pytest.raises(FileExistsError): asyncio.run(evaluation.evaluate(tmp_path, case_id="NP03"))


def test_real_clarification_route_offline_report_and_cleanup(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("Unexpected network/SDK")
    monkeypatch.setattr(ModelClient, "__init__", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    closed = []
    monkeypatch.setattr(evaluation.runner, "close", lambda: closed.append(True))
    report = asyncio.run(evaluation.evaluate(tmp_path / "run", case_id="IMG03"))
    assert report["results"][0]["status"] == "passed"
    assert report["provider_cost"] is None and report["live_accuracy"] == "unmeasured"
    assert report["results"][0]["steps"][0]["network_attempts"] == 0
    assert closed == [True]
    assert "Human review pending" in (tmp_path / "run/results.md").read_text()


def test_report_failure_still_closes_worker(tmp_path, monkeypatch):
    closed = []
    monkeypatch.setattr(evaluation.runner, "close", lambda: closed.append(True))
    def failed(*args): raise OSError("disk unavailable")
    monkeypatch.setattr(evaluation, "write_report", failed)
    with pytest.raises(OSError): asyncio.run(evaluation.evaluate(tmp_path / "run", case_id="NP03"))
    assert closed == [True]


def test_failed_step_stops_its_sequence_and_writes_failure(tmp_path, monkeypatch):
    failure = AsyncMock(side_effect=AssertionError("bad candidate"))
    monkeypatch.setattr(evaluation, "run_step", failure)
    report = asyncio.run(evaluation.evaluate(tmp_path / "run", case_id="NP01"))
    assert report["results"][0]["status"] == "failed"
    assert "bad candidate" in report["results"][0]["error"]
    assert (tmp_path / "run/results.json").is_file()
    assert failure.await_count == 3  # one failed first step per variant, never later steps


@pytest.mark.parametrize("corruption", ["stub", "accepted_state", "export", "apply"])
def test_runner_rejects_stub_mutation_export_and_apply_failure(tmp_path, monkeypatch, corruption):
    from cadenza_backend.geometry_bridge import BuildOutcome
    corpus, contexts, responses = evaluation.load()
    case = next(c for c in corpus.cases if c.id == "SP01")
    step = case.steps[0]
    assert step.expected is not None and step.geometry is not None
    context = contexts[case.context]
    session = evaluation.Session(primary_ledger_id=context["primary"], stores={k: evaluation.LedgerStore(evaluation.Ledger.model_validate(v)) for k, v in context["ledgers"].items()})
    session.logger = evaluation.QuietLogger()
    async def fake_handler(current, request, emit, *, client):
        assert step.expected is not None and step.geometry is not None
        await client.complete_json(images=[])
        current.preview = SimpleNamespace(id="preview", ledgers={k: evaluation.Ledger.model_validate(v) for k,v in step.expected.items()},
            outcome=BuildOutcome(ok=True, detail="unit fixture", stub=corruption == "stub", step=b"unit-test-only",
                volume_mm3=step.geometry.volume, bbox=dict(min=step.geometry.minimum,max=step.geometry.maximum)), display_outcome=None)
        if corruption == "accepted_state": current.store.ledger.metadata.name = "corrupted"
    monkeypatch.setattr(evaluation.design_prompts, "handle_design_prompt", fake_handler)
    measured = dict(volume=step.geometry.volume, minimum=step.geometry.minimum, maximum=step.geometry.maximum, solids=1)
    if corruption == "export": measured["minimum"] = [999, 0, 0]
    monkeypatch.setattr(evaluation, "inspect_step", lambda path: measured)
    monkeypatch.setattr(evaluation, "decide_preview", AsyncMock(side_effect=RuntimeError("Apply failed")))
    with pytest.raises((AssertionError, RuntimeError)):
        asyncio.run(evaluation.run_step(session, step, responses["SP01/base"][0], tmp_path, evaluation.FIXTURES))


@pytest.mark.parametrize("failure", ["precommit", "acknowledgement"])
def test_apply_failure_boundary_and_replay_without_double_accept(monkeypatch, failure):
    from cadenza_backend import design
    from cadenza_backend.geometry_bridge import BuildOutcome
    from cadenza_contracts.errors import CadenzaError
    from cadenza_contracts.messages import PreviewDecision
    corpus, contexts, _ = evaluation.load()
    step = next(c for c in corpus.cases if c.id == "SP01").steps[0]
    assert step.expected is not None
    context = contexts["plate"]
    session = evaluation.Session(primary_ledger_id="plate", stores={k: evaluation.LedgerStore(evaluation.Ledger.model_validate(v)) for k,v in context["ledgers"].items()})
    session.logger = evaluation.QuietLogger()
    before = evaluation.state(session)
    candidate = {k: evaluation.Ledger.model_validate(v) for k,v in step.expected.items()}
    outcome = BuildOutcome(ok=True, stub=False, detail="transaction unit fixture", step=b"unit-only")
    async def disconnected(_): raise ConnectionError("Lost acknowledgement")
    if failure == "precommit":
        def invalid(_): raise ValueError("Message preparation failed")
        monkeypatch.setattr(design, "dump_message", invalid)
        with pytest.raises(ValueError): asyncio.run(design.accept_design(session,candidate,outcome,"evaluation",evaluation.ignore))
        assert evaluation.state(session) == before
    else:
        with pytest.raises(ConnectionError): asyncio.run(design.accept_design(session,candidate,outcome,"evaluation",disconnected))
        assert session.revision == 1 and len(session.history) == 1
        accepted = evaluation.state(session)
        frames = []
        async def collect(frame): frames.append(frame)
        asyncio.run(design.replay_geometry(session,collect))
        assert len(frames) == 1 and frames[0].revision == 1
        assert evaluation.state(session) == accepted
        with pytest.raises(CadenzaError):
            asyncio.run(evaluation.decide_preview(session,PreviewDecision(message_id="duplicate",base_revision=0,preview_id="old",action="apply"),evaluation.ignore))
        assert evaluation.state(session) == accepted


def test_per_solid_oracle_catches_interior_swap_with_same_total_bounds():
    expected = evaluation.Geometry(volume=2.0, minimum=[0.0]*3, maximum=[3.0,1.0,1.0], solids=2,
        per_solid=[evaluation.Measure(volume=1.0, minimum=[0.0]*3, maximum=[1.0]*3),
                   evaluation.Measure(volume=1.0, minimum=[2.0,0.0,0.0], maximum=[3.0,1.0,1.0])])
    actual = expected.model_dump()
    actual["per_solid"][1]["minimum"][0] = 1.5
    with pytest.raises(AssertionError): evaluation.check_geometry(actual, expected)


def test_cli_live_mode_requires_explicit_route_and_limits(monkeypatch):
    monkeypatch.setattr("sys.argv", ["evaluation", "--live", "--output", "unused"])
    monkeypatch.setattr(ModelClient, "__init__", lambda *args, **kwargs: pytest.fail("Unapproved client construction"))
    assert evaluation.main() == 1


def test_stale_decision_preserves_accepted_state(monkeypatch):
    from cadenza_backend import edit_preview
    from cadenza_contracts.errors import CadenzaError
    from cadenza_contracts.messages import PreviewDecision
    session = evaluation.Session()
    session.logger = evaluation.QuietLogger()
    before = evaluation.state(session)
    monkeypatch.setattr(edit_preview, "pending", lambda current: SimpleNamespace(id="old", base=session.snapshot()))
    with pytest.raises(CadenzaError):
        asyncio.run(edit_preview.decide_preview(session, PreviewDecision(message_id="stale", base_revision=5, preview_id="old",action="apply"),evaluation.ignore))
    assert evaluation.state(session) == before


def test_step_inspection_timeout_is_bounded_and_propagated(monkeypatch, tmp_path):
    import subprocess
    def timeout(args, **kwargs):
        assert kwargs["timeout"] == 60 and kwargs["check"] is True
        raise subprocess.TimeoutExpired(args, 60)
    monkeypatch.setattr(evaluation.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired): evaluation.inspect_step(tmp_path / "candidate.step")


@pytest.mark.parametrize("field", ["x", "y", "radius", "z_min", "z_max"])
def test_step_bore_oracle_rejects_wrong_position_radius_or_depth(field):
    bore = dict(x=20.0, y=0.0, radius=3.0, z_min=0.0, z_max=8.0)
    expected = evaluation.Geometry(volume=100.0, minimum=[0.0]*3, maximum=[100.0]*3, solids=1, bores=[bore])
    actual = expected.model_dump()
    actual["bores"][0][field] += 1
    with pytest.raises(AssertionError): evaluation.check_geometry(actual, expected)


def test_keyboard_interrupt_preserves_partial_report_and_closes_runner(monkeypatch, tmp_path):
    closed = []
    monkeypatch.setattr(evaluation.runner, "close", lambda: closed.append(True))
    monkeypatch.setattr(evaluation, "run_step", AsyncMock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt): asyncio.run(evaluation.evaluate(tmp_path / "run", case_id="NP01"))
    report = json.loads((tmp_path / "run/results.json").read_text())
    assert len(report["results"]) == 1 and report["selected_executions"] == 3
    assert report["results"][0]["error"] == "Interrupted: KeyboardInterrupt"
    assert closed == [True]


def test_live_observation_adds_no_provider_calls(monkeypatch, tmp_path):
    requests, counts, _ = anthropic_sdk(monkeypatch)
    result = anthropic_run(tmp_path / "observed-live", observe_view=True)
    assert len(requests) == len(counts) == 1
    assert result["results"][0]["status"] == "passed"
    manifest = json.loads((tmp_path / "observed-live/observer/manifest.json").read_text())
    assert manifest["status"] == "completed" and manifest["estimated_usd"] > 0
    assert any(c["phase"] == "clarification" for c in manifest["checkpoints"])
