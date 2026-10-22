import asyncio
import json
import math
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from cadenza_backend.design_commands import compile_command, handle_design_command
from cadenza_backend.design_prompts import handle_design_prompt
from cadenza_backend.edit_preview import decide_preview
from cadenza_backend.session import Session, LedgerStore
from cadenza_contracts.design_commands import DesignCommand
from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.messages import DesignPrompt, PreviewDecision
from cadenza_contracts.patch import apply_patch
from cadenza_geometry.service import Build123dGeometryService


PROFILE = [[0, 0], [10, 0], [10, 5], [5, 5], [5, 20], [0, 20]]


class Offline:
    def __init__(self):
        self.complete_json = AsyncMock()


def add(**overrides):
    return dict(op="add_revolve", name="Flange", operation="add", vertices=PROFILE, angle=360,
                placement=dict(origin=[0, 0, 0], rotation_deg=[90, 0, 0])) | overrides


def session_for(ledger=None):
    ledger = ledger or Ledger()
    return Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger)})


def command(session, ops, revision=None):
    return DesignCommand.model_validate(dict(message_id="msg_revolve", ledger_id=session.primary_ledger_id,
        base_revision=session.revision if revision is None else revision, operations=ops))


async def discard(_):
    pass


def created():
    session = session_for()
    return apply_patch(session.store.ledger, compile_command(session, command(session, [add()])))


def test_compiler_preserves_encoding_placement_operation_and_other_sources():
    ledger = created()
    session = session_for(ledger)
    feature = ledger.features[0]
    before = ledger.model_dump(mode="json")
    changed = apply_patch(ledger, compile_command(session, command(session,
        [dict(op="update_revolve", feature_id=feature.id, vertices=PROFILE, angle=180)])))
    assert changed.features[0].placement == feature.placement
    assert changed.features[0].operation == feature.operation
    assert changed.features[0].parameters.model_dump()["axis"] == [0, 1, 0]
    assert changed.features[0].parameters.model_dump()["angle"] == 180
    assert ledger.model_dump(mode="json") == before
    for operations in [
        [dict(op="update_revolve", feature_id=feature.id, vertices=PROFILE, angle=90)] * 2,
        [dict(op="remove_feature", feature_id=feature.id)],
        [dict(op="set_suppressed", feature_id=feature.id, suppressed=True)],
        [dict(op="set_dimension", feature_id=feature.id, parameter="angle", value=90)],
    ]:
        with pytest.raises((CadenzaError, ValidationError)):
            compile_command(session, command(session, operations))


@pytest.mark.parametrize("mutation", ["axis", "relative", "suppressed", "bound"])
def test_unsupported_legacy_or_bound_updates_fail_without_mutation(mutation):
    raw = created().model_dump(mode="json")
    feature = raw["features"][0]
    if mutation == "axis": feature["parameters"]["axis"] = [0, 0, 1]
    elif mutation == "relative": feature["placement"]["relative_to"] = "feat_stock"
    elif mutation == "suppressed": feature["suppressed"] = True
    else:
        raw["relationships"] = dict(parameters=dict(offset=dict(unit="mm", value=0)),
            bindings=[dict(feature_id=feature["id"], target="x", expression="offset")])
    if mutation == "bound":
        with pytest.raises(ValidationError, match="Unsupported binding target"):
            Ledger.model_validate(raw)
        return
    ledger = Ledger.model_validate(raw)
    session = session_for(ledger)
    before = session.snapshot()
    with pytest.raises(CadenzaError):
        compile_command(session, command(session, [dict(op="update_revolve", feature_id=feature["id"], vertices=PROFILE, angle=90)]))
    assert session.snapshot() == before


@pytest.mark.parametrize("ai", [False, True])
def test_native_preview_apply_cancel_stale_and_no_effect_are_atomic(ai):
    session = session_for()
    accepted = session.snapshot()
    if ai:
        client = Offline()
        client.complete_json = AsyncMock(return_value=dict(kind="proposal", description="Turn flange",
            operations=[add()], assumptions=[]))
        asyncio.run(handle_design_prompt(session, DesignPrompt(message_id="msg_ai", base_revision=0,
            scope="source", ledger_id=session.primary_ledger_id, prompt="Add a turned flange with the supplied profile"), discard, client=client))
        context = json.loads(client.complete_json.call_args.kwargs["user"])
        assert context["revolve_capability"]["axis"] == "+Y"
    else:
        asyncio.run(handle_design_command(session, command(session, [add()]), discard))
    assert session.preview is not None and session.snapshot() == accepted
    assert session.preview.outcome.volume_mm3 == pytest.approx(875 * math.pi)
    assert any("material effect" in check for check in session.preview.message.checks)
    asyncio.run(decide_preview(session, PreviewDecision(message_id="apply", base_revision=0,
        preview_id=session.preview.id, action="apply"), discard))
    assert session.revision == 1 and len(session.history) == 1
    accepted = session.snapshot()
    fid = session.store.ledger.features[0].id
    update = dict(op="update_revolve", feature_id=fid, vertices=PROFILE, angle=180)
    asyncio.run(handle_design_command(session, command(session, [update]), discard))
    assert session.preview.outcome.volume_mm3 == pytest.approx(437.5 * math.pi)
    asyncio.run(decide_preview(session, PreviewDecision(message_id="cancel", base_revision=1,
        preview_id=session.preview.id, action="cancel"), discard))
    assert session.snapshot() == accepted
    artifacts, history = dict(session.artifacts._items), list(session.history)
    for ops, revision in [([add()], 1), ([add(placement=dict(origin=[100, 0, 0], rotation_deg=[90, 0, 0]))], 1), ([update], 0)]:
        with pytest.raises(CadenzaError):
            asyncio.run(handle_design_command(session, command(session, ops, revision), discard))
        assert session.snapshot() == accepted and session.preview is None
        assert session.artifacts._items == artifacts and session.history == history


