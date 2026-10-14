import asyncio
import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from cadenza_backend import sketch_drafts
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.edit_preview import decide_preview
from cadenza_backend.geometry_jobs import build_design
from cadenza_backend.design import accept_design
from cadenza_backend.session import Session, LedgerStore
from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.messages import PreviewDecision, parse_client_message
from cadenza_contracts.sketch_draft import DraftStart, DraftUpdate, DraftAction


async def ignore(_): pass


def aliased_plate():
    return [
        {"op": "add_entity", "alias": "outer", "entity": {"kind": "rectangle", "width": 100, "height": 60}},
        *[{"op": "add_dimension", "alias": name, "entity_id": {"entity_alias": "outer"}, "parameter": name, "value": value}
          for name, value in [("x", 10), ("y", 5), ("width", 100), ("height", 60)]],
        *[{"op": "add_driver", "parameter": name, "value": value}
          for name, value in [("outer_x", 10), ("outer_y", 5), ("width", 100), ("margin", 15)]],
        *[{"op": "bind_dimension", "constraint_id": {"constraint_alias": constraint}, "expression": name}
          for constraint, name in [("x", "outer_x"), ("y", "outer_y"), ("width", "width")]],
        {"op": "set_extrusion", "amount": 8},
    ]


def aliased_opening(alias, sign):
    return [
        {"op": "add_entity", "alias": alias, "entity": {"kind": "circle", "operation": "subtract", "diameter": 6}},
        *[{"op": "add_dimension", "alias": f"{alias}_{name}", "entity_id": {"entity_alias": alias}, "parameter": name, "value": value}
          for name, value in [("x", 0), ("y", 5), ("diameter", 6)]],
        {"op": "bind_dimension", "constraint_id": {"constraint_alias": f"{alias}_x"},
         "expression": f"outer_x {sign} (width / 2 - margin)"},
        {"op": "bind_dimension", "constraint_id": {"constraint_alias": f"{alias}_y"}, "expression": "outer_y"},
    ]


def test_atomic_aliases_and_persistent_margins_without_ai_or_kernel(monkeypatch):
    async def forbidden(*args, **kwargs): pytest.fail("Update called provider/kernel")
    monkeypatch.setattr(sketch_drafts, "build_design", forbidden)
    monkeypatch.setattr(ModelClient, "complete_json", forbidden)
    session = Session()
    start(session)
    state = update(session, *aliased_plate(), *aliased_opening("left", "-"), *aliased_opening("right", "+"))
    assert state.revision == 1
    assert state.solution is not None and state.solution.status == "fully_constrained"
    assert [e.x for e in state.solution.entities] == [10, -25, 45]
    width_id = next(c.id for c in state.definition.constraints if c.kind == "dimension" and c.parameter == "width")
    state = update(session, {"op": "set_dimension", "constraint_id": width_id, "value": 140})
    assert state.solution is not None
    assert [(e.x, e.y) for e in state.solution.entities] == [(10, 5), (-45, 5), (65, 5)]
    assert [e.diameter for e in state.solution.entities[1:]] == [6, 6]
    assert session.revision == 0 and not session.store.ledger.features
    assert "alias" not in state.model_dump_json()


@pytest.mark.parametrize("operations", [
    [{"op": "add_dimension", "entity_id": {"entity_alias": "future"}, "parameter": "x", "value": 0}],
    [{"op": "add_entity", "alias": "same", "entity": {"kind": "circle", "diameter": 40}},
     {"op": "add_dimension", "alias": "same", "entity_id": {"entity_alias": "same"}, "parameter": "x", "value": 0}],
    [{"op": "add_entity", "alias": "outer", "entity": {"kind": "circle", "diameter": 40}},
     {"op": "remove_constraint", "constraint_id": {"constraint_alias": "outer"}}],
    [{"op": "add_driver", "parameter": "value", "value": 10},
     {"op": "add_driver", "parameter": "value", "value": 20}],
    [{"op": "add_driver", "parameter": "value", "value": 10},
     {"op": "bind_dimension", "constraint_id": "con_missing", "expression": "value"}],
])
def test_invalid_alias_or_relationship_batch_rolls_back(operations):
    session = Session()
    before = start(session).model_copy(deep=True)
    with pytest.raises(CadenzaError): update(session, *operations)
    assert session.draft is not None and session.draft.state == before


