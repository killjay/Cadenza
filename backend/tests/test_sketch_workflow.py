import asyncio
import json
import math
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from cadenza_backend import app as transport
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.design import accept_design
from cadenza_backend.edit_preview import preview_parameter, decide_preview
from cadenza_backend.geometry_jobs import build_design
from cadenza_backend.orchestrator import handle_prompt
from cadenza_backend.projects import ProjectRepository
from cadenza_backend.session import Session, LedgerStore, registry
from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.ledger import Ledger, ProfileFeature
from cadenza_contracts.messages import SketchDimensionEdit, PreviewDecision


def raw_plate():
    return json.loads((Path(__file__).parents[2] / "shared/contract_fixtures/profile_plate.json").read_text())


async def discard(_): pass


@pytest.fixture
def design():
    ledger = Ledger.model_validate(raw_plate())
    session = Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger=ledger)})
    async def seed():
        candidate = session.stage({})
        await accept_design(session, candidate, await build_design(candidate, session.primary_ledger_id), "msg_seed", discard)
    asyncio.run(seed())
    return session


def dimension(session, value=140, constraint="con_width"):
    return SketchDimensionEdit(message_id="msg_dimension", base_revision=session.revision,
        ledger_id=session.primary_ledger_id, feature_id="feat_profile", constraint_id=constraint, value=value)


def decision(session, action="apply"):
    assert session.preview is not None
    return PreviewDecision.model_validate({"message_id": "msg_decision", "base_revision": session.revision,
        "preview_id": session.preview.id, "action": action})


def entities(ledger):
    profile = ledger.features[0]
    assert isinstance(profile, ProfileFeature) and profile.parameters.solution is not None
    assert profile.parameters.solution.status == "fully_constrained"
    return profile.parameters.solution.entities


def run_prompt(session, monkeypatch, prompt, patch):
    async def complete(_self, **kwargs):
        if kwargs["stage"] == "architect": return {"is_edit": True, "is_assembly": False, "summary": "Edit sketch"}
        assert "CONSTRAINED 2D PROFILES" in kwargs["agent_card"].system_prompt
        return {"summary": "Edit constrained profile", "patch": patch}
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    events = []
    async def emit(event): events.append(event)
    asyncio.run(handle_prompt(session, "msg_prompt", prompt, [], emit, preview=True))
    return events


def test_numeric_cancel_prompt_parity_and_shared_centre(design, monkeypatch):
    async def no_ai(*args, **kwargs): pytest.fail("Numeric sketch editing called a provider")
    monkeypatch.setattr(ModelClient, "complete_json", no_ai)
    before = design.snapshot()
    asyncio.run(preview_parameter(design, dimension(design), discard))
    assert design.preview is not None
    expected = design.preview.ledgers
    assert design.preview.outcome.volume_mm3 == pytest.approx((140 * 60 - math.pi * 100) * 8)
    assert design.snapshot() == before
    asyncio.run(decide_preview(design, decision(design, "cancel"), discard))
    run_prompt(design, monkeypatch, "Set width to 140 mm", [{"op": "replace", "path": "/relationships/parameters/plate_width/value", "value": 140}])
    assert design.preview is not None and design.preview.ledgers == expected
    asyncio.run(decide_preview(design, decision(design), discard))
    asyncio.run(preview_parameter(design, dimension(design, 30, "con_diameter"), discard))
    assert design.preview is not None
    rectangle, circle = entities(design.preview.ledgers[design.primary_ledger_id])
    assert rectangle.width == 140 and circle.diameter == 30
    assert circle.x == rectangle.x == 0 and circle.y == rectangle.y == 0


@pytest.mark.parametrize("constraint,value", [("con_width", 10), ("con_height", -1), ("con_diameter", 80), ("con_center", 10), ("con_missing", 10)])
def test_invalid_dimensions_preserve_state(design, constraint, value):
    before, history, geometry = design.snapshot(), list(design.history), design.last_geometry
    with pytest.raises(CadenzaError): asyncio.run(preview_parameter(design, dimension(design, value, constraint), discard))
    assert design.preview is None and design.snapshot() == before
    assert design.history == history and design.last_geometry is geometry


def test_conflicting_prompt_reports_constraint_ids(design, monkeypatch):
    before = design.snapshot()
    events = run_prompt(design, monkeypatch, "Propose an incompatible centre", [{"op": "add",
        "path": "/features/feat_profile/parameters/constraints/-", "value": {
        "id": "con_conflict", "kind": "dimension", "entity_id": "ent_bore", "parameter": "x", "value": 5}}])
    assert design.preview is None and design.snapshot() == before
    assert any("con_conflict" in str(event.model_dump()) for event in events)


