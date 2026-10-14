import asyncio

from fastapi.testclient import TestClient
import pytest

from cadenza_backend import app as transport, graph, surface_references as surfaces
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.contracts_bridge import ErrorCode
from cadenza_backend.geometry_jobs import GeometryJobError
from cadenza_backend.orchestrator import handle_prompt
from cadenza_backend.session import LedgerStore, Session, registry
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.messages import Target
from cadenza_contracts.surface_reference import SavedSurfaceTarget, SurfaceReference
from cadenza_geometry.service import Build123dGeometryService


@pytest.fixture
def saved_session(plate_ledger):
    captured = Build123dGeometryService().run_probe_query(plate_ledger,
        {"query_type": "capture_surface_reference", "point": [20, 20, 20], "normal": [0, 0, 1]})
    assert captured["status"] == "resolved"
    plate_ledger.surface_references["ref_top"] = SurfaceReference.model_validate(captured["reference"])
    session = Session(primary_ledger_id=plate_ledger.project_id,
                      stores={plate_ledger.project_id: LedgerStore(ledger=plate_ledger)})
    registry.register(session)
    yield session
    registry.drop(session.id)


def check(client, session, **changes):
    body = {"ledger_id": session.primary_ledger_id, "reference_id": "ref_top", "base_revision": session.revision}
    body.update(changes)
    return client.post(f"/api/sessions/{session.id}/surface-references/resolve", json=body)


def test_http_checks_are_read_only_and_revision_guarded(saved_session):
    session = saved_session
    before = session.snapshot()
    with TestClient(transport.app) as client:
        response = check(client, session)
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == session.id and data["revision"] == session.revision
        assert data["result"]["status"] == "resolved"
        assert data["result"]["face"]["center"][2] == pytest.approx(20)
        assert check(client, session, base_revision=session.revision + 1).status_code == 409
        assert check(client, session, reference_id="ref_unknown").status_code == 404
        assert check(client, session, ledger_id="unknown").status_code == 404
        assert check(client, session, reference_id="face_7").status_code == 422
        assert client.post(f"/api/sessions/{session.id}/surface-references/resolve",
            json={"ledger_id": session.primary_ledger_id, "reference_id": "ref_top"}).status_code == 422
        assert client.post("/api/sessions/unknown/surface-references/resolve", json={
            "ledger_id": session.primary_ledger_id, "reference_id": "ref_top", "base_revision": 0}).status_code == 404
        asyncio.run(session.lock.acquire())
        try:
            assert check(client, session).status_code == 409
        finally:
            session.lock.release()
    assert session.snapshot() == before and not session.history and not session.redo


@pytest.mark.parametrize("code,status", [(ErrorCode.GEOMETRY_TIMEOUT, 504), (ErrorCode.GEOMETRY_KERNEL_FAILURE, 503)])
def test_http_worker_failures_are_explicit(saved_session, monkeypatch, code, status):
    async def fail(*args):
        raise GeometryJobError(code, "Worker unavailable")
    monkeypatch.setattr(surfaces, "inspect", fail)
    with TestClient(transport.app) as client:
        response = check(client, saved_session)
        assert response.status_code == status
        assert response.json()["detail"] == "Worker unavailable"
    assert not saved_session.history


def run_prompt(session, target):
    events = []
    async def emit(event):
        events.append(event)
    asyncio.run(handle_prompt(session, "msg_saved_edit", "Make the saved surface taller", [target], emit))
    return events


def target_for(ledger):
    # Deliberately stale, remote coordinates must never override saved identity.
    return Target(point=[9999, 9999, -100], normal=[1, 0, 0],
        saved_reference=SavedSurfaceTarget(ledger_id=ledger.project_id, reference_id="ref_top"))


def test_prompt_resolves_current_secondary_ledger_before_model(saved_session, monkeypatch):
    owner = saved_session.store.ledger
    doc = owner.model_dump(mode="json")
    doc["features"][0]["parameters"]["height"] = 40
    owner = Ledger.model_validate(doc)
    # The primary document is deliberately different: coordinates are in the
    # saved reference owner's frame, not automatically the primary frame.
    session = Session()
    session.stores[owner.project_id] = LedgerStore(ledger=owner)
    calls = []
    async def no_probe(*args, **kwargs):
        pytest.fail("Saved identity must not fall back to proximity")
    async def complete(_self, **kwargs):
        calls.append(kwargs)
        assert kwargs["stage"] == "machinist"
        assert f"Resolved surface in ledger {owner.project_id}" in kwargs["user"]
        assert "40.0]" in kwargs["user"] and "9999" not in kwargs["user"]
        assert f"source ledger: {owner.project_id}" in kwargs["user"]
        return {"summary": "Which height?", "needs_clarification": "Which height?", "patch": []}
    monkeypatch.setattr(graph, "probe", no_probe)
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    before = session.snapshot()
    events = run_prompt(session, target_for(owner))
    assert len(calls) == 1 and any(event.type == "agent.message" for event in events)
    assert session.snapshot() == before and not session.history


@pytest.mark.parametrize("change,expected", [("delete", "missing"), ("split", "ambiguous"), ("kind", "unsupported")])
def test_unresolved_checks_are_visible_and_block_prompt_before_provider(saved_session, monkeypatch, change, expected):
    session = saved_session
    doc = session.store.ledger.model_dump(mode="json")
    if change == "delete":
        doc["features"] = []
    elif change == "split":
        doc["features"].append({"id": "feat_split", "kind": "box", "operation": "subtract",
            "parameters": {"length": 10, "width": 120, "height": 10}, "placement": {"origin": [0, 0, 15]}})
    else:
        doc["features"][0] = {"id": doc["features"][0]["id"], "kind": "cylinder",
                              "parameters": {"diameter": 100, "height": 20}}
    session.stores[session.primary_ledger_id] = LedgerStore(ledger=Ledger.model_validate(doc))
    async def no_model(*args, **kwargs):
        pytest.fail("Unresolved saved targets must stop before provider calls")
    monkeypatch.setattr(ModelClient, "complete_json", no_model)
    with TestClient(transport.app) as client:
        response = check(client, session)
        assert response.status_code == 200
        assert response.json()["result"]["status"] == expected
        assert "face" not in response.json()["result"]
    before = session.snapshot()
    events = run_prompt(session, target_for(session.store.ledger))
    error = next(event for event in events if event.type == "error")
    assert error.code == ErrorCode.NO_HIT and expected in error.message
    assert session.snapshot() == before and not session.history


@pytest.mark.parametrize("identity", [{"ledger_id": "part", "reference_id": "face_7"},
                                      {"ledger_id": "", "reference_id": "ref_top"},
                                      {"ledger_id": "part"}])
def test_prompt_contract_rejects_incomplete_saved_identity(identity):
    with pytest.raises(ValueError):
        Target.model_validate({"point": [0, 0, 0], "saved_reference": identity})
