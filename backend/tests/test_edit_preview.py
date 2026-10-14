import asyncio
import json
from pathlib import Path
from typing import Literal

from fastapi.testclient import TestClient
import pytest

from cadenza_backend import app as transport
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.contracts_bridge import CadenzaError, BoxFeature, Ledger
from cadenza_backend.design import accept_design
from cadenza_backend.edit_preview import decide_preview, preview_parameter
from cadenza_backend.geometry_jobs import build_design
from cadenza_backend.orchestrator import handle_prompt
from cadenza_backend.session import Session, LedgerStore, registry
from cadenza_backend.projects import ProjectRepository
from cadenza_contracts.messages import ParameterEdit, PreviewDecision


async def discard(_):
    pass


def height(ledger: Ledger) -> float:
    feature = ledger.features[0]
    assert isinstance(feature, BoxFeature)
    return feature.parameters.height


@pytest.fixture
def design(plate_ledger):
    session = Session(primary_ledger_id=plate_ledger.project_id,
        stores={plate_ledger.project_id: LedgerStore(ledger=plate_ledger)})
    async def seed():
        ledgers = session.stage({})
        await accept_design(session, ledgers, await build_design(ledgers, session.primary_ledger_id), "msg_seed", discard)
    asyncio.run(seed())
    return session


def dimension(session, value=40):
    return ParameterEdit(message_id="msg_dimension", base_revision=session.revision,
        ledger_id=session.primary_ledger_id, feature_id=session.store.ledger.features[0].id,
        parameter="height", value=value)


def decision(session, action: Literal["apply", "cancel"] = "apply", preview_id=None, base_revision=None):
    if preview_id is None:
        assert session.preview is not None
        preview_id = session.preview.id
    return PreviewDecision(message_id="msg_decide", base_revision=session.revision if base_revision is None else base_revision,
        preview_id=preview_id, action=action)


