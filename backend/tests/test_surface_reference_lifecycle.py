import asyncio

from fastapi.testclient import TestClient
import pytest

from cadenza_backend import app as transport, geometry_jobs as jobs
from cadenza_backend.design import accept_design
from cadenza_backend.projects import ProjectRepository
from cadenza_backend.session import LedgerStore, Session, registry
from cadenza_backend.tools.probe import GeometryProbeTool, ProbeQuery
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.orchestrator import handle_prompt
from cadenza_backend.wire import Target
from cadenza_contracts.patch import PatchOp


def test_reference_capture_edit_undo_redo_and_reopen(tmp_path, monkeypatch, plate_ledger):
    monkeypatch.setattr(transport, "projects", ProjectRepository(tmp_path / "projects.sqlite3"))
    runner = jobs.GeometryJobRunner(1, 20)
    monkeypatch.setattr(jobs, "runner", runner)
    session = Session(primary_ledger_id=plate_ledger.project_id,
                      stores={plate_ledger.project_id: LedgerStore(ledger=plate_ledger)})
    registry.register(session)
    tool = GeometryProbeTool()

    async def discard(_):
        pass

    async def query(current, kind, **kwargs):
        return await tool.execute_async(context={"ledgers": current.snapshot()}, query_type=kind, **kwargs)

    async def commit(ops=None, undo=False, redo=False):
        index = None
        if undo:
            proposed = session.stage_undo()
            assert proposed is not None
            candidate, index = proposed
        elif redo:
            candidate = session.stage_redo()
            assert candidate is not None
        else:
            assert ops is not None
            staged = session.store.stage(ops)
            candidate = session.stage({session.primary_ledger_id: staged.ledger})
        outcome = await jobs.build_design(candidate, session.primary_ledger_id)
        assert outcome.ok and not outcome.stub
        await accept_design(session, candidate, outcome, "msg_reference", discard, undo_index=index, redo=redo)

    try:
        captured = asyncio.run(query(session, "capture_surface_reference", point=[20, 20, 20], normal=[0, 0, 1]))
        assert captured["status"] == "resolved"
        reference = captured["reference"]
        asyncio.run(commit([PatchOp(op="add", path="/surface_references/ref_plate_top", value=reference)]))
        asyncio.run(commit([PatchOp(op="replace", path=f"/features/{plate_ledger.features[0].id}/parameters/height", value=40)]))
        for action, height in ((None, 40), ("undo", 20), ("redo", 40)):
            if action:
                asyncio.run(commit(undo=action == "undo", redo=action == "redo"))
            result = asyncio.run(query(session, "resolve_surface_reference", reference_id="ref_plate_top"))
            assert result["status"] == "resolved" and result["face"]["center"][2] == pytest.approx(height)
            assert result["reference"] == reference

        with TestClient(transport.app) as client:
            saved = client.post(f"/api/sessions/{session.id}/save", json={"name": "Referenced plate", "base_revision": session.revision})
            assert saved.status_code == 200
            registry.drop(session.id)
            opened = client.post(f"/api/projects/{saved.json()['id']}/open")
            assert opened.status_code == 200
            restored = registry.get(opened.json()["session_id"])
            assert restored is not None
            result = asyncio.run(query(restored, "resolve_surface_reference", reference_id="ref_plate_top"))
            assert result["status"] == "resolved" and result["face"]["center"][2] == pytest.approx(40)
            assert result["reference"] == reference and restored.history == session.history
            # Delete all geometry through the same validated transaction; the
            # saved reference persists and the project remains reopenable.
            session = restored
            asyncio.run(commit([PatchOp(op="remove", path=f"/features/{feature.id}")
                                for feature in reversed(session.store.ledger.features)]))
            result = asyncio.run(query(session, "resolve_surface_reference", reference_id="ref_plate_top"))
            assert result["status"] == "missing" and "face" not in result
            saved = client.post(f"/api/sessions/{session.id}/save", json={"name": "Unresolved plate", "base_revision": session.revision})
            assert saved.status_code == 200
            opened = client.post(f"/api/projects/{saved.json()['id']}/open")
            assert opened.status_code == 200
            restored = registry.get(opened.json()["session_id"])
            assert restored is not None
            assert asyncio.run(query(restored, "resolve_surface_reference", reference_id="ref_plate_top"))["status"] == "missing"
    finally:
        runner.close()


@pytest.mark.parametrize("kwargs", [
    {"query_type": "capture_surface_reference"},
    {"query_type": "capture_surface_reference", "point": [0, 0, float("nan")]},
    {"query_type": "capture_surface_reference", "point": [0, 0, 0], "feature_1_id": "feat_a"},
    {"query_type": "resolve_surface_reference"},
    {"query_type": "resolve_surface_reference", "reference_id": "ref_a", "point": [0, 0, 0]},
    {"query_type": "get_face_center", "reference_id": "ref_a"},
])
def test_reference_tool_rejects_incomplete_or_mixed_queries(kwargs):
    with pytest.raises(ValueError):
        ProbeQuery.model_validate(kwargs)


def test_selected_surface_can_be_saved_through_actual_graph(monkeypatch, plate_ledger):
    session = Session(primary_ledger_id=plate_ledger.project_id,
                      stores={plate_ledger.project_id: LedgerStore(ledger=plate_ledger)})
    tool = GeometryProbeTool()
    async def complete(_self, **kwargs):
        assert kwargs["stage"] == "machinist"
        assert f"Resolved surface in ledger {plate_ledger.project_id}" in kwargs["user"]
        assert "point=[20.0, 20.0, 20.0]" in kwargs["user"]
        result = await tool.execute_async(context=kwargs["context"], query_type="capture_surface_reference",
                                          ledger_id=plate_ledger.project_id, point=[20, 20, 20], normal=[0, 0, 1])
        assert result["status"] == "resolved"
        return {"summary": "Remembered the plate top.", "patch": [{"op": "add",
            "ledger_id": plate_ledger.project_id, "path": "/surface_references/ref_plate_top", "value": result["reference"]}]}
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    events = []
    async def emit(event):
        events.append(event)
    asyncio.run(handle_prompt(session, "msg_remember", "Remember this face as the plate top.",
        [Target(point=[20, 20, 20], normal=[0, 0, 1])], emit))
    assert session.store.ledger.surface_references["ref_plate_top"].role == "top_face"
    assert session.revision == plate_ledger.revision + 1
    assert any(event.type == "ledger.updated" for event in events)
