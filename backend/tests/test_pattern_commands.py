import asyncio
import json
import math
from unittest.mock import AsyncMock

import pytest

from cadenza_backend.design_commands import compile_command, handle_design_command
from cadenza_backend.design_prompts import handle_design_prompt
from cadenza_backend.edit_preview import decide_preview
from cadenza_backend.session import Session, LedgerStore
from cadenza_contracts.design_commands import DesignCommand
from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.ledger import Ledger, CircularPatternFeature
from cadenza_contracts.messages import DesignPrompt, PreviewDecision
from cadenza_contracts.patch import apply_patch
from cadenza_geometry.service import Build123dGeometryService


def source():
    return Ledger.model_validate({"features": [
        {"id": "feat_stock", "kind": "box", "parameters": {"length": 100, "width": 100, "height": 8}},
        {"id": "feat_seed", "name": "Seed", "kind": "hole", "parameters": {"diameter": 6, "through": True},
         "placement": {"origin": [20, 0, 8]}}]})


def session_for(ledger):
    return Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger)})


def command(ledger, operations, revision=0):
    return DesignCommand.model_validate(dict(message_id="msg_pattern", base_revision=revision,
                                            ledger_id=ledger.project_id, operations=operations))


def circular(**overrides):
    return dict(op="add_circular_pattern", name="Bolt circle", target_feature="feat_seed", count=4,
                center_xy=[0, 0], **overrides)


async def discard(_):
    pass


def circle_feature(ledger):
    feature = ledger.features[-1]
    assert isinstance(feature, CircularPatternFeature)
    return feature


class OfflineClient:
    def __init__(self, operations):
        self.complete_json = AsyncMock(return_value=dict(kind="proposal", description="Pattern proposal",
                                                        operations=operations, assumptions=[]))


@pytest.mark.parametrize("count", [2, 3, 8, 32])
def test_creation_owns_identity_and_inherits_operation(count):
    ledger = source()
    op = circular()
    op["count"] = count
    patches = compile_command(session_for(ledger), command(ledger, [op]))
    candidate = apply_patch(ledger, patches)
    pattern = circle_feature(candidate)
    assert pattern.id.startswith("feat_") and pattern.id not in {f.id for f in ledger.features}
    assert pattern.operation == "subtract" and pattern.parameters.count == count
    assert pattern.parameters.radius == 20
    assert candidate.features[:-1] == ledger.features


def test_updates_preserve_radius_and_conflicts_reject_atomically():
    ledger = source()
    ledger = apply_patch(ledger, compile_command(session_for(ledger), command(ledger, [circular()])))
    pid = ledger.features[-1].id
    update = dict(op="update_circular_pattern", feature_id=pid, count=8, center_xy=[5, 0])
    changed = apply_patch(ledger, compile_command(session_for(ledger), command(ledger, [update])))
    assert circle_feature(changed).parameters.radius == 20
    assert changed.features[-1].placement.origin == [5, 0, 0]
    for operations in [[update, update], [update, dict(op="remove_feature", feature_id=pid)],
                       [update, dict(op="set_suppressed", feature_id=pid, suppressed=True)],
                       [dict(op="set_suppressed", feature_id=pid, suppressed=True), update],
                       [dict(op="remove_feature", feature_id="feat_seed")],
                       [dict(op="set_suppressed", feature_id="feat_seed", suppressed=True)]]:
        with pytest.raises(CadenzaError):
            compile_command(session_for(ledger), command(ledger, operations))
    grouped = [dict(op="remove_feature", feature_id=fid) for fid in ("feat_seed", pid)]
    assert len(apply_patch(ledger, compile_command(session_for(ledger), command(ledger, grouped))).features) == 1


