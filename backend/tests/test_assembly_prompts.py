"""Offline assembly interpretation: no provider calls and no accepted-state mutation."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from cadenza_backend import assembly_commands as commands, assembly_prompts as prompts
from cadenza_backend.agents import assembly_intents as intents
from cadenza_backend.edit_preview import verify_edit
from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.messages import AssemblyPrompt, parse_client_message
from backend.tests.test_assembly_commands import established, command, placement, discard, decide

CASES = json.loads((Path(__file__).parent / "fixtures/assembly_intents.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_offline_rejection_and_clarification_corpus(monkeypatch, case):
    session, (a, _) = established(); before = session.snapshot()
    provider = client(case["output"]); build = AsyncMock()
    monkeypatch.setattr(commands, "build_design", build)
    req = request(session, a.id).model_copy(update={"prompt": case["prompt"]})
    if case.get("invalid"):
        with pytest.raises(CadenzaError): asyncio.run(prompts.handle_assembly_prompt(session, req, discard, client=provider))
    else:
        asyncio.run(prompts.handle_assembly_prompt(session, req, discard, client=provider))
    provider.complete_json.assert_awaited_once(); build.assert_not_called()
    assert session.snapshot() == before and not session.history and not session.redo and session.preview is None


def request(session, component_id, **updates):
    return AssemblyPrompt.model_validate(dict(type="design.assembly.prompt", message_id="e3_test",
        base_revision=session.revision, ledger_id=session.primary_ledger_id, owner_path=[],
        scope="assembly_definition", target=dict(kind="instance", component_id=component_id),
        prompt="Set X to 20 mm", **updates))


def proposal(*operations):
    return dict(kind="proposal", description="Assembly edit", operations=list(operations), assumptions=[])


def client(raw):
    result = AsyncMock()
    result.complete_json.return_value = raw
    return result


def test_explicit_definition_child_and_instance_names_survive_preview_cancel():
    from cadenza_backend.design_evaluation import state
    session, _ = established(); before = state(session)
    text = ('Create definition "Unit definition" with Source 0 child "Unit child" at origin (0,0,0), '
        'rotation (0,0,0). Add instance "Unit instance" at (400,0,0), rotation (0,0,0).')
    req = AssemblyPrompt.model_validate(dict(message_id="names", base_revision=session.revision,
        ledger_id=session.primary_ledger_id, owner_path=[], scope="assembly_definition",
        target=dict(kind="definition"), prompt=text))
    provider = client(proposal(dict(op="create_subassembly", name="Unit definition",
        instance_name="Unit instance", placement=placement(400), children=[dict(
            source_ledger_id="source_0", name="Unit child", placement=placement())])))
    asyncio.run(prompts.handle_assembly_prompt(session, req, discard, client=provider))
    provider.complete_json.assert_awaited_once()
    sent = provider.complete_json.call_args.kwargs
    assert json.loads(sent["user"])["request"] == text
    assert "Preserve explicitly quoted names exactly" in sent["agent_card"].system_prompt
    preview = session.preview
    assert preview is not None and state(session) == before
    created = next(ledger for lid, ledger in preview.ledgers.items() if lid not in session.stores)
    assert created.metadata.name == "Unit definition" and created.assembly is not None
    assert created.assembly.components[0].name == "Unit child"
    owner = preview.ledgers[session.primary_ledger_id].assembly
    assert owner is not None
    assert next(c for c in owner.components if c.sub_assembly_id == created.project_id).name == "Unit instance"
    assert preview.message.prompt == text
    decide(session, "cancel")
    assert state(session) == before


@pytest.mark.parametrize("invalid", [False, True])
def test_assumptions_preview_apply_and_invalid_state_preservation(invalid):
    from cadenza_backend.design_evaluation import state
    session, (a, _) = established(); before = state(session)
    raw = proposal(dict(op="set_instance_placement", component_id=a.id, placement=placement(20)))
    metadata = dict(field="placement.origin", value=[20, 0, 0] if invalid else "(20,0,0)",
        basis="Owner frame", confidence=1)
    raw["assumptions"] = [metadata]
    provider = client(raw)
    if invalid:
        with pytest.raises(CadenzaError) as exc:
            asyncio.run(prompts.handle_assembly_prompt(session, request(session, a.id), discard, client=provider))
        assert exc.value.code.value == "AGENT_INVALID_OUTPUT"
        assert state(session) == before and session.preview is None
    else:
        asyncio.run(prompts.handle_assembly_prompt(session, request(session, a.id), discard, client=provider))
        preview = session.preview
        assert preview is not None and state(session) == before
        assert preview.message.assumptions[0].model_dump() == metadata
        candidate = preview.ledgers
        decide(session)
        assert session.snapshot() == candidate and session.revision == 1 and len(session.history) == 1
        assert preview.message.assumptions[0].model_dump() == metadata
    provider.complete_json.assert_awaited_once()


@pytest.mark.parametrize("updates", [dict(base_revision=True), dict(owner_path=["bad"]),
    dict(prompt=" "), dict(prompt="x" * 4001), dict(patch=[]), dict(scope="source"),
    dict(target=dict(kind="instance", component_id="comp_a", detach_joint_ids=["joint_a"] * 2)),
    dict(target=dict(kind="definition", component_id="comp_a")),
    dict(target=dict(kind="attachment", component_a="comp_a", component_b="comp_a"))])
def test_strict_wire_rejects_ambiguous_or_extra_fields(updates):
    session, (a, _) = established()
    raw = request(session, a.id).model_dump(mode="json")
    with pytest.raises(ValidationError):
        parse_client_message({**raw, **updates})


@pytest.mark.parametrize("kind", ["clarification", "unsupported"])
def test_nonproposal_is_one_call_and_preserves_state(monkeypatch, kind):
    session, (a, _) = established(); before = session.snapshot()
    output = dict(kind=kind, **({"question": "Which axis?"} if kind == "clarification" else {"reason": "Motion is unsupported."}))
    provider = client(output); emit = AsyncMock(); build = AsyncMock()
    monkeypatch.setattr(commands, "build_design", build)
    asyncio.run(prompts.handle_assembly_prompt(session, request(session, a.id), emit, client=provider))
    provider.complete_json.assert_awaited_once(); build.assert_not_called()
    assert session.snapshot() == before and session.preview is None and not session.history
    assert emit.call_args_list[1].args[0].type == "agent.message"


@pytest.mark.parametrize("operation", [dict(op="rename_instance", component_id="$other", name="Wrong"),
    dict(op="remove_joint", joint_id="joint_unapproved"),
    dict(op="set_instance_placement", component_id="$self", placement=placement(21)),
    dict(op="set_instance_placement", component_id="$self", placement=placement(20, [1, 0, 0]))])
def test_wrong_scope_or_numeric_value_rejected_before_kernel(monkeypatch, operation):
    session, (a, b) = established(); before = session.snapshot()
    operation = {**operation}
    if "component_id" in operation: operation["component_id"] = a.id if operation["component_id"] == "$self" else b.id
    build = AsyncMock(); monkeypatch.setattr(commands, "build_design", build)
    provider = client(proposal(operation))
    with pytest.raises(CadenzaError):
        asyncio.run(prompts.handle_assembly_prompt(session, request(session, a.id), discard, client=provider))
    build.assert_not_called(); provider.complete_json.assert_awaited_once()
    assert session.snapshot() == before and session.preview is None


def test_invalid_target_rejected_before_provider():
    session, _ = established(); provider = client({})
    with pytest.raises(CadenzaError):
        asyncio.run(prompts.handle_assembly_prompt(session, request(session, "comp_missing"), discard, client=provider))
    provider.complete_json.assert_not_called()


@pytest.mark.parametrize("case", ["revision", "path", "owner", "draft", "pending", "incident"])
def test_admission_never_calls_provider(monkeypatch, case):
    session, (a, _) = established(); req = request(session, a.id); provider = client({})
    if case == "revision": req = req.model_copy(update={"base_revision": 100})
    if case == "path": req = req.model_copy(update={"owner_path": [a.id]})
    if case == "owner": req = req.model_copy(update={"ledger_id": "source_0"})
    if case == "draft": monkeypatch.setattr(session, "draft", object())
    if case == "pending": monkeypatch.setattr(prompts, "pending", lambda current: object())
    if case == "incident":
        raw = req.model_dump(mode="json"); raw["target"]["detach_joint_ids"] = ["joint_missing"]
        req = AssemblyPrompt.model_validate(raw)
    with pytest.raises(CadenzaError): asyncio.run(prompts.handle_assembly_prompt(session, req, discard, client=provider))
    provider.complete_json.assert_not_called()


@pytest.mark.parametrize("failure", [prompts.AIError("private provider details"), prompts.AINotConfigured("missing"),
    prompts.AIRefused("refused"), prompts.HTTPError("private URL")])
def test_provider_failures_preserve_design_and_do_not_retry(failure):
    session, (a, _) = established(); before = session.snapshot(); provider = client({})
    provider.complete_json.side_effect = failure
    with pytest.raises(CadenzaError) as error:
        asyncio.run(prompts.handle_assembly_prompt(session, request(session, a.id), discard, client=provider))
    assert "private" not in error.value.message
    provider.complete_json.assert_awaited_once()
    assert session.snapshot() == before and not session.history and session.preview is None


def test_unavailable_provider_is_rejected_before_interpretation(monkeypatch):
    from types import SimpleNamespace
    session, (a, _) = established(); interpreter = AsyncMock()
    monkeypatch.setattr(prompts, "get_settings", lambda: SimpleNamespace(ai_available=False, vision_available=False))
    monkeypatch.setattr(prompts, "interpret", interpreter)
    with pytest.raises(CadenzaError): asyncio.run(prompts.handle_assembly_prompt(session, request(session, a.id), discard))
    interpreter.assert_not_called()


@pytest.mark.parametrize("raw", [dict(kind="proposal", description="x", operations=[], assumptions=[]),
    dict(kind="clarification", question="Which?", operations=[]),
    proposal(dict(op="add_rigid_joint", component_a="comp_a", component_b="comp_b",
        target_a=dict(point=[0, 0, 0]), target_b=dict(point=[1, 0, 0]))),
    proposal(dict(op="set_instance_placement", component_id="comp_a", placement=dict(origin=[True, 0, 0], rotation_deg=[0, 0, 0])))])
def test_interpreter_strict_output(raw):
    with pytest.raises(ValidationError): intents.INTENT.validate_python(raw)


@pytest.mark.parametrize("corruption", ["source", "owner_feature", "other_instance", "deleted_source"])
def test_candidate_delta_rejects_unrelated_changes(corruption):
    session, (a, b) = established()
    req = command(session, dict(op="rename_instance", component_id=a.id, name="Renamed"))
    candidate, _ = commands.prepare_command(session, req)
    owner = candidate[session.primary_ledger_id]
    assert owner.assembly is not None
    if corruption == "source": candidate["source_0"].metadata.name = "Changed"
    if corruption == "owner_feature": owner.features = candidate["source_0"].features
    if corruption == "other_instance": owner.assembly.components[1].name = "Wrong"
    if corruption == "deleted_source": del candidate["source_0"]
    with pytest.raises(CadenzaError): commands.validate_command_delta(session.snapshot(), candidate, req)


def test_assembly_words_do_not_trigger_source_geometry_regexes():
    session, _ = established(); before = session.snapshot()
    for text in ("add two holes", "set height to 200 mm"):
        checks = asyncio.run(verify_edit(before, before, text, prompt_scope="assembly"))
        assert "not independently verified" in checks[-1]
        with pytest.raises(CadenzaError): asyncio.run(verify_edit(before, before, text))


def test_real_prompt_preview_cancel_and_apply_preserve_sources():
    from cadenza_backend.session import Session, LedgerStore
    session, (a, _) = established(); before = session.snapshot()
    req = request(session, a.id)
    raw = proposal(dict(op="set_instance_placement", component_id=a.id, placement=placement(20)))
    asyncio.run(prompts.handle_assembly_prompt(session, req, discard, client=client(raw)))
    assert session.preview is not None and not session.preview.outcome.stub
    assert session.preview.message.prompt == req.prompt and session.snapshot() == before
    assert session.preview.outcome.volume_mm3 == pytest.approx((10 + 11) * 8 * 4)
    manual = Session(primary_ledger_id=session.primary_ledger_id,
        stores={lid: LedgerStore(ledger.model_copy(deep=True)) for lid, ledger in before.items()})
    asyncio.run(commands.handle_assembly_command(manual, command(manual,
        dict(op="set_instance_placement", component_id=a.id, placement=placement(20))), discard))
    assert manual.preview is not None
    assert manual.preview.ledgers == session.preview.ledgers
    assert manual.preview.outcome.volume_mm3 == session.preview.outcome.volume_mm3
    decide(session, "cancel")
    assert session.snapshot() == before and not session.history
    asyncio.run(prompts.handle_assembly_prompt(session, req, discard, client=client(raw)))
    decide(session)
    assert session.revision == 1 and len(session.history) == 1
    assert session.store.ledger.assembly is not None
    assert session.store.ledger.assembly.components[0].placement.origin == [20, 0, 0]
    for lid in ("source_0", "source_1", "source_2"):
        assert session.stores[lid].ledger.model_dump(exclude={"revision"}) == before[lid].model_dump(exclude={"revision"})


@pytest.mark.parametrize("target,operations", [
    (dict(kind="definition"), [dict(op="initialize_assembly", name="Fixture"), dict(op="add_instance", source_ledger_id="source", name="Plate", placement=placement())]),
    (dict(kind="definition"), [dict(op="create_subassembly", name="Module", instance_name="Module A", placement=placement(), children=[dict(source_ledger_id="source", name="Plate", placement=placement())])]),
    (dict(kind="instance", component_id="comp_a"), [dict(op="rename_instance", component_id="comp_a", name="Plate A")]),
    (dict(kind="instance", component_id="comp_a"), [dict(op="duplicate_instance", component_id="comp_a", name="Repeat", placement=placement(100))]),
    (dict(kind="instance", component_id="comp_a", detach_joint_ids=["joint_a"]), [dict(op="remove_joint", joint_id="joint_a"), dict(op="remove_instance", component_id="comp_a")]),
    (dict(kind="attachment", component_a="comp_a", component_b="comp_b"), [dict(op="add_rigid_joint", component_a="comp_a", component_b="comp_b", target_a=dict(point=[0, 0, 0], normal=None), target_b=dict(point=[20, 0, 0], normal=None))]),
    (dict(kind="attachment", component_a="comp_a", component_b="comp_b", joint_id="joint_a"), [dict(op="remove_joint", joint_id="joint_a"), dict(op="add_rigid_joint", component_a="comp_a", component_b="comp_b", target_a=dict(point=[0, 0, 4], normal=[0, 0, 1]), target_b=dict(point=[20, 0, 0], normal=[0, 0, -1]))]),
])
def test_offline_supported_target_vocabulary(target, operations):
    from pydantic import TypeAdapter
    from cadenza_contracts.assembly_commands import AssemblyPromptTarget
    result = intents.INTENT.validate_python(proposal(*operations))
    assert isinstance(result, intents.Proposal)
    prompts.validate_operations(TypeAdapter(AssemblyPromptTarget).validate_python(target), result.operations)


@pytest.mark.parametrize("wording,expected", [("move by -5 mm along assembly X", placement(-5)),
    ("set rotation Z to 90 degrees", placement(0, [0, 0, 90])), ("set Y to -12.5 mm", dict(origin=[0, -12.5, 0], rotation_deg=[0, 0, 0]))])
def test_numeric_request_conventions(wording, expected):
    session, (a, _) = established()
    req = request(session, a.id).model_copy(update={"prompt": wording})
    result = intents.INTENT.validate_python(proposal(dict(op="set_instance_placement", component_id=a.id, placement=expected)))
    assert isinstance(result, intents.Proposal)
    prompts.verify_numeric_request(req, session.store.ledger, result.operations)
    assert result.operations[0].op == "set_instance_placement"
    result.operations[0].placement.origin[2] = 100
    with pytest.raises(CadenzaError): prompts.verify_numeric_request(req, session.store.ledger, result.operations)


def test_timeout_and_context_budget_never_retry(monkeypatch):
    session, (a, _) = established(); req = request(session, a.id)
    provider = client({})
    monkeypatch.setattr(intents, "MAX_CONTEXT_BYTES", 10)
    with pytest.raises(intents.AIError): asyncio.run(intents.interpret(req, {}, provider))
    provider.complete_json.assert_not_called()
    monkeypatch.setattr(intents, "MAX_CONTEXT_BYTES", 200_000)
    monkeypatch.setattr(intents, "PROMPT_TIMEOUT_S", 0.01)
    async def slow(**kwargs): await asyncio.sleep(1)
    provider.complete_json.side_effect = slow
    with pytest.raises(CadenzaError, match="timed out"):
        asyncio.run(prompts.handle_assembly_prompt(session, req, discard, client=provider))
    provider.complete_json.assert_awaited_once()
    assert session.preview is None and not session.history


def test_actual_request_budget_blocks_second_provider_attempt(monkeypatch):
    session, (a, _) = established(); calls = []
    class Provider:
        def __init__(self, before_request): self.admit = before_request
        async def complete_json(self, **kwargs):
            assert kwargs["max_tokens"] <= 8000
            for _ in range(2):
                self.admit("offline", "fixture")
                calls.append("request")
    monkeypatch.setattr(intents, "ModelClient", Provider)
    with pytest.raises(intents.AIError): asyncio.run(intents.interpret(request(session, a.id), {}))
    assert calls == ["request"]


def test_changed_snapshot_during_interpretation_is_rejected(monkeypatch):
    session, (a, _) = established(); provider = client({}); build = AsyncMock()
    async def replace(**kwargs):
        session.store.ledger.metadata.name = "External change"
        return proposal(dict(op="set_instance_placement", component_id=a.id, placement=placement(20)))
    provider.complete_json.side_effect = replace
    monkeypatch.setattr(commands, "build_design", build)
    with pytest.raises(CadenzaError): asyncio.run(prompts.handle_assembly_prompt(session, request(session, a.id), discard, client=provider))
    build.assert_not_called(); assert session.preview is None