@pytest.mark.parametrize("expression", ["missing", "2", "width + 2", "width / 0", "width ** 2", "__import__('os')"])
def test_invalid_dimension_bindings_preserve_draft(expression):
    session = Session()
    start(session)
    state = update(session, *aliased_plate())
    height = next(c.id for c in state.definition.constraints if c.kind == "dimension" and c.parameter == "height")
    with pytest.raises(CadenzaError): update(session, {"op": "bind_dimension", "constraint_id": height, "expression": expression})
    assert session.draft is not None and session.draft.state == state


def test_relationship_limits_rebinding_and_alias_lifetime():
    session = Session()
    start(session)
    state = update(session, *aliased_plate())
    width = next(c.id for c in state.definition.constraints if c.kind == "dimension" and c.parameter == "width")
    for op in [
        {"op": "bind_dimension", "constraint_id": width, "expression": "width * 2"},
        {"op": "remove_constraint", "constraint_id": width},
        {"op": "remove_entity", "entity_id": {"entity_alias": "outer"}},
        {"op": "set_dimension", "constraint_id": {"constraint_alias": "width"}, "value": 120},
    ]:
        with pytest.raises(CadenzaError): update(session, op)
        assert session.draft is not None and session.draft.state == state
    for offset in (0, 30):
        state = update(session, *[{"op": "add_driver", "parameter": f"extra_{i}", "value": 1, "unit": "scalar"}
                                 for i in range(offset, offset + 30)])
    assert len(state.relationships.parameters) == 64
    with pytest.raises(CadenzaError): update(session, {"op": "add_driver", "parameter": "overflow", "value": 1})
    assert session.draft is not None and session.draft.state == state
    empty = Session()
    start(empty)
    for offset in (0, 32):
        saved = update(empty, *[{"op": "add_driver", "parameter": f"value_{i}", "value": 1} for i in range(offset, offset + 32)])
    with pytest.raises(CadenzaError): update(empty, {"op": "add_driver", "parameter": "overflow", "value": 1})
    assert empty.draft is not None and empty.draft.state == saved


def test_margin_relationships_real_build_rotated_plane_and_persistence(tmp_path):
    from cadenza_backend.projects import ProjectRepository
    from cadenza_contracts.project import ProjectDocument
    session = Session()
    start(session)
    update(session, *aliased_plate())
    update(session, *aliased_opening("left", "-"))
    update(session, *aliased_opening("right", "+"))
    action(session, "finish")
    asyncio.run(decide_preview(session, decision(session), ignore))
    original = session.store.ledger
    raw = original.model_dump(mode="json")
    raw["features"][0]["placement"] = {"origin": [20, 30, 10], "rotation_deg": [30, 20, 15]}
    rotated = Ledger.model_validate(raw)
    session = Session(primary_ledger_id=rotated.project_id, stores={rotated.project_id: LedgerStore(ledger=rotated)})
    start(session, rotated.features[0].id)
    update(session, {"op": "set_driver", "parameter": "width", "value": 140})
    action(session, "finish")
    assert session.preview is not None and session.preview.outcome.step and session.preview.outcome.glb
    assert session.store.ledger == rotated
    asyncio.run(decide_preview(session, decision(session), ignore))
    accepted = session.store.ledger
    assert accepted.features[0].placement == rotated.features[0].placement
    repo = ProjectRepository(tmp_path / "margins.sqlite3")
    document = ProjectDocument(name="Margins", primary_ledger_id=session.primary_ledger_id,
        ledgers=session.snapshot(), history=session.history)
    identity, _ = repo.save(document, None, 0)
    restored, _ = repo.load(identity)
    assert restored.ledgers == session.snapshot() and restored.history == session.history
    built = asyncio.run(build_design(restored.ledgers, session.primary_ledger_id))
    assert built.ok and built.step
    reopened = restored.ledgers[session.primary_ledger_id]
    profile = reopened.features[0]
    assert profile.kind == "profile" and profile.parameters.solution is not None
    assert [e.x for e in profile.parameters.solution.entities] == [10, -45, 65]
    # Further width edits recompute the persisted relation, not just saved numeric positions.
    edited = Session(primary_ledger_id=reopened.project_id, stores={reopened.project_id: LedgerStore(ledger=reopened)})
    start(edited, profile.id)
    state = update(edited, {"op": "set_driver", "parameter": "width", "value": 160})
    assert state.solution is not None and [e.x for e in state.solution.entities] == [10, -55, 75]