def test_profile_creation_uses_minted_reference_ids(monkeypatch):
    session = Session()
    async def complete(_self, **kwargs):
        if kwargs["stage"] == "architect": return {"is_edit": False, "is_assembly": False, "summary": "Create profile part"}
        raw = raw_plate()
        raw["features"][1]["parameters"]["target_feature"] = "Plate sketch"
        names = {f["id"]: f["name"] for f in raw["features"]}
        for binding in raw["relationships"]["bindings"]: binding["feature_id"] = names[binding["feature_id"]]
        return {"summary": "Create rectangle with centred opening", "features": raw["features"], "relationships": raw["relationships"]}
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    events = []
    async def emit(event): events.append(event)
    asyncio.run(handle_prompt(session, "msg_create", "Create a rectangular profile with a centred circular opening and extrude 8 mm", [], emit, preview=True))
    assert session.preview is not None, [e.model_dump() for e in events]
    ledger = session.preview.ledgers[session.primary_ledger_id]
    extrusion = ledger.features[1]
    assert extrusion.kind == "extrude"
    assert extrusion.parameters.target_feature == ledger.features[0].id
    assert entities(ledger)[1].diameter == 20


def test_prompt_adds_a_sketch_opening_without_losing_constraints(design, monkeypatch):
    operations = [{"op": "add", "path": "/features/feat_profile/parameters/entities/-",
        "value": {"id": "ent_second", "kind": "circle", "operation": "subtract", "diameter": 10, "x": 25, "y": 0}}]
    operations.extend({"op": "add", "path": "/features/feat_profile/parameters/constraints/-",
        "value": {"id": f"con_second_{parameter}", "kind": "dimension", "entity_id": "ent_second", "parameter": parameter, "value": value}}
        for parameter, value in [("x", 25), ("y", 0), ("diameter", 10)])
    original = design.store.ledger.features[0].parameters.constraints
    run_prompt(design, monkeypatch, "Add one hole, diameter 10 mm, at x25 y0", operations)
    assert design.preview is not None
    result = design.preview.ledgers[design.primary_ledger_id].features[0]
    assert result.parameters.constraints[:len(original)] == original
    assert design.preview.outcome.volume_mm3 == pytest.approx((6000 - math.pi * 125) * 8)


def test_saved_reopen_history_and_sketch_websocket(design, monkeypatch, tmp_path):
    monkeypatch.setattr(transport, "projects", ProjectRepository(tmp_path / "sketch.sqlite3"))
    registry.register(design)
    def receive(ws, kind):
        while True:
            msg = ws.receive()
            if msg.get("text"):
                frame = json.loads(msg["text"])
                assert frame["type"] != "error", frame
                if frame["type"] == kind: return frame
    with TestClient(transport.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "client.hello", "contract_version": "1.0.0", "session_id": design.id})
            receive(ws, "session.ready")
            ws.send_json(dimension(design).model_dump(mode="json"))
            receive(ws, "design.preview")
            ws.send_json(decision(design).model_dump(mode="json"))
            receive(ws, "design.preview_closed")
        saved = client.post(f"/api/sessions/{design.id}/save", json={"name": "Sketch part", "base_revision": design.revision})
        assert saved.status_code == 200
        opened = client.post(f"/api/projects/{saved.json()['id']}/open")
        assert opened.status_code == 200
        restored = registry.get(opened.json()["session_id"])
        assert restored is not None and entities(restored.store.ledger)[0].width == 140
        undo = restored.stage_undo()
        assert undo is not None
        ledgers, index = undo
        asyncio.run(accept_design(restored, ledgers, asyncio.run(build_design(ledgers, restored.primary_ledger_id)), "msg_undo", discard, undo_index=index))
        assert entities(restored.store.ledger)[0].width == 100
        redo = restored.stage_redo()
        assert redo is not None
        asyncio.run(accept_design(restored, redo, asyncio.run(build_design(redo, restored.primary_ledger_id)), "msg_redo", discard, redo=True))
        assert entities(restored.store.ledger)[0].width == 140
        assert restored.store.ledger.relationships == design.store.ledger.relationships
        registry.drop(restored.id)
    registry.drop(design.id)