def test_copy_budget_and_invalid_seed_modes_reject_before_build():
    ledger = source()
    op = circular()
    op["count"] = 32
    with pytest.raises(CadenzaError, match="128"):
        compile_command(session_for(ledger), command(ledger, [op] * 5))
    for modification in [dict(rotation_deg=[0, 0, 45]), dict(relative_to="feat_stock")]:
        raw = source().model_dump(mode="json")
        raw["features"][1]["placement"].update(modification)
        invalid = Ledger.model_validate(raw)
        with pytest.raises(CadenzaError):
            compile_command(session_for(invalid), command(invalid, [circular()]))


@pytest.mark.parametrize("ai", [False, True])
def test_real_preview_apply_cancel_and_failed_effect_preserve_accepted_state(ai):
    session = session_for(source())
    ledger = session.store.ledger
    before = session.snapshot()
    events = []
    async def emit(event): events.append(event)
    op = circular()
    if ai:
        client = OfflineClient([op])
        asyncio.run(handle_design_prompt(session, DesignPrompt(message_id="msg_ai", base_revision=0, scope="source",
            ledger_id=ledger.project_id, feature_id="feat_seed", prompt="Repeat this hole as four total around center 0,0"), emit, client=client))
        assert client.complete_json.call_count == 1
    else:
        asyncio.run(handle_design_command(session, command(ledger, [op]), emit))
    assert session.preview is not None and session.snapshot() == before and not session.history
    assert session.preview.outcome.volume_mm3 == pytest.approx(80000 - 4 * 9 * math.pi * 8)
    assert any("4 complete bores" in check for check in session.preview.message.checks)
    decision = PreviewDecision(message_id="msg_apply", base_revision=0, preview_id=session.preview.id, action="apply")
    asyncio.run(decide_preview(session, decision, emit))
    with pytest.raises(CadenzaError): asyncio.run(decide_preview(session, decision, emit))
    accepted = session.snapshot()
    pattern = session.store.ledger.features[-1]
    assert session.revision == 1 and len(session.history) == 1
    # A seed move updates every bore, while compatibility radius stays unchanged.
    move = dict(op="set_origin", feature_id="feat_seed", axis="x", value=30)
    asyncio.run(handle_design_command(session, command(ledger, [move], 1), emit))
    assert session.preview is not None
    assert circle_feature(session.preview.ledgers[ledger.project_id]).parameters.radius == 20
    asyncio.run(decide_preview(session, PreviewDecision(message_id="msg_cancel", base_revision=1,
        preview_id=session.preview.id, action="cancel"), emit))
    assert session.snapshot() == accepted
    # Stock shrink consumes part of the promised hole pattern. Build alone passes;
    # the independent check must reject without touching accepted data/artifacts.
    artifacts, history = dict(session.artifacts._items), list(session.history)
    with pytest.raises(CadenzaError):
        asyncio.run(handle_design_command(session, command(ledger,
            [dict(op="set_dimension", feature_id="feat_stock", parameter="width", value=20)], 1), emit))
    assert session.snapshot() == accepted and session.artifacts._items == artifacts and session.history == history
    assert session.preview is None
    assert pattern.id in {f.id for f in session.store.ledger.features}


def test_pinned_pattern_cannot_move_seed_and_context_uses_derived_radius():
    ledger = source()
    ledger = apply_patch(ledger, compile_command(session_for(ledger), command(ledger, [circular()])))
    ledger.features[1].placement.origin = [30, 0, 8]
    session = session_for(ledger)
    op = dict(op="set_origin", feature_id="feat_seed", axis="x", value=40)
    client = OfflineClient([op])
    events = []
    async def emit(event): events.append(event)
    asyncio.run(handle_design_prompt(session, DesignPrompt(message_id="msg_ai", base_revision=0, scope="source",
        ledger_id=ledger.project_id, feature_id=ledger.features[-1].id, prompt="Set radius to 40 mm"), emit, client=client))
    assert session.preview is None and any(getattr(e, "needs_clarification", False) for e in events)
    context = json.loads(client.complete_json.call_args.kwargs["user"])
    pattern = context["features"][-1]
    assert pattern["actual_radius_read_only"] == 30 and "radius" not in pattern["parameters"]