def plate():
    raw = json.loads((Path(__file__).parents[2] / "shared/contract_fixtures/profile_plate.json").read_text())
    ledger = Ledger.model_validate(raw)
    return Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger=ledger)})


def start(session, profile_id=None):
    asyncio.run(sketch_drafts.handle_draft(session, DraftStart(message_id="start", base_revision=session.revision,
        profile_id=profile_id), ignore))
    assert session.draft is not None
    return session.draft.state


def request(session, **kwargs):
    assert session.draft is not None
    return dict(message_id="edit", base_revision=session.revision, draft_id=session.draft.state.id,
        draft_revision=session.draft.state.revision, **kwargs)


def update(session, *operations):
    asyncio.run(sketch_drafts.handle_draft(session, DraftUpdate.model_validate(request(session, operations=list(operations))), ignore))
    assert session.draft is not None
    return session.draft.state


def action(session, action):
    asyncio.run(sketch_drafts.handle_draft(session, DraftAction.model_validate(request(session, action=action)), ignore))


def decision(session, action: Literal["apply", "cancel"] = "apply"):
    assert session.preview is not None
    return PreviewDecision(message_id="decision", base_revision=session.revision, preview_id=session.preview.id, action=action)


def test_diagnostics_repair_bindings_and_atomic_updates_without_kernel_or_ai(monkeypatch):
    async def forbidden(*args, **kwargs): pytest.fail("Draft update called kernel or provider")
    monkeypatch.setattr(sketch_drafts, "build_design", forbidden)
    monkeypatch.setattr(ModelClient, "complete_json", forbidden)
    session = plate()
    before = session.snapshot()
    state = start(session, "feat_profile")
    assert state.solution is not None and state.solution.status == "fully_constrained"
    state = update(session, {"op": "remove_constraint", "constraint_id": "con_center"})
    assert state.solution is not None and state.solution.degrees_of_freedom == 2
    state = update(session, {"op": "set_seed", "entity_id": "ent_bore", "parameter": "x", "value": 5})
    assert state.solution is not None and state.solution.entities[1].x == 5
    state = update(session, {"op": "add_centre", "entity_a": "ent_plate", "entity_b": "ent_bore"})
    assert state.solution is not None and state.solution.status == "fully_constrained"
    state = update(session, {"op": "add_dimension", "entity_id": "ent_bore", "parameter": "x", "value": 5})
    assert state.solution is not None and state.solution.status == "conflicting"
    state = update(session, {"op": "remove_constraint", "constraint_id": state.definition.constraints[-1].id},
        {"op": "set_dimension", "constraint_id": "con_width", "value": 140})
    assert state.relationships.parameters["plate_width"].value == 140
    assert state.solution is not None and state.solution.entities[0].width == 140
    saved = state.model_copy(deep=True)
    for operations in [
        [{"op": "remove_constraint", "constraint_id": "con_width"}],
        [{"op": "set_seed", "entity_id": "ent_bore", "parameter": "x", "value": 1}],
        [{"op": "set_driver", "parameter": "plate_width", "value": 150}, {"op": "remove_entity", "entity_id": "ent_missing"}],
        [{"op": "remove_entity", "entity_id": "ent_plate"}],
        [{"op": "remove_entity", "entity_id": "ent_bore"}],
    ]:
        with pytest.raises(CadenzaError): update(session, *operations)
        assert session.draft is not None and session.draft.state == saved
    assert session.snapshot() == before and not session.history and session.preview is None


@pytest.mark.parametrize("operation", [
    {"op": "add_entity", "entity": {"id": "ent_client", "kind": "circle", "diameter": 20}},
    {"op": "add_entity", "entity": {"kind": "rectangle", "diameter": 20}},
    {"op": "set_seed", "entity_id": "ent_x", "parameter": "x", "value": float("inf")},
    {"op": "set_extrusion", "amount": 0},
    {"op": "set_driver", "parameter": "width", "value": float("nan")},
    {"op": "unknown"},
])
def test_malformed_operations_rejected_at_transport(operation):
    with pytest.raises(ValidationError):
        parse_client_message(json.dumps(dict(type="sketch.draft_update", message_id="bad", base_revision=0,
            draft_id="draft", draft_revision=0, operations=[operation])))


