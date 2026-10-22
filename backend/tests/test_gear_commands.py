import asyncio
import json
from unittest.mock import AsyncMock
from typing import Any

import pytest
from pydantic import ValidationError

from cadenza_backend.design_commands import compile_command, handle_design_command
from cadenza_backend.design_prompts import handle_design_prompt
from cadenza_backend.edit_preview import decide_preview
from cadenza_backend.part_commands import create_part, prompt_part
from cadenza_backend.session import Session, LedgerStore
from cadenza_contracts.design_commands import DesignCommand
from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.gear_authoring import GearSettings
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.messages import DesignPrompt, PartPrompt, PreviewDecision
from cadenza_contracts.part_commands import PartCreate
from cadenza_contracts.patch import apply_patch


SETTINGS = dict(module=1, teeth=24, height=5, pressure_angle_deg=20, shift=0)


def add(**overrides):
    return dict(op="add_gear", name="Spur gear", operation="add", **SETTINGS,
        placement=dict(origin=[0, 0, 0], rotation_deg=[0, 0, 0])) | overrides


def session_for(ledger=None):
    ledger = ledger or Ledger()
    return Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger)})


async def discard(_):
    pass


def command(session, ops):
    return DesignCommand.model_validate(dict(message_id="gear", base_revision=session.revision,
        ledger_id=session.primary_ledger_id, operations=ops))


@pytest.mark.parametrize("field,value", [("teeth", True), ("teeth", 24.5), ("teeth", 129), ("module", 0), ("height", float("inf")), ("shift", 2), ("pressure_angle_deg", 46)])
def test_strict_settings(field, value):
    with pytest.raises(ValidationError):
        GearSettings.model_validate(SETTINGS | {field: value})


def test_compiler_identity_duplicates_and_last_solid():
    session = session_for()
    doc = apply_patch(session.store.ledger, compile_command(session, command(session, [add()])))
    session = session_for(doc)
    fid = doc.features[0].id
    update = dict(op="update_gear", feature_id=fid, **(SETTINGS | dict(teeth=28)))
    changed = apply_patch(doc, compile_command(session, command(session, [update])))
    assert changed.features[0].id == fid and changed.features[0].placement == doc.features[0].placement
    for ops in ([update, update], [add()], [dict(op="remove_feature", feature_id=fid)],
                [dict(op="set_suppressed", feature_id=fid, suppressed=True)],
                [update, dict(op="set_suppressed", feature_id=fid, suppressed=True)]):
        with pytest.raises(CadenzaError):
            compile_command(session, command(session, ops))


def test_part_creation_version_and_native_cancel_apply():
    session = session_for()
    raw = dict(message_id="new", base_revision=0, name="Drive", operations=[add()])
    with pytest.raises(ValidationError): PartCreate.model_validate(raw)
    request = PartCreate.model_validate(raw | dict(creation_version=2))
    before = session.snapshot()
    asyncio.run(create_part(session, request, discard))
    assert session.snapshot() == before and session.preview is not None
    assert session.preview.message.warnings
    asyncio.run(decide_preview(session, PreviewDecision(message_id="cancel", base_revision=0, preview_id=session.preview.id, action="cancel"), discard))
    assert session.snapshot() == before and session.preview is None
    asyncio.run(create_part(session, request, discard))
    asyncio.run(decide_preview(session, PreviewDecision(message_id="apply", base_revision=0, preview_id=session.preview.id, action="apply"), discard))
    assert len(session.stores) == 2 and session.revision == 1 and len(session.history) == 1


@pytest.mark.parametrize("version", [1, 2])
def test_new_part_ai_is_version_gated(version):
    session = session_for()
    class Offline:
        complete_json = AsyncMock(return_value=dict(kind="proposal", description="Create spur gear", operations=[add()], assumptions=[]))
    client = Offline()
    request = PartPrompt(message_id="ai", base_revision=0, name="Drive", prompt="Create the specified gear", creation_version=version)
    if version == 1:
        with pytest.raises(CadenzaError): asyncio.run(prompt_part(session, request, discard, client=client))
        assert session.preview is None
    else:
        asyncio.run(prompt_part(session, request, discard, client=client))
        assert session.preview is not None
    assert ("gear" in json.loads(client.complete_json.call_args.kwargs["user"])["allowed_kinds"]) == (version == 2)


def test_parent_edit_rechecks_unchanged_bore_and_preserves_accepted_state():
    session = session_for()
    doc = apply_patch(session.store.ledger, compile_command(session, command(session, [add(), dict(
        op="add_feature", kind="hole", name="Bore", operation="subtract", placement=dict(origin=[0, 0, 5]),
        parameters=dict(diameter=4, through=True))])))
    session = session_for(doc)
    before, history = session.snapshot(), list(session.history)
    op = dict(op="update_gear", feature_id=doc.features[0].id, **(SETTINGS | dict(height=8)))
    with pytest.raises(CadenzaError, match="opening"):
        asyncio.run(handle_design_command(session, command(session, [op]), discard))
    assert session.snapshot() == before and session.history == history and session.preview is None


