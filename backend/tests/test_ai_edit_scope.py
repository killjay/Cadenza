import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from cadenza_backend import design_prompts as prompts
from cadenza_backend.session import LedgerStore, Session
from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.messages import DesignPrompt, PromptCapabilitiesResponse


def fixture(*, nested=False, uses=2):
    part = Ledger.model_validate(dict(project_id="part", metadata=dict(name="Spacer"), features=[
        dict(id="feat_stock", name="Spacer stock", kind="cylinder", operation="add", parameters=dict(diameter=12, height=30))]))
    inner = Ledger.model_validate(dict(project_id="inner" if nested else "root", metadata=dict(name="Fixture"),
        assembly=dict(components=[dict(id=f"comp_{i}", name=f"Spacer {i}", sub_assembly_id="part",
            placement=dict(origin=[i * 40, 0, 0])) for i in range(uses)])))
    root = Ledger.model_validate(dict(project_id="root", assembly=dict(components=[
        dict(id="comp_first", name="First fixture", sub_assembly_id="inner"),
        dict(id="comp_second", name="Second fixture", sub_assembly_id="inner", placement=dict(origin=[200, 0, 0]))]))) if nested else inner
    docs = {p.project_id: LedgerStore(p) for p in [root, inner, part]}
    return Session(primary_ledger_id="root", stores=docs)


def request(session, **updates):
    return DesignPrompt.model_validate(dict(message_id="scope", base_revision=session.revision, ledger_id="part",
        feature_id="feat_stock", scope="source", prompt="Set height to 34 mm", **updates))


def provider(operations=None, result=None):
    return SimpleNamespace(complete_json=AsyncMock(return_value=result or dict(kind="proposal",
        description="Proposed height", assumptions=[], operations=operations or [
            dict(op="set_dimension", feature_id="feat_stock", parameter="height", value=34)])))


@pytest.mark.parametrize("context", [dict(shared_source_confirmed="true"), dict(shared_source_confirmed=1),
    dict(component_path=[]), dict(component_path=["invalid"]), dict(component_path=["comp_a"] * 9), dict(extra=True)])
def test_strict_context(context):
    with pytest.raises(ValidationError): request(fixture(), edit_context=context)


def test_discovery_and_face_path_consistency():
    assert PromptCapabilitiesResponse(message_id="m", session_id="s").explicit_source_scope is True
    raw = request(fixture()).model_dump(mode="json")
    raw["face"] = dict(mode="edit_feature", component_path=["comp_0"], point=[0, 0, 30], normal=[0, 0, 1],
        reference=dict(ledger_id="part", source_ledger_id="part", feature_id="feat_stock", feature_kind="cylinder",
            operation="add", role="top_face"))
    raw["edit_context"] = dict(component_path=["comp_left"], shared_source_confirmed=True)
    with pytest.raises(ValidationError, match="same occurrence"): DesignPrompt.model_validate(raw)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("confirmed", [None, False, True])
def test_scope_admitted_before_provider_and_passed_as_server_context(monkeypatch, nested, confirmed):
    session = fixture(nested=nested); before = session.snapshot(); path = (["comp_first"] if nested else []) + ["comp_0"]
    req = request(session, edit_context=dict(component_path=path, shared_source_confirmed=confirmed) if confirmed is not None else None)
    client = provider(); emit = AsyncMock(); build = AsyncMock(); events = []
    monkeypatch.setattr(prompts, "handle_design_command", build)
    monkeypatch.setattr(prompts, "event", lambda name, **data: events.append((name, data)))
    asyncio.run(prompts.handle_design_prompt(session, req, emit, client=client))
    if confirmed:
        context = json.loads(client.complete_json.call_args.kwargs["user"])["edit_scope"]
        assert context["source_name"] == "Spacer" and context["selected_occurrence"]["path"] == path
        assert len(context["affected_occurrences"]) == (4 if nested else 2)
        assert build.call_args.kwargs["context_path"] == path
        assert "Shared geometry" in build.call_args.kwargs["scope_summary"]
    else:
        client.complete_json.assert_not_called(); build.assert_not_called()
        assert any(getattr(call.args[0], "needs_clarification", False) for call in emit.call_args_list)
    assert events[0][1]["allowed"] is bool(confirmed)
    assert "prompt" not in events[0][1]
    assert session.snapshot() == before and not session.history and session.preview is None