def test_batch_bounds_staleness_and_discard():
    session = Session()
    start(session)
    for count in (0, 33):
        with pytest.raises(ValidationError):
            DraftUpdate.model_validate(request(session, operations=[{"op": "set_extrusion", "amount": 8}] * count))
    old = DraftUpdate.model_validate(request(session, operations=[{"op": "add_entity", "entity": {"kind": "circle", "diameter": 40}}]))
    asyncio.run(sketch_drafts.handle_draft(session, old, ignore))
    with pytest.raises(CadenzaError): asyncio.run(sketch_drafts.handle_draft(session, old, ignore))
    assert session.draft is not None and len(session.draft.state.definition.entities) == 1
    discard = DraftAction.model_validate(request(session, action="discard"))
    asyncio.run(sketch_drafts.handle_draft(session, discard, ignore))
    assert session.draft is None and session.revision == 0 and not session.history
    with pytest.raises(CadenzaError): asyncio.run(sketch_drafts.handle_draft(session, old, ignore))


def rectangle_with_hole(session):
    start(session)
    state = update(session, {"op": "add_entity", "entity": {"kind": "rectangle", "width": 100, "height": 60}})
    outer = state.definition.entities[0].id
    update(session, *[{"op": "add_dimension", "entity_id": outer, "parameter": name, "value": value}
        for name, value in [("x", 0), ("y", 0), ("width", 100), ("height", 60)]])
    state = update(session, {"op": "add_entity", "entity": {"kind": "circle", "operation": "subtract", "diameter": 20}})
    hole = state.definition.entities[1].id
    update(session, {"op": "add_dimension", "entity_id": hole, "parameter": "diameter", "value": 20},
        {"op": "add_centre", "entity_a": outer, "entity_b": hole}, {"op": "set_extrusion", "amount": 8})


def test_real_finish_cancel_apply_once_history_and_stable_ids():
    session = Session()
    before = session.snapshot()
    rectangle_with_hole(session)
    assert session.draft is not None
    state = session.draft.state.model_copy(deep=True)
    action(session, "finish")
    assert session.preview is not None and session.preview.outcome.step and session.preview.outcome.glb
    assert session.snapshot() == before and session.revision == 0 and not session.history
    first = session.preview.ledgers
    with pytest.raises(CadenzaError): update(session, {"op": "set_extrusion", "amount": 9})
    asyncio.run(decide_preview(session, decision(session, "cancel"), ignore))
    assert session.preview is None and session.draft.state == state
    action(session, "finish")
    assert session.preview is not None and session.preview.ledgers == first
    apply = decision(session)
    asyncio.run(decide_preview(session, apply, ignore))
    assert session.draft is None and session.preview is None and session.revision == 1 and len(session.history) == 1
    with pytest.raises(CadenzaError): asyncio.run(decide_preview(session, apply, ignore))
    assert session.revision == 1 and len(session.history) == 1
    undo = session.stage_undo()
    assert undo is not None
    ledgers, index = undo
    asyncio.run(accept_design(session, ledgers, asyncio.run(build_design(ledgers, session.primary_ledger_id)), "undo", ignore, undo_index=index))
    assert not session.store.ledger.features and session.revision == 2


def test_invalid_finish_and_failed_build_preserve_accepted_artifacts(monkeypatch):
    session = plate()
    ledgers = session.stage({})
    asyncio.run(accept_design(session, ledgers, asyncio.run(build_design(ledgers, session.primary_ledger_id)), "seed", ignore))
    before, history, geometry = session.snapshot(), list(session.history), session.last_geometry
    start(session, "feat_profile")
    update(session, {"op": "set_dimension", "constraint_id": "con_diameter", "value": 80})
    with pytest.raises(CadenzaError): action(session, "finish")
    update(session, {"op": "set_dimension", "constraint_id": "con_diameter", "value": 30})
    async def fail(*args):
        from cadenza_backend.geometry_bridge import BuildOutcome
        return BuildOutcome(ok=False, detail="Injected kernel failure", stub=False)
    monkeypatch.setattr(sketch_drafts, "build_design", fail)
    with pytest.raises(CadenzaError, match="Injected kernel failure"): action(session, "finish")
    assert session.snapshot() == before and session.history == history and session.last_geometry is geometry
    assert session.draft is not None and session.preview is None


