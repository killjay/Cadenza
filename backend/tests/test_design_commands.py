import pytest
from cadenza_backend.contracts_bridge import CadenzaError, Ledger
from cadenza_backend.design_commands import compile_command
from cadenza_contracts.design_commands import DesignCommand

class DummyStore:
    def __init__(self, ledger):
        self.ledger = ledger

class DummySession:
    def __init__(self, ledger: Ledger):
        self.stores = {ledger.project_id: DummyStore(ledger)}

def test_compile_add_feature():
    ledger = Ledger(project_id="prj_1", features=[])
    session = DummySession(ledger)
    cmd = DesignCommand.model_validate({
        "type": "design.command", "message_id": "m1", "base_revision": 1, "ledger_id": "prj_1", "scope": "source",
        "operations": [
            {"op": "add_feature", "kind": "box", "name": "Box", "operation": "add", 
             "placement": {"origin": [0,0,0]}, "parameters": {"length": 10, "width": 10, "height": 10}}
        ]
    })
    patches = compile_command(session, cmd)
    assert len(patches) == 1
    assert patches[0].op == "add"
    assert patches[0].path == "/features/-"
    assert patches[0].value["kind"] == "box"
    assert patches[0].value["parameters"]["length"] == 10
    
def test_compile_set_dimension():
    ledger = Ledger.model_validate({
        "project_id": "prj_1",
        "features": [{"id": "feat_1", "kind": "box", "operation": "add", "placement": {"origin": [0,0,0]}, "parameters": {"length": 10, "width": 10, "height": 10}}]
    })
    session = DummySession(ledger)
    cmd = DesignCommand.model_validate({
        "type": "design.command", "message_id": "m1", "base_revision": 1, "ledger_id": "prj_1", "scope": "source",
        "operations": [
            {"op": "set_dimension", "feature_id": "feat_1", "parameter": "length", "value": 20}
        ]
    })
    patches = compile_command(session, cmd)
    assert len(patches) == 1
    assert patches[0].op == "replace"
    assert patches[0].path == "/features/feat_1/parameters/length"
    assert patches[0].value == 20

def test_compile_remove_feature():
    ledger = Ledger.model_validate({
        "project_id": "prj_1",
        "features": [
            {"id": "feat_1", "kind": "box", "operation": "add", "placement": {"origin": [0,0,0]}, "parameters": {"length": 10, "width": 10, "height": 10}},
            {"id": "feat_2", "kind": "cylinder", "operation": "add", "placement": {"origin": [0,0,0]}, "parameters": {"diameter": 10, "height": 10}}
        ]
    })
    session = DummySession(ledger)
    cmd = DesignCommand.model_validate({
        "type": "design.command", "message_id": "m1", "base_revision": 1, "ledger_id": "prj_1", "scope": "source",
        "operations": [
            {"op": "remove_feature", "feature_id": "feat_1"}
        ]
    })
    patches = compile_command(session, cmd)
    assert len(patches) == 1
    assert patches[0].op == "remove"
    assert patches[0].path == "/features/feat_1"

def test_compile_set_suppressed():
    ledger = Ledger.model_validate({
        "project_id": "prj_1",
        "features": [{"id": "feat_1", "kind": "box", "operation": "add", "placement": {"origin": [0,0,0]}, "parameters": {"length": 10, "width": 10, "height": 10}}]
    })
    session = DummySession(ledger)
    cmd = DesignCommand.model_validate({
        "type": "design.command", "message_id": "m1", "base_revision": 1, "ledger_id": "prj_1", "scope": "source",
        "operations": [
            {"op": "set_suppressed", "feature_id": "feat_1", "suppressed": True}
        ]
    })
    with pytest.raises(CadenzaError, match="last solid"):
        compile_command(session, cmd)

# --- New V7 Constraints Tests ---

def test_primitive_in_assembly_rejected():
    # Assemblies cannot have primitive bodies added, only components
    ledger = Ledger.model_validate({"project_id": "prj_1", "assembly": {"parts": [], "components": [], "joints": []}, "features": []})
    session = DummySession(ledger)
    cmd = DesignCommand.model_validate({
        "type": "design.command", "message_id": "m1", "base_revision": 1, "ledger_id": "prj_1", "scope": "source",
        "operations": [{"op": "add_feature", "kind": "box", "name": "Box", "operation": "add", "placement": {"origin": [0,0,0]}, "parameters": {"length": 10, "width": 10, "height": 10}}]
    })
    with pytest.raises(CadenzaError, match="cannot contain primitives|Cannot add solid primitives"):
        compile_command(session, cmd)