@pytest.mark.parametrize("direction", ["+x", "-x", "+y", "-y", "+z", "-z"])
@pytest.mark.parametrize("kind", ["box", "cylinder"])
def test_linear_primitive_copies_independent_bounds_and_volume(direction, kind):
    params = dict(length=2, width=2, height=2) if kind == "box" else dict(diameter=2, height=2)
    ledger = Ledger.model_validate({"features": [dict(id="feat_seed", kind=kind, parameters=params)]})
    op = dict(op="add_linear_pattern", name="Copies", target_feature="feat_seed", count=3, spacing=10, direction=direction)
    candidate = apply_patch(ledger, compile_command(session_for(ledger), command(ledger, [op])))
    service = Build123dGeometryService()
    solid = service._compile(candidate).solid
    assert sum(s.volume for s in solid.solids()) == pytest.approx(3 * (8 if kind == "box" else 2 * math.pi))
    axis = "xyz".index(direction[1])
    centers = sorted(tuple(s.center()) for s in solid.solids())
    assert sorted(c[axis] for c in centers) == pytest.approx(sorted((1 if axis == 2 else 0) + (1 if direction[0] == "+" else -1) * i * 10 for i in range(3)))
    result = service.run_probe_query(candidate, dict(query_type="pattern_effects", feature_ids=[candidate.features[-1].id]))
    assert "error" not in result, result


@pytest.mark.parametrize("kind", ["box", "cylinder"])
@pytest.mark.parametrize("operation", ["add", "subtract"])
@pytest.mark.parametrize("pattern_kind", ["linear", "circular"])
def test_primitive_effects_for_both_operations(kind, operation, pattern_kind):
    params = dict(length=2, width=2, height=4) if kind == "box" else dict(diameter=2, height=4)
    features = [dict(id="feat_seed", kind=kind, operation=operation, parameters=params, placement=dict(origin=[20, 0, 2]))]
    if operation == "subtract":
        features.insert(0, dict(id="feat_stock", kind="box", parameters=dict(length=120, width=120, height=10)))
    ledger = Ledger.model_validate(dict(features=features))
    op = circular() if pattern_kind == "circular" else dict(op="add_linear_pattern", name="Row", target_feature="feat_seed", count=4, spacing=10, direction="-x")
    candidate = apply_patch(ledger, compile_command(session_for(ledger), command(ledger, [op])))
    service = Build123dGeometryService()
    result = service.run_probe_query(candidate, dict(query_type="pattern_effects", feature_ids=[candidate.features[-1].id]))
    assert "error" not in result, result
    unit = 16 if kind == "box" else 4 * math.pi
    assert sum(s.volume for s in service._compile(candidate).solid.solids()) == pytest.approx(4 * unit if operation == "add" else 144000 - 4 * unit)
    assert result["patterns"][0]["copy_effects_mm3"] == pytest.approx([unit] * 3)


def test_named_seed_driver_and_other_source_ownership():
    raw = source().model_dump(mode="json")
    raw["relationships"] = dict(parameters=dict(seed_x=dict(unit="mm", value=20)),
        bindings=[dict(feature_id="feat_seed", target="x", expression="seed_x")])
    ledger = Ledger.model_validate(raw)
    ledger = apply_patch(ledger, compile_command(session_for(ledger), command(ledger, [circular()])))
    session = session_for(ledger)
    other = ledger.model_copy(deep=True, update=dict(project_id="prj_other"))
    session.stores[other.project_id] = LedgerStore(other)
    asyncio.run(handle_design_command(session, command(ledger,
        [dict(op="set_origin", feature_id="feat_seed", axis="x", value=30)]), discard))
    assert session.preview is not None
    candidate = session.preview.ledgers[ledger.project_id]
    assert candidate.relationships.parameters["seed_x"].value == 30
    assert candidate.features[1].placement.origin[0] == 30
    assert session.preview.ledgers[other.project_id] == other.model_copy(update={"revision": candidate.revision})
    assert session.stores[other.project_id].ledger == other