def test_pinned_gear_prompt_and_shared_consent_are_enforced():
    session = session_for()
    doc = apply_patch(session.store.ledger, compile_command(session, command(session, [add()])))
    owner = Ledger.model_validate(dict(assembly=dict(components=[
        dict(id="comp_a", name="A", sub_assembly_id=doc.project_id, placement=dict(origin=[-20, 0, 0])),
        dict(id="comp_b", name="B", sub_assembly_id=doc.project_id, placement=dict(origin=[20, 0, 0]))])))
    session = session_for(owner); session.stores[doc.project_id] = LedgerStore(doc)
    class Offline:
        complete_json = AsyncMock(return_value=dict(kind="proposal", description="Change gear width", assumptions=[],
            operations=[dict(op="update_gear", feature_id=doc.features[0].id, **(SETTINGS | dict(height=8)))]))
    client = Offline()
    raw: dict[str, Any] = dict(message_id="ai", base_revision=0, scope="source", ledger_id=doc.project_id, feature_id=doc.features[0].id,
        prompt="Set height to 8 mm", edit_context=dict(component_path=["comp_a"], shared_source_confirmed=False))
    asyncio.run(handle_design_prompt(session, DesignPrompt.model_validate(raw), discard, client=client))
    assert not client.complete_json.called and session.preview is None
    raw['edit_context']['shared_source_confirmed'] = True
    before = session.snapshot()
    asyncio.run(handle_design_prompt(session, DesignPrompt.model_validate(raw), discard, client=client))
    assert session.preview is not None and session.snapshot() == before
    asyncio.run(decide_preview(session, PreviewDecision(message_id="cancel", base_revision=0, preview_id=session.preview.id, action="cancel"), discard))
    client.complete_json.return_value['operations'] = [add()]
    asyncio.run(handle_design_prompt(session, DesignPrompt.model_validate(raw), discard, client=client))
    assert session.preview is None and session.snapshot() == before
    client.complete_json.return_value['operations'] = [dict(op="update_gear", feature_id=doc.features[0].id, **(SETTINGS | dict(module=2)))]
    raw['prompt'] = 'Change module to 2 mm.'
    with pytest.raises(CadenzaError):
        asyncio.run(handle_design_prompt(session, DesignPrompt.model_validate(raw), discard, client=client))
    assert session.preview is None and session.snapshot() == before


def test_independent_revolve_creation_uses_v2_and_keeps_profile():
    session = session_for()
    op = dict(op="add_revolve", name="Shaft", operation="add", vertices=[[0, 0], [4, 0], [4, 12], [0, 12]],
        angle=360, placement=dict(origin=[0, 0, 0], rotation_deg=[90, 0, 0]))
    request = PartCreate.model_validate(dict(message_id="shaft", base_revision=0, name="Shaft", creation_version=2, operations=[op]))
    asyncio.run(create_part(session, request, discard))
    assert session.preview is not None
    source_id = session.preview.message.display_ledger_id
    assert source_id is not None
    feature = session.preview.ledgers[source_id].features[0]
    assert feature.kind == "revolve"
    assert feature.parameters.model_dump(mode="json")["vertices"] == op['vertices']
    assert len(session.stores) == 1


def test_make_independent_then_edit_preserves_other_gear_occurrence():
    from cadenza_backend.assembly_commands import handle_assembly_command
    from cadenza_contracts.assembly_commands import AssemblyCommand
    session = session_for()
    doc = apply_patch(session.store.ledger, compile_command(session, command(session, [add()])))
    owner = Ledger.model_validate(dict(assembly=dict(components=[
        dict(id="comp_a", name="A", sub_assembly_id=doc.project_id, placement=dict(origin=[-20, 0, 0])),
        dict(id="comp_b", name="B", sub_assembly_id=doc.project_id, placement=dict(origin=[20, 0, 0]))])))
    session = session_for(owner);session.stores[doc.project_id] = LedgerStore(doc)
    request = AssemblyCommand.model_validate(dict(message_id="copy", base_revision=0, ledger_id=owner.project_id,
        owner_path=[], scope="occurrence", operations=[dict(op="make_independent", component_id="comp_a", name="Custom gear")]))
    asyncio.run(handle_assembly_command(session, request, discard))
    assert session.preview is not None
    asyncio.run(decide_preview(session, PreviewDecision(message_id="apply", base_revision=0, preview_id=session.preview.id, action="apply"), discard))
    assembly = session.store.ledger.assembly
    assert assembly is not None
    copy_id = assembly.components[0].sub_assembly_id
    assert copy_id is not None and copy_id != doc.project_id
    original = session.stores[doc.project_id].ledger.model_dump(mode="json", exclude={"revision"})
    update = DesignCommand.model_validate(dict(message_id="edit", base_revision=1, ledger_id=copy_id,
        operations=[dict(op="update_gear", feature_id=session.stores[copy_id].ledger.features[0].id, **(SETTINGS | dict(teeth=28)))]))
    asyncio.run(handle_design_command(session, update, discard))
    assert session.preview is not None and session.preview.ledgers[doc.project_id].model_dump(mode="json", exclude={"revision"}) == original
    assert session.stores[doc.project_id].ledger.model_dump(mode="json", exclude={"revision"}) == original
    asyncio.run(decide_preview(session, PreviewDecision(message_id="apply2", base_revision=1, preview_id=session.preview.id, action="apply"), discard))
    old, new = session.stores[doc.project_id].ledger.features[0], session.stores[copy_id].ledger.features[0]
    assert old.kind == "gear"
    assert new.kind == "gear"
    assert old.parameters.teeth == 24 and new.parameters.teeth == 28
