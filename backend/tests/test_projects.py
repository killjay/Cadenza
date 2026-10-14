import asyncio
from unittest.mock import AsyncMock
import sqlite3

import pytest
from fastapi.testclient import TestClient

from cadenza_backend import app as transport, geometry_bridge
from cadenza_backend.design import accept_design
from cadenza_backend.projects import ProjectRepository, SaveConflict
from cadenza_backend.session import Session, registry
from cadenza_contracts.project import ProjectDocument, load_document
from cadenza_contracts.ledger import Ledger


async def discard(_):
    pass


def accept(session, ledgers, **kwargs):
    asyncio.run(accept_design(session, ledgers,
        geometry_bridge.BuildOutcome(ok=True, detail="fixture", stub=False, empty=True),
        "msg_test", discard, **kwargs))


def document(session):
    return ProjectDocument(name="Design", primary_ledger_id=session.primary_ledger_id,
        ledgers=session.snapshot(), history=session.history, redo=session.redo)


def test_repository_restart_conflict_and_failed_write(tmp_path):
    repo = ProjectRepository(tmp_path / "projects.sqlite3")
    doc = document(Session())
    pid, version = repo.save(doc, None, 0)
    restarted = ProjectRepository(repo.path)
    assert restarted.load(pid) == (doc, version)
    assert restarted.list()[0]["id"] == pid
    with pytest.raises(SaveConflict):
        restarted.save(doc.model_copy(update={"name": "stale"}), pid, 0)
    with repo.connect() as db:
        db.execute("CREATE TRIGGER fail_save BEFORE UPDATE ON projects BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
    with pytest.raises(sqlite3.Error):
        repo.save(doc.model_copy(update={"name": "failed"}), pid, version)
    assert restarted.load(pid) == (doc, version)


def test_legacy_migration_and_invalid_documents():
    session = Session()
    raw = session.store.ledger.model_dump(mode="json")
    migrated = load_document(raw)
    assert migrated.ledgers[session.primary_ledger_id] == session.store.ledger
    assert "document_version" not in raw
    for changes in ({"document_version": 99}, {"primary_ledger_id": "missing"}):
        with pytest.raises(ValueError):
            load_document({**document(session).model_dump(), **changes})


def test_redo_multiple_undo_and_branching():
    session = Session()
    for _ in range(3):
        accept(session, session.stage({}))
    candidate = session.stage_undo(1)
    assert candidate is not None
    ledgers, index = candidate
    accept(session, ledgers, undo_index=index)
    assert [s[session.primary_ledger_id].revision for s in session.redo] == [3, 2]
    redo = session.stage_redo()
    assert redo is not None
    accept(session, redo, redo=True)
    assert session.revision == 5 and len(session.redo) == 1
    accept(session, session.stage({}))
    assert session.revision == 6 and session.stage_redo() is None


def test_save_open_preserves_history_and_redo(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "projects", ProjectRepository(tmp_path / "projects.sqlite3"))
    with TestClient(transport.app) as client:
        session = registry.create()
        accept(session, session.stage({}))
        undo = session.stage_undo()
        assert undo is not None
        accept(session, undo[0], undo_index=undo[1])
        response = client.post(f"/api/sessions/{session.id}/save", json={"name": "Saved design", "base_revision": 2})
        assert response.status_code == 200
        pid = response.json()["id"]
        registry.drop(session.id)
        monkeypatch.setattr(transport, "projects", ProjectRepository(transport.projects.path))
        opened = client.post(f"/api/projects/{pid}/open")
        assert opened.status_code == 200
        restored = registry.get(opened.json()["session_id"])
        assert restored is not None
        assert restored.redo == session.redo and restored.history == session.history
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "client.hello", "contract_version": "1.0.0", "session_id": restored.id})
            ready = ws.receive_json()
            assert ready["can_redo"] and ready["project_name"] == "Saved design"
            ws.receive_json()
            ws.send_json({"type": "session.redo", "message_id": "msg_redo", "base_revision": restored.revision})
            updated = ws.receive_json()
            assert updated["can_undo"] and not updated["can_redo"]
            assert ws.receive_json()["empty"]
        assert client.post(f"/api/sessions/{restored.id}/save", json={"name": "stale", "base_revision": 0}).status_code == 409


def test_failed_open_does_not_register_or_replace_session(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "projects", ProjectRepository(tmp_path / "projects.sqlite3"))
    current = registry.create()
    pid, _ = transport.projects.save(document(current), None, 0)
    before = len(registry)
    monkeypatch.setattr(transport, "build_design", AsyncMock(return_value=geometry_bridge.BuildOutcome(ok=False, detail="failure", stub=False)))
    with TestClient(transport.app) as client:
        assert client.post(f"/api/projects/{pid}/open").status_code == 422
    assert len(registry) == before and registry.get(current.id) is current


def test_assembly_dependencies_survive_storage(tmp_path, plate_ledger):
    assembly = Ledger.model_validate({"assembly": {"components": [
        {"id": "comp_plate", "sub_assembly_id": plate_ledger.project_id}]}})
    doc = ProjectDocument(name="Assembly", primary_ledger_id=assembly.project_id,
        ledgers={assembly.project_id: assembly, plate_ledger.project_id: plate_ledger})
    repo = ProjectRepository(tmp_path / "projects.sqlite3")
    pid, _ = repo.save(doc, None, 0)
    loaded, _ = repo.load(pid)
    assert loaded == doc
    outcome = asyncio.run(transport.build_design(loaded.ledgers, loaded.primary_ledger_id))
    assert outcome.ok and not outcome.stub


def test_failed_redo_preserves_state(tmp_path, monkeypatch):
    current = registry.create()
    accept(current, current.stage({}))
    undo = current.stage_undo()
    assert undo is not None
    accept(current, undo[0], undo_index=undo[1])
    before = document(current)
    monkeypatch.setattr(transport, "build_design", AsyncMock(return_value=geometry_bridge.BuildOutcome(ok=False, detail="failure", stub=False)))
    with TestClient(transport.app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "client.hello", "contract_version": "1.0.0", "session_id": current.id})
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "session.redo", "message_id": "msg_redo", "base_revision": current.revision})
        assert ws.receive_json()["type"] == "error"
    assert document(current) == before