def test_legacy_project_history_and_redo_roundtrip(tmp_path):
    from cadenza_backend.projects import ProjectRepository
    from cadenza_contracts.project import ProjectDocument, load_document
    raw = source().model_dump(mode="json")
    raw["features"].append(dict(id="feat_pattern", kind="circular_pattern", operation="subtract",
        parameters=dict(target_feature="feat_seed", count=4, radius=777, sweep_angle=180, axis=[1, 0, 0])))
    ledger = Ledger.model_validate(raw)
    doc = ProjectDocument(name="Legacy", primary_ledger_id=ledger.project_id, ledgers={ledger.project_id: ledger},
        history=[{ledger.project_id: ledger.model_copy(deep=True)}], redo=[{ledger.project_id: ledger.model_copy(deep=True)}])
    repo = ProjectRepository(tmp_path / "legacy.sqlite3")
    pid, version = repo.save(doc, None, 0)
    reopened, actual_version = ProjectRepository(repo.path).load(pid)
    assert reopened == doc and actual_version == version
    assert load_document(doc.model_dump(mode="json")) == doc
    for snapshot in [reopened.ledgers, *reopened.history, *reopened.redo]:
        pattern = circle_feature(snapshot[ledger.project_id])
        assert pattern.parameters.radius == 777 and pattern.parameters.axis == [1, 0, 0]


def test_linear_update_ai_and_direct_geometry_parity():
    ledger = source()
    ledger.features[1].placement.origin = [-20, 0, 8]
    creation = dict(op="add_linear_pattern", name="Row", target_feature="feat_seed", count=3, spacing=20, direction="+x")
    ledger = apply_patch(ledger, compile_command(session_for(ledger), command(ledger, [creation])))
    target = ledger.features[-1].id
    update = dict(op="update_linear_pattern", feature_id=target, count=4, spacing=15, direction="+y")
    direct, ai = session_for(ledger.model_copy(deep=True)), session_for(ledger.model_copy(deep=True))
    asyncio.run(handle_design_command(direct, command(ledger, [update]), discard))
    asyncio.run(handle_design_prompt(ai, DesignPrompt(message_id="msg_ai", base_revision=0, scope="source",
        ledger_id=ledger.project_id, feature_id=target, prompt="Use four total instances spaced 15 mm in +Y"),
        discard, client=OfflineClient([update])))
    assert ai.preview is not None and direct.preview is not None
    assert ai.preview.ledgers == direct.preview.ledgers
    assert ai.preview.outcome.volume_mm3 == pytest.approx(80000 - 4 * 9 * math.pi * 8)


@pytest.mark.parametrize("valid", [True, False])
def test_legacy_embedded_pattern_edits_receive_effect_verification(valid):
    from cadenza_backend.edit_preview import verify_edit
    features = source().model_dump(mode="json")["features"]
    before = Ledger.model_validate({"assembly": {"parts": [dict(id="part_plate", features=features)],
        "components": [dict(id="comp_plate", part_id="part_plate")]}})
    raw = before.model_dump(mode="json")
    raw["assembly"]["parts"][0]["features"].append(dict(id="feat_pattern", kind="circular_pattern" if valid else "linear_pattern",
        operation="subtract", parameters=dict(target_feature="feat_seed", count=4, **(dict(radius=20) if valid else dict(spacing=50)))))
    after = Ledger.model_validate(raw)
    # Both candidates are valid B-reps. The outside-stock copies must be caught
    # by the same independent inspection used for top-level source patterns.
    assert Build123dGeometryService().build(after).glb
    request = verify_edit({before.project_id: before}, {after.project_id: after}, "")
    if valid:
        assert any("4 complete bores" in check for check in asyncio.run(request))
    else:
        with pytest.raises(CadenzaError, match="hole is outside stock or fully redundant"):
            asyncio.run(request)
