import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cadenza_backend import app as transport
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.geometry_bridge import probe
from cadenza_backend.orchestrator import handle_prompt
from cadenza_backend.session import Session, LedgerStore, registry
from cadenza_backend.design import accept_design
from cadenza_backend.geometry_jobs import build_design
from cadenza_backend.projects import ProjectRepository
from cadenza_backend.edit_preview import decide_preview
from cadenza_backend.surface_references import resolve_surface_target
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.messages import Target, PreviewDecision
from cadenza_contracts.surface_reference import SurfaceReference
from cadenza_geometry.service import Build123dGeometryService


@pytest.fixture
def design():
    raw = json.loads((Path(__file__).parents[2] / "shared/contract_fixtures/profile_plate.json").read_text())
    ledger = Ledger.model_validate(raw)
    service = Build123dGeometryService()
    ref = SurfaceReference.model_validate(service.run_probe_query(ledger,
        {"query_type": "capture_surface_reference", "point": [10, 0, 4]})["reference"])
    ledger.surface_references["ref_opening"] = ref
    session = Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger=ledger)})
    return session, ref


def prompt(session, target, text="Enlarge this opening to 30 mm"):
    events = []
    async def emit(event): events.append(event)
    asyncio.run(handle_prompt(session, "msg_selected", text, [target], emit, preview=True))
    return events


def test_selected_prompt_uses_entity_constraint_and_named_driver(design, monkeypatch):
    session, ref = design
    async def complete(_self, **kwargs):
        assert kwargs["stage"] == "machinist"
        assert "entity ent_bore" in kwargs["user"] and "con_diameter" in kwargs["user"] and "bore_diameter" in kwargs["user"]
        assert "9999" not in kwargs["user"]
        return {"summary": "Enlarge the selected bore", "patch": [{"op": "replace", "path": "/relationships/parameters/bore_diameter/value", "value": 30}]}
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    before = session.snapshot()
    events = prompt(session, Target(point=[9999, 0, 0], surface_reference=ref))
    assert session.preview is not None and session.snapshot() == before, [e.model_dump() for e in events if getattr(e, "type", None) in {"error", "geometry.failed"}]
    async def discard(_): pass
    asyncio.run(decide_preview(session, PreviewDecision(message_id="msg_apply", base_revision=0, preview_id=session.preview.id, action="apply"), discard))
    feature = session.store.ledger.features[0]
    assert feature.kind == "profile" and feature.parameters.solution is not None
    assert feature.parameters.solution.entities[1].diameter == 30
    assert feature.parameters.solution.entities[1].x == feature.parameters.solution.entities[1].y == 0
    assert asyncio.run(resolve_surface_target(session.snapshot(), ref)).feature_id == "feat_extrusion"
    reopened = Ledger.model_validate_json(session.store.ledger.model_dump_json())
    assert reopened.surface_references["ref_opening"] == ref


def test_coordinate_probe_describes_source_entity(design):
    session, _ = design
    hit = probe(session.store.ledger, [10, 0, 4], [-1, 0, 0])
    assert hit.feature_id == "feat_extrusion" and "entity ent_bore" in hit.descriptor
    assert "con_diameter" in hit.descriptor


def test_saved_profile_identity_survives_history_and_http_reopen(design, monkeypatch, tmp_path):
    session, ref = design
    async def discard(_): pass
    async def accept(ledgers, **kwargs):
        outcome = await build_design(ledgers, session.primary_ledger_id)
        assert outcome.ok, outcome.detail
        await accept_design(session, ledgers, outcome, "msg_history", discard, **kwargs)
    changed = session.store.ledger.model_dump(mode="json")
    changed["relationships"]["parameters"]["bore_diameter"]["value"] = 30
    asyncio.run(accept(session.stage({session.primary_ledger_id: Ledger.model_validate(changed)})))
    undo = session.stage_undo()
    assert undo is not None
    asyncio.run(accept(undo[0], undo_index=undo[1]))
    assert session.store.ledger.relationships.parameters["bore_diameter"].value == 20
    assert asyncio.run(resolve_surface_target(session.snapshot(), ref)).feature_id == ref.feature_id
    redo = session.stage_redo()
    assert redo is not None
    asyncio.run(accept(redo, redo=True))
    monkeypatch.setattr(transport, "projects", ProjectRepository(tmp_path / "identity.sqlite3"))
    registry.register(session)
    try:
        with TestClient(transport.app) as client:
            saved = client.post(f"/api/sessions/{session.id}/save", json={"name": "Selected profile", "base_revision": session.revision})
            assert saved.status_code == 200
            opened = client.post(f"/api/projects/{saved.json()['id']}/open")
            assert opened.status_code == 200
            restored = registry.get(opened.json()["session_id"])
            assert restored is not None
            try:
                assert restored.store.ledger.surface_references["ref_opening"] == ref
                assert restored.store.ledger.relationships.parameters["bore_diameter"].value == 30
                response = client.post(f"/api/sessions/{restored.id}/surface-references/resolve",
                    json={"ledger_id": restored.primary_ledger_id, "reference_id": "ref_opening", "base_revision": restored.revision})
                assert response.status_code == 200 and response.json()["result"]["status"] == "resolved"
            finally: registry.drop(restored.id)
    finally: registry.drop(session.id)


