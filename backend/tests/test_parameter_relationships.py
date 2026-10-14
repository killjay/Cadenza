import asyncio
import json
import math
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from cadenza_backend import app as transport
from cadenza_backend.agents.draftsman import run_draftsman
from cadenza_backend.agents.model_client import ModelClient, AIError
from cadenza_backend.design import accept_design
from cadenza_backend.edit_preview import preview_parameter, decide_preview
from cadenza_backend.geometry_jobs import build_design
from cadenza_backend.orchestrator import handle_prompt
from cadenza_backend.projects import ProjectRepository
from cadenza_backend.session import Session, LedgerStore, registry
from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.messages import NamedParameterEdit, ParameterEdit, PreviewDecision


def plate_data():
    return json.loads((Path(__file__).parents[2] / "shared/contract_fixtures/parameter_plate.json").read_text())


async def discard(_):
    pass


def named(session, name="plate_width", value=140):
    return NamedParameterEdit(message_id="msg_named", base_revision=session.revision,
        ledger_id=session.primary_ledger_id, parameter=name, value=value)


def decide(session, action="apply"):
    assert session.preview is not None
    return PreviewDecision.model_validate({"message_id": "msg_decide", "base_revision": session.revision,
        "preview_id": session.preview.id, "action": action})


@pytest.fixture
def design():
    ledger = Ledger.model_validate(plate_data())
    session = Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger=ledger)})
    async def seed():
        candidate = session.stage({})
        await accept_design(session, candidate, await build_design(candidate, session.primary_ledger_id), "msg_seed", discard)
    asyncio.run(seed())
    return session


def assert_plate(ledger, width):
    plate, *holes = ledger.features
    assert plate.parameters.width == width
    for hole in holes:
        assert width / 2 - abs(hole.placement.origin[1]) == 15
        assert plate.parameters.length / 2 - abs(hole.placement.origin[0]) == 15
        assert hole.parameters.diameter == 6


def test_real_numeric_prompt_parity_and_persisted_history(design, monkeypatch, tmp_path):
    before = design.snapshot()
    async def no_ai(*args, **kwargs):
        pytest.fail("Numeric editing must not call a provider")
    monkeypatch.setattr(ModelClient, "complete_json", no_ai)
    asyncio.run(preview_parameter(design, named(design), discard))
    preview = design.preview
    assert preview is not None
    assert_plate(preview.ledgers[design.primary_ledger_id], 140)
    assert preview.outcome.volume_mm3 == pytest.approx(120 * 140 * 10 - 4 * math.pi * 3**2 * 10)
    assert sum("Distinct bore" in check for check in preview.message.checks) == 4
    assert design.snapshot() == before
    expected = preview.ledgers
    asyncio.run(decide_preview(design, decide(design, "cancel"), discard))
    # The existing feature dimension control follows its simple binding too.
    direct = ParameterEdit(message_id="msg_feature", base_revision=design.revision,
        ledger_id=design.primary_ledger_id, feature_id="feat_plate", parameter="width", value=140)
    asyncio.run(preview_parameter(design, direct, discard))
    assert design.preview is not None and design.preview.ledgers == expected
    asyncio.run(decide_preview(design, decide(design, "cancel"), discard))
    async def complete(_self, **kwargs):
        if kwargs["stage"] == "architect":
            return {"is_edit": True, "is_assembly": False, "summary": "Resize plate"}
        assert "PERSISTENT PARAMETER RELATIONSHIPS" in kwargs["agent_card"].system_prompt
        return {"summary": "Resize plate with fixed margins", "patch": [{"op": "replace",
            "path": "/relationships/parameters/plate_width/value", "value": 140}]}
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    asyncio.run(handle_prompt(design, "msg_prompt", "Set width to 140 mm", [], discard, preview=True))
    assert design.preview is not None and design.preview.ledgers == expected
    asyncio.run(decide_preview(design, decide(design), discard))
    repository = ProjectRepository(tmp_path / "relationships.sqlite3")
    monkeypatch.setattr(transport, "projects", repository)
    registry.register(design)
    with TestClient(transport.app) as client:
        saved = client.post(f"/api/sessions/{design.id}/save", json={"name": "Parametric plate", "base_revision": design.revision})
        assert saved.status_code == 200
        opened = client.post(f"/api/projects/{saved.json()['id']}/open")
        assert opened.status_code == 200
        restored = registry.get(opened.json()["session_id"])
        assert restored is not None
        assert_plate(restored.store.ledger, 140)
        assert restored.store.ledger.relationships == design.store.ledger.relationships
        undo = restored.stage_undo()
        assert undo is not None
        ledgers, index = undo
        asyncio.run(accept_design(restored, ledgers, asyncio.run(build_design(ledgers, restored.primary_ledger_id)),
            "msg_undo", discard, undo_index=index))
        assert_plate(restored.store.ledger, 100)
        redo = restored.stage_redo()
        assert redo is not None
        asyncio.run(accept_design(restored, redo, asyncio.run(build_design(redo, restored.primary_ledger_id)), "msg_redo", discard, redo=True))
        assert_plate(restored.store.ledger, 140)
        registry.drop(restored.id)
    registry.drop(design.id)