@pytest.mark.parametrize("fault", ["cleanup", "result_read"])
def test_worker_failure_cannot_publish_preview(design, monkeypatch, fault):
    from cadenza_backend import geometry_jobs as jobs
    before, history, redo = design.snapshot(), list(design.history), list(design.redo)
    revision, manifest = design.revision, design.last_geometry
    artifacts = [design.artifacts.get(revision, kind) for kind in ("glb", "step")]
    original_directory = jobs.TemporaryDirectory
    class FailingCleanup(original_directory):
        def cleanup(self):
            super().cleanup()
            raise PermissionError(13, "injected cleanup failure")
    original_read = Path.read_text
    def read(path, *args, **kwargs):
        if path.name == "result.json": raise PermissionError(13, "injected read denial")
        return original_read(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        if fault == "cleanup": patch.setattr(jobs, "TemporaryDirectory", FailingCleanup)
        else: patch.setattr(Path, "read_text", read)
        with pytest.raises(CadenzaError):
            asyncio.run(preview_parameter(design, dimension(design), discard))
    assert design.preview is None and design.snapshot() == before and design.revision == revision
    assert design.history == history and design.redo == redo and design.last_geometry == manifest
    assert [design.artifacts.get(revision, kind) for kind in ("glb", "step")] == artifacts
    asyncio.run(preview_parameter(design, dimension(design), discard))
    assert design.preview is not None and design.snapshot() == before


def test_numeric_preview_cancel_apply_and_double_apply(design, monkeypatch):
    async def no_ai(*args, **kwargs):
        pytest.fail("Numeric edits must not call a model")
    monkeypatch.setattr(ModelClient, "complete_json", no_ai)
    before, history = design.snapshot(), list(design.history)
    original_glb = design.artifacts.get(design.revision, "glb")
    asyncio.run(preview_parameter(design, dimension(design), discard))
    assert design.snapshot() == before and design.history == history
    assert design.artifacts.get(design.revision, "glb") is original_glb
    assert design.preview is not None
    changes = design.preview.message.changes
    assert len(changes) == 1 and changes[0].before == 20 and changes[0].after == 40
    assert design.preview.outcome.volume_mm3 > original_volume(design)
    asyncio.run(decide_preview(design, decision(design, "cancel"), discard))
    assert design.preview is None and design.snapshot() == before and design.history == history
    asyncio.run(preview_parameter(design, dimension(design), discard))
    apply = decision(design)
    asyncio.run(decide_preview(design, apply, discard))
    assert design.preview is None and design.store.ledger.features[0].parameters.height == 40
    assert design.store.ledger.features[1] == before[design.primary_ledger_id].features[1]
    assert len(design.history) == len(history) + 1
    with pytest.raises(CadenzaError):
        asyncio.run(decide_preview(design, apply, discard))


def original_volume(session):
    return session.last_geometry.stats.volume_mm3


@pytest.mark.parametrize("change", ["revision", "snapshot", "expired", "id"])
def test_stale_or_replaced_preview_cannot_commit(design, change):
    asyncio.run(preview_parameter(design, dimension(design), discard))
    request = decision(design)
    if change == "revision":
        request = request.model_copy(update={"base_revision": design.revision + 1})
    elif change == "snapshot":
        design.store.ledger.metadata.name = "Changed outside preview"
    elif change == "expired":
        design.preview.created -= 901
    else:
        request = request.model_copy(update={"preview_id": "different"})
    before = design.snapshot()
    with pytest.raises(CadenzaError):
        asyncio.run(decide_preview(design, request, discard))
    assert design.snapshot() == before


def run_prompt(session, monkeypatch, prompt, patch, **output):
    async def complete(_self, **kwargs):
        if kwargs["stage"] == "architect":
            return {"is_edit": True, "is_assembly": False, "summary": "Edit plate"}
        assert kwargs["stage"] == "machinist"
        return {"summary": "Proposed edit", "patch": patch, **output}
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    events = []
    async def emit(event):
        events.append(event)
    asyncio.run(handle_prompt(session, "msg_prompt", prompt, [], emit, preview=True))
    return events


def test_prompt_and_numeric_dimensions_produce_same_candidate(design, monkeypatch):
    before = design.snapshot()
    asyncio.run(preview_parameter(design, dimension(design), discard))
    assert design.preview is not None
    expected = design.preview.ledgers
    asyncio.run(decide_preview(design, decision(design, "cancel"), discard))
    patch = [{"op": "replace", "ledger_id": design.primary_ledger_id,
        "path": f"/features/{design.store.ledger.features[0].id}/parameters/height", "value": 40}]
    events = run_prompt(design, monkeypatch, "Make this plate 40 mm thick", patch)
    assert design.snapshot() == before and design.preview.ledgers == expected
    assert any(event.type == "design.preview" for event in events)
    assert not any(event.type == "ledger.updated" for event in events)
    assert any("height verified" in check for check in design.preview.message.checks)


def hole_ops(session, positions):
    return [{"op": "add", "ledger_id": session.primary_ledger_id, "path": "/features/-", "value": {
        "id": f"feat_mount_{i}", "kind": "hole", "operation": "subtract",
        "parameters": {"diameter": 6, "through": True}, "placement": {"origin": [x, y, 20]}}}
        for i, (x, y) in enumerate(positions)]


def test_four_hole_addition_resize_and_removal_preserve_base(design, monkeypatch):
    before = design.snapshot()
    run_prompt(design, monkeypatch, "Add four mounting holes", hole_ops(design, [(-25, -25), (-25, 25), (25, -25), (25, 25)]))
    assert design.preview is not None
    candidate = design.preview.ledgers[design.primary_ledger_id]
    assert len(candidate.features) == 6 and candidate.features[:2] == design.store.ledger.features
    assert sum("Distinct bore verified" in check for check in design.preview.message.checks) == 4
    assert design.snapshot() == before
    asyncio.run(decide_preview(design, decision(design), discard))
    for op in [{"op": "replace", "path": "/features/feat_mount_0/parameters/diameter", "value": 8},
               {"op": "remove", "path": "/features/feat_mount_0"}]:
        base = design.snapshot()
        run_prompt(design, monkeypatch, "Change this hole", [{**op, "ledger_id": design.primary_ledger_id}])
        assert design.preview is not None and design.snapshot() == base
        asyncio.run(decide_preview(design, decision(design), discard))
        assert design.store.ledger.features[:2] == before[design.primary_ledger_id].features
    assert len(design.store.ledger.features) == 5


@pytest.mark.parametrize("positions", [[(25, 25)], [(25, 25)] * 4, [(500, 500), (600, 600), (700, 700), (800, 800)]])
def test_wrong_count_overlapping_or_ineffective_holes_cannot_be_previewed(design, monkeypatch, positions):
    before = design.snapshot()
    events = run_prompt(design, monkeypatch, "Add four mounting holes", hole_ops(design, positions))
    assert design.preview is None and design.snapshot() == before
    assert any(event.type in {"error", "geometry.failed"} for event in events)


def test_wrong_requested_dimension_and_clarification_preserve_state(design, monkeypatch):
    patch = [{"op": "replace", "ledger_id": design.primary_ledger_id,
        "path": f"/features/{design.store.ledger.features[0].id}/parameters/height", "value": 30}]
    before = design.snapshot()
    events = run_prompt(design, monkeypatch, "Set thickness to 40 mm", patch)
    assert design.preview is None and any(event.type == "error" for event in events)
    events = run_prompt(design, monkeypatch, "Add mounting holes", patch, needs_clarification="What diameter and positions?")
    assert design.preview is None and design.snapshot() == before
    assert any(event.type == "agent.message" and event.needs_clarification for event in events)


def test_preview_artifacts_expire_and_never_replace_accepted_downloads(design):
    registry.register(design)
    asyncio.run(preview_parameter(design, dimension(design), discard))
    candidate = design.preview
    with TestClient(transport.app) as client:
        assert client.get(candidate.message.glb_url).content == candidate.outcome.glb
        assert client.get(candidate.message.step_url).content.startswith(b"ISO-10303-21")
        assert client.get(f"/api/sessions/{design.id}/previews/unknown/model.glb").status_code == 404
        asyncio.run(decide_preview(design, decision(design, "cancel"), discard))
        assert client.get(candidate.message.glb_url).status_code == 404
    registry.drop(design.id)


def test_invalid_numeric_edit_has_no_candidate(design):
    before = design.snapshot()
    request = dimension(design).model_copy(update={"parameter": "diameter"})
    with pytest.raises(CadenzaError):
        asyncio.run(preview_parameter(design, request, discard))
    assert design.preview is None and design.snapshot() == before
    for value in [0, -1, float("nan"), float("inf")]:
        with pytest.raises(ValueError):
            dimension(design, value)


def test_explicit_dimension_rejects_unrelated_changes(design, monkeypatch):
    feature = design.store.ledger.features[0]
    patch = [{"op": "replace", "ledger_id": design.primary_ledger_id,
        "path": f"/features/{feature.id}/parameters/{parameter}", "value": value}
        for parameter, value in [("height", 40), ("width", 200)]]
    before = design.snapshot()
    events = run_prompt(design, monkeypatch, "Make this plate 40 mm thick", patch)
    assert design.preview is None and design.snapshot() == before
    assert any(event.type == "error" and "unrelated" in event.message for event in events)


@pytest.mark.parametrize("denial", ["none", "transient", "persistent"])
def test_saved_document_excludes_pending_candidate_and_applied_edit_reopens(design, tmp_path, monkeypatch, denial):
    repository = ProjectRepository(tmp_path / "projects.sqlite3")
    monkeypatch.setattr(transport, "projects", repository)
    registry.register(design)
    asyncio.run(preview_parameter(design, dimension(design), discard))
    with TestClient(transport.app) as client:
        saved = client.post(f"/api/sessions/{design.id}/save", json={"name": "Plate", "base_revision": design.revision})
        assert saved.status_code == 200
        document, _ = repository.load(saved.json()["id"])
        assert height(document.ledgers[design.primary_ledger_id]) == 20
        assert "preview" not in document.model_dump()
        asyncio.run(decide_preview(design, decision(design), discard))
        saved = client.post(f"/api/sessions/{design.id}/save", json={"name": "Plate", "base_revision": design.revision})
        persisted = repository.load(saved.json()["id"])
        before = design.snapshot(), design.revision, list(design.history), list(design.redo), design.last_geometry
        artifacts = [design.artifacts.get(design.revision, kind) for kind in ("glb", "step")]
        original_read = Path.read_text
        reads = []
        def read(path, *args, **kwargs):
            if path.name == "result.json":
                reads.append(path)
                if denial == "persistent" or (denial == "transient" and len(reads) == 1):
                    raise PermissionError(13, "injected reopen read denial")
            return original_read(path, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", read)
            registered = set(registry._sessions)
            opened = client.post(f"/api/projects/{saved.json()['id']}/open")
        assert repository.load(saved.json()["id"]) == persisted
        assert (design.snapshot(), design.revision, list(design.history), list(design.redo), design.last_geometry) == before
        assert [design.artifacts.get(design.revision, kind) for kind in ("glb", "step")] == artifacts
        if denial == "persistent":
            assert opened.status_code == 422
            assert set(registry._sessions) == registered
            opened = client.post(f"/api/projects/{saved.json()['id']}/open")
        elif denial == "transient":
            assert len(reads) == 2 and len(set(reads)) == 1
        assert opened.status_code == 200
        restored = registry.get(opened.json()["session_id"])
        assert restored is not None and restored.preview is None
        assert height(restored.store.ledger) == 40
        candidate = restored.stage_undo()
        assert candidate is not None
        ledgers, index = candidate
        asyncio.run(accept_design(restored, ledgers, asyncio.run(build_design(ledgers, restored.primary_ledger_id)),
            "msg_undo", discard, undo_index=index))
        assert height(restored.store.ledger) == 20
        redo = restored.stage_redo()
        assert redo is not None
        asyncio.run(accept_design(restored, redo, asyncio.run(build_design(redo, restored.primary_ledger_id)),
            "msg_redo", discard, redo=True))
        assert height(restored.store.ledger) == 40
        registry.drop(restored.id)
    registry.drop(design.id)


def test_websocket_numeric_preview_stale_apply_cancel_and_reconnect(design):
    registry.register(design)
    def receive(ws, kind):
        while True:
            message = ws.receive()
            if message.get("text"):
                data = json.loads(message["text"])
                if data["type"] == kind:
                    return data
    with TestClient(transport.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "client.hello", "contract_version": "1.0.0", "session_id": design.id})
            receive(ws, "session.ready")
            ws.send_json(dimension(design).model_dump(mode="json"))
            preview = receive(ws, "design.preview")
            assert preview["base_revision"] == design.revision
            ws.send_json({"type": "user.prompt", "message_id": "msg_busy", "base_revision": design.revision,
                          "preview": True, "prompt": "Another edit"})
            assert receive(ws, "error")["code"] == "BUSY"
            ws.send_json(decision(design, base_revision=design.revision + 1).model_dump(mode="json"))
            assert receive(ws, "error")["code"] == "REVISION_CONFLICT"
            assert design.preview is not None
            ws.send_json(decision(design, "cancel").model_dump(mode="json"))
            assert receive(ws, "design.preview_closed")["preview_id"] == preview["preview_id"]
            assert design.preview is None
            ws.send_json(dimension(design).model_dump(mode="json"))
            receive(ws, "design.preview")
            assert design.preview is not None
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "client.hello", "contract_version": "1.0.0", "session_id": design.id})
            receive(ws, "session.ready")
            assert design.preview is None and design.store.ledger.features[0].parameters.height == 20
    registry.drop(design.id)


def test_cancelled_build_never_publishes_a_candidate(design, monkeypatch):
    from cadenza_backend import design_commands
    async def cancelled(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(design_commands, "build_design", cancelled)
    before = design.snapshot()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(preview_parameter(design, dimension(design), discard))
    assert design.preview is None and design.snapshot() == before