def test_wrong_selected_diameter_patch_is_rejected(design, monkeypatch):
    session, ref = design
    async def complete(_self, **kwargs):
        return {"summary": "Wrong dimension", "patch": [{"op": "replace", "path": "/relationships/parameters/plate_width/value", "value": 130}]}
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    before = session.snapshot()
    events = prompt(session, Target(point=[10, 0, 4], surface_reference=ref))
    assert any(getattr(e, "code", None) == "PATCH_INVALID" for e in events)
    assert session.preview is None and session.snapshot() == before


@pytest.mark.parametrize("field,value", [("entity_id", "ent_deleted"), ("profile_id", "feat_deleted"), ("feature_id", "feat_deleted")])
def test_stale_identity_never_falls_back_to_coordinates(design, monkeypatch, field, value):
    session, ref = design
    async def no_ai(*args, **kwargs): pytest.fail("Invalid selection reached the model")
    monkeypatch.setattr(ModelClient, "complete_json", no_ai)
    ref = SurfaceReference.model_validate({**ref.model_dump(), field: value})
    before = session.snapshot()
    events = prompt(session, Target(point=[10, 0, 4], surface_reference=ref))
    assert any(getattr(e, "code", None) == "NO_HIT" for e in events)
    assert session.preview is None and session.snapshot() == before


def test_cap_and_instance_scope_need_clarification(design, monkeypatch):
    session, ref = design
    async def no_ai(*args, **kwargs): pytest.fail("Ambiguous scope reached the model")
    monkeypatch.setattr(ModelClient, "complete_json", no_ai)
    cap = SurfaceReference.model_validate({**ref.model_dump(), "role": "top_face", "entity_id": None})
    events = prompt(session, Target(point=[20, 0, 8], surface_reference=cap), "Make this bigger")
    assert any(getattr(e, "code", None) == "AGENT_NEEDS_CLARIFICATION" for e in events)
    root = Ledger.model_validate({"assembly": {"components": [{"id": "comp_left", "sub_assembly_id": ref.source_ledger_id}]}})
    session.stores[root.project_id] = LedgerStore(ledger=root)
    session.primary_ledger_id = root.project_id
    instance = SurfaceReference.model_validate({**ref.model_dump(), "ledger_id": root.project_id, "component_path": ["comp_left"]})
    events = prompt(session, Target(point=[10, 0, 4], surface_reference=instance))
    assert any(getattr(e, "code", None) == "AGENT_NEEDS_CLARIFICATION" for e in events)
    events = prompt(session, Target(point=[10, 0, 4], surface_reference=instance), "Change this hole, not all instances")
    assert any(getattr(e, "code", None) == "AGENT_NEEDS_CLARIFICATION" for e in events)
    calls = []
    async def confirmed(_self, **kwargs):
        calls.append(kwargs)
        assert "entity ent_bore" in kwargs["user"] and "comp_left" in kwargs["user"]
        return {"summary": "Need a dimension", "patch": [], "needs_clarification": "What diameter should all instances use?"}
    monkeypatch.setattr(ModelClient, "complete_json", confirmed)
    prompt(session, Target(point=[10, 0, 4], surface_reference=instance), "All instances: enlarge this opening")
    assert len(calls) == 1 and session.preview is None


@pytest.mark.parametrize("change", [{"profile_id": None}, {"entity_id": None}, {"entity_id": "face_7"}, {"feature_kind": "box"}])
def test_incomplete_profile_identity_rejected(design, change):
    _, ref = design
    with pytest.raises(ValueError): SurfaceReference.model_validate({**ref.model_dump(), **change})