def test_empty_and_underconstrained_finish_never_build(monkeypatch):
    async def forbidden(*args): pytest.fail("Invalid draft reached build")
    monkeypatch.setattr(sketch_drafts, "build_design", forbidden)
    session = Session()
    start(session)
    with pytest.raises(CadenzaError): action(session, "finish")
    update(session, {"op": "add_entity", "entity": {"kind": "circle", "diameter": 40}})
    with pytest.raises(CadenzaError): action(session, "finish")
    assert session.revision == 0 and session.preview is None


def test_derived_bindings_and_explicit_dependency_removal():
    session = plate()
    session.store.ledger.relationships.bindings[1].expression = "bore_diameter * 2"
    start(session, "feat_profile")
    with pytest.raises(CadenzaError): update(session, {"op": "set_dimension", "constraint_id": "con_diameter", "value": 30})
    state = update(session, {"op": "set_driver", "parameter": "bore_diameter", "value": 15})
    assert state.solution is not None and state.solution.entities[1].diameter == 30
    action(session, "discard")
    session = Session()
    rectangle_with_hole(session)
    assert session.draft is not None
    state = session.draft.state
    hole = state.definition.entities[1].id
    referencing = [c.id for c in state.definition.constraints if c.kind == "coincident" or c.entity_id == hole]
    state = update(session, *[{"op": "remove_constraint", "constraint_id": cid} for cid in referencing],
        {"op": "remove_entity", "entity_id": hole})
    assert len(state.definition.entities) == 1 and state.solution is not None and state.solution.status == "fully_constrained"


@pytest.mark.parametrize("invalidate", ["expiry", "draft_revision", "accepted_snapshot"])
def test_review_invalidation_cannot_commit(invalidate):
    session = Session()
    rectangle_with_hole(session)
    action(session, "finish")
    assert session.preview is not None and session.draft is not None
    apply = decision(session)
    if invalidate == "expiry": session.preview.created -= 901
    elif invalidate == "draft_revision": session.draft.state.revision += 1
    else: session.store.ledger.metadata.name = "Changed externally"
    before = session.snapshot()
    with pytest.raises(CadenzaError): asyncio.run(decide_preview(session, apply, ignore))
    assert session.snapshot() == before and not session.history and session.draft is not None


def test_websocket_guards_save_reconnect_and_stale_revision(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from cadenza_backend import app as transport
    from cadenza_backend.projects import ProjectRepository
    from cadenza_backend.session import registry
    monkeypatch.setattr(transport, "projects", ProjectRepository(tmp_path / "draft.sqlite3"))
    session = plate()
    registry.register(session)
    def receive(ws, kind):
        while True:
            frame = ws.receive()
            if frame.get("text"):
                value = json.loads(frame["text"])
                if value["type"] == kind: return value
                assert value["type"] != "error", value
    with TestClient(transport.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "client.hello", "contract_version": "1.0.0", "session_id": session.id})
            receive(ws, "session.ready")
            ws.send_json(DraftStart(message_id="start", base_revision=0, profile_id="feat_profile").model_dump())
            receive(ws, "sketch.draft_state")
            for frame in [
                {"type": "user.prompt", "prompt": "Change width"},
                {"type": "session.undo"}, {"type": "session.redo"}, {"type": "ledger.create", "name": "Other"},
                {"type": "session.reset"},
                {"type": "design.parameter", "ledger_id": session.primary_ledger_id, "feature_id": "feat_extrusion", "parameter": "amount", "value": 12},
                {"type": "design.sketch_dimension", "ledger_id": session.primary_ledger_id, "feature_id": "feat_profile", "constraint_id": "con_width", "value": 140},
            ]:
                ws.send_json({"message_id": "competing", **({"base_revision": 0} if frame["type"] != "session.reset" else {}), **frame})
                assert receive(ws, "error")["code"] == "BUSY"
            change = DraftUpdate.model_validate(request(session, operations=[{"op": "set_driver", "parameter": "plate_width", "value": 140}]))
            ws.send_json(change.model_dump(mode="json"))
            receive(ws, "sketch.draft_state")
            saved = client.post(f"/api/sessions/{session.id}/save", json={"name": "Accepted only", "base_revision": 0})
            assert saved.status_code == 200
            reopened = client.post(f"/api/projects/{saved.json()['id']}/open")
            assert reopened.status_code == 200
            restored = registry.get(reopened.json()["session_id"])
            assert restored is not None and restored.draft is None
            assert restored.store.ledger.relationships.parameters["plate_width"].value == 100
            stale = DraftAction.model_validate(request(session, action="finish"))
            with client.websocket_connect("/ws") as second:
                second.send_json({"type": "client.hello", "contract_version": "1.0.0", "session_id": session.id})
                receive(second, "session.ready")
                assert session.draft is None
                ws.send_json(stale.model_dump())
                assert receive(ws, "error")["code"] == "REVISION_CONFLICT"
                receive(ws, "sketch.draft_closed")
            start(session, "feat_profile")
            stale = DraftAction.model_validate(request(session, action="finish"))
            session.commit(session.stage({}))
            ws.send_json(stale.model_dump())
            assert receive(ws, "error")["code"] == "REVISION_CONFLICT"
            receive(ws, "sketch.draft_closed")
            assert receive(ws, "ledger.updated")["revision"] == 1
            ws.send_json({"type": "design.preview_decision", "message_id": "obsolete_review", "base_revision": 0,
                "preview_id": "expired_review", "action": "apply"})
            assert receive(ws, "error")["code"] == "REVISION_CONFLICT"
            receive(ws, "design.preview_closed")
            assert receive(ws, "ledger.updated")["revision"] == 1
            registry.drop(restored.id)
    registry.drop(session.id)