def test_remove_last_solid_rejected():
    ledger = Ledger.model_validate({
        "project_id": "prj_1",
        "features": [{"id": "feat_1", "kind": "box", "operation": "add", "placement": {"origin": [0,0,0]}, "parameters": {"length": 10, "width": 10, "height": 10}}]
    })
    session = DummySession(ledger)
    cmd = DesignCommand.model_validate({
        "type": "design.command", "message_id": "m1", "base_revision": 1, "ledger_id": "prj_1", "scope": "source",
        "operations": [{"op": "remove_feature", "feature_id": "feat_1"}]
    })
    with pytest.raises(CadenzaError, match="Cannot remove or suppress the last solid feature"):
        compile_command(session, cmd)

def test_suppress_with_dependents_rejected():
    # To test explicit dependency: Extrude uses sketch as target.
    ledger = Ledger.model_validate({
        "project_id": "prj_1",
        "features": [
            {"id": "feat_1", "kind": "box", "operation": "add", "placement": {"origin": [0,0,0]}, "parameters": {"length": 10, "width": 10, "height": 10}},
            {"id": "feat_sk1", "kind": "sketch", "operation": "add", "placement": {"origin": [0,0,0]}, "parameters": {"vertices": [[0,0], [1,0], [1,1]], "height": 0}},
            {"id": "feat_ext1", "kind": "extrude", "operation": "add", "placement": {"origin": [0,0,0]}, "parameters": {"target_feature": "feat_sk1", "amount": 10}}
        ]
    })
    session = DummySession(ledger)
    cmd = DesignCommand.model_validate({
        "type": "design.command", "message_id": "m1", "base_revision": 1, "ledger_id": "prj_1", "scope": "source",
        "operations": [{"op": "set_suppressed", "feature_id": "feat_sk1", "suppressed": True}]
    })
    with pytest.raises(CadenzaError, match="suppressed feature"):
        compile_command(session, cmd)


def candidate(features, operations):
    from cadenza_backend.ledger_store import apply_patch
    ledger = Ledger.model_validate({"features": features})
    before = ledger.model_dump(mode="json")
    command = DesignCommand.model_validate(dict(message_id="test", base_revision=0,
        ledger_id=ledger.project_id, operations=operations))
    patches = compile_command(DummySession(ledger), command)
    result = apply_patch(ledger, patches).ledger
    assert ledger.model_dump(mode="json") == before
    return result


BOX = {"id": "feat_box", "kind": "box", "parameters": {"length": 20, "width": 20, "height": 10}}
HOLE = {"id": "feat_hole", "kind": "hole", "parameters": {"diameter": 2, "through": True},
        "placement": {"origin": [3, 0, 10]}}
PATTERN = {"id": "feat_pattern", "kind": "linear_pattern", "operation": "subtract",
           "parameters": {"target_feature": "feat_hole", "count": 2, "spacing": -6}}


@pytest.mark.parametrize("reverse", [False, True])
def test_suppress_and_restore_dependency_group_in_either_order(reverse):
    ids = ["feat_hole", "feat_pattern"]
    if reverse:
        ids.reverse()
    suppressed = candidate([BOX, HOLE, PATTERN], [dict(op="set_suppressed", feature_id=fid, suppressed=True) for fid in ids])
    assert [f.suppressed for f in suppressed.features] == [False, True, True]
    restored = candidate([f.model_dump(mode="json") for f in suppressed.features],
        [dict(op="set_suppressed", feature_id=fid, suppressed=False) for fid in ids])
    assert not any(f.suppressed for f in restored.features)


def test_restore_sole_suppressed_solid():
    restored = candidate([dict(BOX, suppressed=True)],
        [dict(op="set_suppressed", feature_id="feat_box", suppressed=False)])
    assert not restored.features[0].suppressed


@pytest.mark.parametrize("op", ["remove_feature", "set_suppressed"])
def test_profile_only_remainder_is_not_a_solid(op):
    sketch = {"id": "feat_sketch", "kind": "sketch", "parameters": {"vertices": [[0, 0], [2, 0], [2, 2]], "height": 0}}
    request: dict[str, object] = dict(op=op, feature_id="feat_box")
    if op == "set_suppressed":
        request["suppressed"] = True
    with pytest.raises(CadenzaError, match="last solid"):
        candidate([BOX, sketch], [request])


@pytest.mark.parametrize("reverse", [False, True])
def test_group_removal_preserves_base_and_rejects_dangling_reference(reverse):
    ids = ["feat_hole", "feat_pattern"]
    if reverse:
        ids.reverse()
    result = candidate([BOX, HOLE, PATTERN], [dict(op="remove_feature", feature_id=fid) for fid in ids])
    assert [f.id for f in result.features] == ["feat_box"]
    with pytest.raises(CadenzaError):
        candidate([BOX, HOLE, PATTERN], [dict(op="remove_feature", feature_id="feat_hole")])