@pytest.mark.parametrize("name,value", [("plate_width", 35), ("edge_margin", 2), ("half_y", 40), ("missing", 1)])
def test_invalid_numeric_edit_preserves_accepted_model(design, name, value):
    before, history = design.snapshot(), list(design.history)
    artifacts = design.last_geometry
    with pytest.raises(CadenzaError):
        asyncio.run(preview_parameter(design, named(design, name, value), discard))
    assert design.preview is None and design.snapshot() == before
    assert design.history == history and design.last_geometry is artifacts


def drafted_data():
    raw = plate_data()
    names = {f["id"]: f["name"] for f in raw["features"]}
    for binding in raw["relationships"]["bindings"]:
        binding["feature_id"] = names[binding["feature_id"]]
    return {"summary": "Create a plate with fixed centre margins", "features": raw["features"], "relationships": raw["relationships"]}


def test_prompt_creation_preserves_relationships(monkeypatch):
    ledger = Ledger()
    session = Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger=ledger)})
    async def complete(_self, **kwargs):
        if kwargs["stage"] == "architect":
            return {"is_edit": False, "is_assembly": False, "summary": "Create plate"}
        assert kwargs["stage"] == "draftsman"
        assert "relationships" in kwargs["agent_card"].output_schema["properties"]
        return drafted_data()
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    events = []
    async def emit(event): events.append(event)
    asyncio.run(handle_prompt(session, "msg_create", "Create a plate with four holes and fixed 15 mm centre margins", [], emit, preview=True))
    assert session.preview is not None, [e.model_dump() for e in events]
    candidate = session.preview.ledgers[session.primary_ledger_id]
    assert_plate(candidate, 100)
    assert all(b.feature_id in {f.id for f in candidate.features} for b in candidate.relationships.bindings)
    assert not session.store.ledger.features


def test_ambiguous_drafted_binding_is_rejected(monkeypatch):
    output = drafted_data()
    output["features"][1]["name"] = "Plate"
    async def complete(*args, **kwargs): return output
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    with pytest.raises(AIError, match="exactly one"):
        asyncio.run(run_draftsman("Create a parametric plate"))


def test_prompt_adds_holes_with_persistent_relationships(monkeypatch):
    raw = plate_data()
    ledger = Ledger.model_validate({"features": raw["features"][:1]})
    session = Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger=ledger)})
    async def complete(_self, **kwargs):
        if kwargs["stage"] == "architect":
            return {"is_edit": True, "is_assembly": False, "summary": "Add holes"}
        patch = [{"op": "add", "path": "/features/-", "value": hole} for hole in raw["features"][1:]]
        patch.append({"op": "add", "path": "/relationships", "value": raw["relationships"]})
        return {"summary": "Add four holes preserving edge margins", "patch": patch}
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    asyncio.run(handle_prompt(session, "msg_holes", "Add four mounting holes with 15 mm centre margins", [], discard, preview=True))
    assert session.preview is not None
    assert_plate(session.preview.ledgers[session.primary_ledger_id], 100)
    asyncio.run(decide_preview(session, decide(session), discard))
    asyncio.run(preview_parameter(session, named(session), discard))
    assert session.preview is not None
    assert_plate(session.preview.ledgers[session.primary_ledger_id], 140)


def test_websocket_named_parameter_frame(design):
    registry.register(design)
    def receive(ws, kind):
        while True:
            message = ws.receive()
            if message.get("text"):
                frame = json.loads(message["text"])
                if frame["type"] == "error": pytest.fail(str(frame))
                if frame["type"] == kind: return frame
    with TestClient(transport.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "client.hello", "contract_version": "1.0.0", "session_id": design.id})
            receive(ws, "session.ready")
            ws.send_json(named(design).model_dump(mode="json"))
            preview = receive(ws, "design.preview")
            assert any(c["path"] == "/relationships/parameters/plate_width/value" for c in preview["changes"])
            ws.send_json(decide(design).model_dump(mode="json"))
            receive(ws, "design.preview_closed")
            assert_plate(design.store.ledger, 140)
    registry.drop(design.id)