def test_entity_and_constraint_limits_rollback():
    session = Session()
    start(session)
    state = update(session, {"op": "add_entity", "entity": {"kind": "circle", "diameter": 100}})
    outer = state.definition.entities[0].id
    state = update(session, *[{"op": "add_entity", "entity": {"kind": "circle", "operation": "subtract", "diameter": 1}}] * 15)
    with pytest.raises(CadenzaError): update(session, {"op": "add_entity", "entity": {"kind": "circle", "operation": "subtract", "diameter": 1}})
    assert session.draft is not None and session.draft.state == state
    for _ in range(3):
        state = update(session, *[{"op": "add_dimension", "entity_id": outer, "parameter": "x", "value": 0}] * 32)
    with pytest.raises(CadenzaError): update(session, {"op": "add_dimension", "entity_id": outer, "parameter": "y", "value": 0})
    assert session.draft.state == state and session.revision == 0


def test_unchanged_finish_and_derived_driver_are_protected():
    session = plate()
    start(session, "feat_profile")
    with pytest.raises(CadenzaError, match="no changes"): action(session, "finish")
    assert session.preview is None and not session.history
    action(session, "discard")
    raw = session.store.ledger.model_dump(mode="json")
    raw["relationships"]["parameters"]["derived_diameter"] = {"unit": "mm", "expression": "bore_diameter * 2"}
    raw["relationships"]["bindings"][1]["expression"] = "derived_diameter"
    session.store.ledger = Ledger.model_validate(raw)
    start(session, "feat_profile")
    with pytest.raises(CadenzaError): update(session, {"op": "set_driver", "parameter": "derived_diameter", "value": 30})
    with pytest.raises(CadenzaError): update(session, {"op": "set_dimension", "constraint_id": "con_diameter", "value": 30})


def test_scope_and_fully_constrained_circle_creation():
    populated = plate()
    with pytest.raises(CadenzaError): start(populated)
    assembly = Ledger.model_validate({"assembly": {"components": []}})
    session = Session(primary_ledger_id=assembly.project_id, stores={assembly.project_id: LedgerStore(ledger=assembly)})
    with pytest.raises(CadenzaError): start(session)
    session = Session()
    start(session)
    state = update(session, {"op": "add_entity", "entity": {"kind": "circle", "diameter": 40}})
    entity_id = state.definition.entities[0].id
    update(session, *[{"op": "add_dimension", "entity_id": entity_id, "parameter": name, "value": value}
        for name, value in [("x", 0), ("y", 0), ("diameter", 40)]], {"op": "set_extrusion", "amount": 10})
    action(session, "finish")
    assert session.preview is not None and session.preview.outcome.volume_mm3 == pytest.approx(4000 * 3.141592653589793)
    action(session, "discard")
    assert session.preview is None and session.draft is None and not session.store.ledger.features