def test_extrude_invalid_target_rejected():
    ledger = Ledger.model_validate({
        "project_id": "prj_1",
        "features": [{"id": "feat_1", "kind": "box", "operation": "add", "placement": {"origin": [0,0,0]}, "parameters": {"length": 10, "width": 10, "height": 10}}]
    })
    session = DummySession(ledger)
    # Target is feat_1, which is a box, not a profile/sketch
    cmd = DesignCommand.model_validate({
        "type": "design.command", "message_id": "m1", "base_revision": 1, "ledger_id": "prj_1", "scope": "source",
        "operations": [{"op": "add_feature", "kind": "extrude", "name": "Ext", "operation": "add", "placement": {"origin": [0,0,0]}, "parameters": {"target_feature": "feat_1", "amount": 10}}]
    })
    with pytest.raises(CadenzaError, match="Extrude target must be a profile or sketch"):
        compile_command(session, cmd)


def test_shared_driver_conflicts_and_bound_deletion_preserve_input():
    ledger = Ledger.model_validate({"features": [BOX], "relationships": {
        "parameters": {"size": {"value": 20, "unit": "mm"}},
        "bindings": [{"feature_id": "feat_box", "target": key, "expression": "size"} for key in ("length", "width")]}})
    before = ledger.model_dump(mode="json")
    for operations in ([dict(op="set_dimension", feature_id="feat_box", parameter="length", value=30),
                        dict(op="set_dimension", feature_id="feat_box", parameter="width", value=40)],
                       [dict(op="remove_feature", feature_id="feat_box")]):
        command = DesignCommand.model_validate(dict(message_id="test", base_revision=0,
            ledger_id=ledger.project_id, operations=operations))
        with pytest.raises(CadenzaError):
            compile_command(DummySession(ledger), command)
        assert ledger.model_dump(mode="json") == before


def test_real_command_origin_hole_suppression_and_history(tmp_path, monkeypatch):
    import asyncio
    import math
    from build123d import GeomType
    from cadenza_backend.agents.model_client import ModelClient
    from cadenza_backend.design import accept_design, build_design
    from cadenza_backend.design_commands import handle_design_command
    from cadenza_backend.edit_preview import decide_preview
    from cadenza_backend.projects import ProjectRepository
    from cadenza_backend.session import Session, LedgerStore
    from cadenza_contracts.messages import PreviewDecision
    from cadenza_contracts.project import ProjectDocument
    from cadenza_geometry.service import Build123dGeometryService

    async def no_ai(*args, **kwargs):
        pytest.fail("Feature commands must not call a model")
    monkeypatch.setattr(ModelClient, "complete_json", no_ai)
    ledger = Ledger.model_validate({"features": [BOX]})
    session = Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger=ledger)})

    async def discard(message):
        pass

    def preview(operations):
        before = session.snapshot(), list(session.history), len(session.artifacts), session.last_geometry
        request = DesignCommand.model_validate(dict(message_id="test", base_revision=session.revision,
            ledger_id=ledger.project_id, operations=operations))
        asyncio.run(handle_design_command(session, request, discard))
        assert before == (session.snapshot(), list(session.history), len(session.artifacts), session.last_geometry)
        assert session.preview is not None
        return session.preview

    def decide(action="apply"):
        assert session.preview is not None
        request = PreviewDecision.model_validate(dict(message_id="decide", base_revision=session.revision,
            preview_id=session.preview.id, action=action))
        asyncio.run(decide_preview(session, request, discard))
        return request

    addition = dict(op="add_feature", kind="hole", name="Bore", operation="subtract",
        placement={"origin": [3, 0, 10]}, parameters={"diameter": 2, "through": True})
    pending = preview([addition])
    assert pending.outcome.volume_mm3 == pytest.approx(4000 - 10 * math.pi)
    decide("cancel")
    assert len(session.store.ledger.features) == 1
    pending = preview([addition])
    hole_id = pending.ledgers[ledger.project_id].features[-1].id
    applied = decide()
    with pytest.raises(CadenzaError):
        asyncio.run(decide_preview(session, applied, discard))

    pending = preview([dict(op="set_origin", feature_id=hole_id, axis="y", value=2)])
    compiled = Build123dGeometryService()._compile(pending.ledgers[ledger.project_id], pending.ledgers)
    bores = [f for f in compiled.solid.faces() if f.geom_type == GeomType.CYLINDER]
    assert len(bores) == 1 and bores[0].radius == pytest.approx(1)
    axis = bores[0].axis_of_rotation
    assert axis is not None and (axis.position.X, axis.position.Y) == pytest.approx((3, 2))
    decide()
    for suppressed in (True, False):
        pending = preview([dict(op="set_suppressed", feature_id=hole_id, suppressed=suppressed)])
        assert pending.outcome.volume_mm3 == pytest.approx(4000 if suppressed else 4000 - 10 * math.pi)
        decide()
    pending = preview([dict(op="remove_feature", feature_id=hole_id)])
    assert pending.outcome.volume_mm3 == pytest.approx(4000)
    decide()
    undo = session.stage_undo()
    assert undo is not None
    ledgers, index = undo
    outcome = build_design(ledgers, session.primary_ledger_id)
    assert outcome.ok and outcome.volume_mm3 == pytest.approx(4000 - 10 * math.pi)
    asyncio.run(accept_design(session, ledgers, outcome, "undo", discard, undo_index=index))
    redo = session.stage_redo()
    assert redo is not None
    outcome = build_design(redo, session.primary_ledger_id)
    assert outcome.ok and outcome.volume_mm3 == pytest.approx(4000)
    asyncio.run(accept_design(session, redo, outcome, "redo", discard, redo=True))
    repository = ProjectRepository(tmp_path / "commands.sqlite3")
    document = ProjectDocument(name="Commands", primary_ledger_id=session.primary_ledger_id,
        ledgers=session.snapshot(), history=session.history, redo=session.redo)
    pid, _ = repository.save(document, None, 0)
    reopened, _ = ProjectRepository(tmp_path / "commands.sqlite3").load(pid)
    assert reopened == document
    assert build_design(reopened.ledgers, reopened.primary_ledger_id).volume_mm3 == pytest.approx(4000)