@pytest.mark.parametrize("kind", ["wrong_path", "wrong_source", "revision", "cycle"])
def test_invalid_context_never_calls_provider(monkeypatch, kind):
    session = fixture(); req = request(session, edit_context=dict(component_path=["comp_0"], shared_source_confirmed=True))
    assert req.edit_context is not None
    assert session.store.ledger.assembly is not None
    if kind == "wrong_path": req.edit_context.component_path = ["comp_missing"]
    if kind == "wrong_source": session.store.ledger.assembly.components[0].sub_assembly_id = "other"
    if kind == "revision": req.base_revision = 100
    if kind == "cycle": session.store.ledger.assembly.components[0].sub_assembly_id = "root"
    client = provider(); build = AsyncMock(); monkeypatch.setattr(prompts, "handle_design_command", build)
    with pytest.raises(CadenzaError): asyncio.run(prompts.handle_design_prompt(session, req, AsyncMock(), client=client))
    client.complete_json.assert_not_called(); build.assert_not_called()


@pytest.mark.parametrize("uses", [0, 1])
def test_legacy_unshared_source_needs_no_confirmation(monkeypatch, uses):
    session = fixture(uses=1)
    if uses == 0:
        session.primary_ledger_id = "part"
    client = provider(); build = AsyncMock(); monkeypatch.setattr(prompts, "handle_design_command", build)
    asyncio.run(prompts.handle_design_prompt(session, request(session), AsyncMock(), client=client))
    client.complete_json.assert_awaited_once(); build.assert_awaited_once()


@pytest.mark.parametrize("operation", [dict(op="add_feature", kind="box", operation="add", name="Unexpected box",
    parameters=dict(length=5, width=5, height=5), placement=dict(origin=[100, 0, 0], rotation_deg=[0, 0, 0])),
    dict(op="remove_feature", feature_id="feat_other")])
def test_feature_action_rejects_unrelated_additions_and_edits(monkeypatch, operation):
    session = fixture(uses=1); client = provider([operation]); build = AsyncMock(); emit = AsyncMock()
    monkeypatch.setattr(prompts, "handle_design_command", build)
    asyncio.run(prompts.handle_design_prompt(session, request(session), emit, client=client))
    build.assert_not_called(); assert any(getattr(c.args[0], "needs_clarification", False) for c in emit.call_args_list)


def test_add_action_cannot_edit_existing_feature(monkeypatch):
    session = fixture(uses=1); req = request(session); req.feature_id = None
    build = AsyncMock(); emit = AsyncMock(); before = session.snapshot()
    monkeypatch.setattr(prompts, "handle_design_command", build)
    asyncio.run(prompts.handle_design_prompt(session, req, emit, client=provider()))
    build.assert_not_called()
    assert any(getattr(c.args[0], "needs_clarification", False) for c in emit.call_args_list)
    assert session.snapshot() == before and session.preview is None


@pytest.mark.parametrize("result", [dict(kind="clarification", question="The image and text disagree. Which height should I use?"),
    dict(kind="unsupported", reason="Use assembly placement controls for this move.")])
def test_clarification_cannot_mutate(result, monkeypatch):
    session = fixture(uses=1); before = session.snapshot(); build = AsyncMock()
    monkeypatch.setattr(prompts, "handle_design_command", build)
    asyncio.run(prompts.handle_design_prompt(session, request(session), AsyncMock(), client=provider(result=result)))
    build.assert_not_called(); assert session.snapshot() == before and session.preview is None


def test_shared_native_preview_cancel_and_apply():
    from cadenza_backend.edit_preview import decide_preview
    from cadenza_contracts.messages import PreviewDecision
    async def run():
        session = fixture(); before = session.snapshot(); req = request(session,
            edit_context=dict(component_path=["comp_1"], shared_source_confirmed=True))
        emit = AsyncMock()
        await prompts.handle_design_prompt(session, req, emit, client=provider())
        preview = session.preview; assert preview and preview.message.context_component_path == ["comp_1"]
        assert "all 2 placed uses" in preview.message.summary
        assert session.snapshot() == before
        await decide_preview(session, PreviewDecision(message_id="cancel", base_revision=0, preview_id=preview.id, action="cancel"), emit)
        assert session.snapshot() == before and not session.history
        await prompts.handle_design_prompt(session, req, emit, client=provider())
        preview = session.preview; assert preview is not None
        await decide_preview(session, PreviewDecision(message_id="apply", base_revision=0, preview_id=preview.id, action="apply"), emit)
        feature = session.stores["part"].ledger.features[0]; assert feature.kind == "cylinder"
        assert feature.parameters.height == 34
        assert session.stores["root"].ledger.assembly == before["root"].assembly
        assert len(session.history) == 1
    asyncio.run(run())