def test_pinned_ai_edit_rejects_addition_and_uses_existing_target():
    session = session_for(created())
    fid = session.store.ledger.features[0].id
    before = session.snapshot()
    events = []
    async def emit(event): events.append(event)
    client = Offline()
    client.complete_json = AsyncMock(return_value=dict(kind="proposal", description="Wrong addition", operations=[add()], assumptions=[]))
    request = DesignPrompt(message_id="ai", base_revision=0, scope="source", ledger_id=session.primary_ledger_id,
        feature_id=fid, prompt="Change the angle to 180 degrees")
    asyncio.run(handle_design_prompt(session, request, emit, client=client))
    assert session.preview is None and session.snapshot() == before
    assert any(getattr(event, "needs_clarification", False) for event in events)
    client.complete_json.return_value = dict(kind="proposal", description="Change angle", assumptions=[],
        operations=[dict(op="update_revolve", feature_id=fid, vertices=PROFILE, angle=180)])
    asyncio.run(handle_design_prompt(session, request, emit, client=client))
    assert session.preview is not None and session.snapshot() == before
    assert session.preview.outcome.volume_mm3 == pytest.approx(437.5 * math.pi)


def test_native_subtraction_and_profile_only_prefix():
    ledger = created()
    raw = ledger.model_dump(mode="json")
    raw["features"].insert(0, dict(id="feat_sketch", kind="sketch", parameters=dict(vertices=[[0, 0], [1, 0], [0, 1]], height=0)))
    ledger = Ledger.model_validate(raw)
    service = Build123dGeometryService()
    result = service.run_probe_query(ledger, dict(query_type="revolve_effects", feature_ids=[ledger.features[-1].id]))
    assert "error" not in result, result
    session = session_for(ledger)
    op = add(operation="subtract", vertices=[[0, 0], [2, 0], [2, 20], [0, 20]])
    cut = apply_patch(ledger, compile_command(session, command(session, [op])))
    result = service.run_probe_query(cut, dict(query_type="revolve_effects", feature_ids=[cut.features[-1].id]))
    assert "error" not in result, result
    assert result["revolves"][0]["material_effect_mm3"] == pytest.approx(80 * math.pi)
    assert sum(s.volume for s in service._compile(cut).solid.solids()) == pytest.approx(795 * math.pi)


def test_shared_revolve_requires_ai_consent_and_native_assembly_interference_rejects():
    source = created()
    owner = Ledger.model_validate(dict(assembly=dict(components=[
        dict(id="comp_left", name="Left", sub_assembly_id=source.project_id, placement=dict(origin=[-12, 0, 0])),
        dict(id="comp_right", name="Right", sub_assembly_id=source.project_id, placement=dict(origin=[12, 0, 0]))])))
    session = session_for(owner)
    session.stores[source.project_id] = LedgerStore(source)
    before = session.snapshot()
    fid = source.features[0].id
    client = Offline()
    client.complete_json.return_value = dict(kind="proposal", description="Change shared angle", assumptions=[],
        operations=[dict(op="update_revolve", feature_id=fid, vertices=PROFILE, angle=180)])
    request = DesignPrompt.model_validate(dict(message_id="ai", base_revision=0, scope="source", ledger_id=source.project_id,
        feature_id=fid, prompt="Change the angle to 180 degrees", edit_context=dict(component_path=["comp_left"], shared_source_confirmed=False)))
    asyncio.run(handle_design_prompt(session, request, discard, client=client))
    assert not client.complete_json.called
    assert session.preview is None and session.snapshot() == before
    assert request.edit_context is not None
    request.edit_context.shared_source_confirmed = True
    asyncio.run(handle_design_prompt(session, request, discard, client=client))
    assert session.preview is not None and session.snapshot() == before
    assert session.preview.ledgers[owner.project_id].assembly == owner.assembly
    assert session.preview.outcome.volume_mm3 == pytest.approx(875 * math.pi)
    asyncio.run(decide_preview(session, PreviewDecision(message_id="cancel", base_revision=0,
        preview_id=session.preview.id, action="cancel"), discard))
    colliding = DesignCommand.model_validate(dict(message_id="overlap", base_revision=0, ledger_id=source.project_id,
        operations=[dict(op="update_revolve", feature_id=fid, vertices=[[0, 0], [20, 0], [20, 20], [0, 20]], angle=360)]))
    with pytest.raises(CadenzaError):
        asyncio.run(handle_design_command(session, colliding, discard))
    assert session.snapshot() == before and session.preview is None