def test_failed_and_stale_commands_preserve_accepted_state(monkeypatch):
    import asyncio
    from cadenza_backend import design_commands
    from cadenza_backend.geometry_bridge import BuildOutcome
    from cadenza_backend.session import Session, LedgerStore
    ledger = Ledger.model_validate({"features": [BOX]})
    session = Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger=ledger)})
    before = session.snapshot(), list(session.history), len(session.artifacts)
    calls = []
    async def failed(ledgers, primary_id):
        calls.append(ledgers)
        return BuildOutcome(ok=False, detail="Deliberate failed rebuild", stub=False)
    async def unexpected(message):
        pytest.fail("Failed commands must not publish")
    monkeypatch.setattr(design_commands, "build_design", failed)
    for revision in (session.revision + 1, session.revision):
        request = DesignCommand.model_validate(dict(message_id="test", base_revision=revision, ledger_id=ledger.project_id,
            operations=[dict(op="set_dimension", feature_id="feat_box", parameter="width", value=30)]))
        with pytest.raises(CadenzaError):
            asyncio.run(design_commands.handle_design_command(session, request, unexpected))
        assert before == (session.snapshot(), list(session.history), len(session.artifacts))
        assert session.preview is None
    assert len(calls) == 1  # Stale request never reached the worker boundary.


def test_websocket_command_preview_and_busy_guards(monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from cadenza_backend import app as transport
    from cadenza_backend.session import Session, LedgerStore, registry
    ledger = Ledger.model_validate({"features": [BOX]})
    session = Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger=ledger)})
    registry.register(session)
    request = DesignCommand.model_validate(dict(message_id="test", base_revision=session.revision, ledger_id=ledger.project_id,
        operations=[dict(op="set_dimension", feature_id="feat_box", parameter="width", value=30)]))
    def receive(ws, kind):
        for _ in range(20):
            frame = ws.receive()
            if frame.get("text"):
                data = json.loads(frame["text"])
                if data["type"] in {kind, "error", "design.preview"}:
                    assert data["type"] == kind, data
                    return data
        pytest.fail(f"No {kind} response")
    try:
        with TestClient(transport.app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json(dict(type="client.hello", contract_version="1.0.0", session_id=session.id))
                receive(ws, "session.ready")
                # Synthetic draft presence isolates the admission guard from sketch authoring.
                monkeypatch.setattr(session, "draft", object())
                ws.send_json(request.model_dump(mode="json"))
                assert "sketch" in receive(ws, "error")["message"]
                monkeypatch.setattr(session, "draft", None)
                ws.send_json(request.model_dump(mode="json"))
                preview = receive(ws, "design.preview")
                ws.send_json(request.model_dump(mode="json"))
                assert "preview" in receive(ws, "error")["message"]
                assert session.preview is not None and session.preview.id == preview["preview_id"]
                ws.send_json(dict(type="design.preview_decision", message_id="cancel", base_revision=session.revision,
                    preview_id=preview["preview_id"], action="cancel"))
                receive(ws, "design.preview_closed")
                assert session.preview is None and session.store.ledger == ledger
    finally:
        registry.drop(session.id)
