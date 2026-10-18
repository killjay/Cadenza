import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from cadenza_backend.part_views import source_artifact, view_part
from cadenza_backend.session import Session, registry
from cadenza_backend.ledger_store import LedgerStore
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.part_commands import PartViewRequest


def test_source_view_is_read_only_and_expires():
    session = registry.create()
    source = Ledger.model_validate(dict(features=[dict(id="feat_box", kind="box", operation="add",
        parameters=dict(length=20, width=10, height=5))]))
    session.stores[source.project_id] = LedgerStore(source)
    before = session.snapshot(); frames = []
    async def emit(frame): frames.append(frame)
    asyncio.run(view_part(session, PartViewRequest(message_id="view", base_revision=0, ledger_id=source.project_id), emit))
    assert session.source_view and session.source_view.outcome.volume_mm3 == pytest.approx(1000)
    assert session.snapshot() == before and not session.history and session.last_geometry is None
    response = asyncio.run(source_artifact(session.id, frames[0].view_id, "model.step"))
    assert b"ISO-10303" in bytes(response.body)
    assert response.headers["content-disposition"].endswith(".step")
    assert response.headers["cache-control"] == "no-store"
    with pytest.raises(HTTPException): asyncio.run(source_artifact(session.id, "wrong", "model.step"))
    session.source_view.created -= 901
    with pytest.raises(HTTPException): asyncio.run(source_artifact(session.id, frames[0].view_id, "model.step"))
    assert session.source_view is None
    registry.drop(session.id)


def test_invalid_and_stale_views_reject_without_work(monkeypatch):
    from unittest.mock import AsyncMock
    from cadenza_backend import part_views
    from cadenza_backend.contracts_bridge import CadenzaError
    build = AsyncMock(); monkeypatch.setattr(part_views, "build_design", build)
    session = registry.create()
    async def emit(_): pass
    for revision, lid in [(9, session.primary_ledger_id), (0, "missing")]:
        with pytest.raises(CadenzaError): asyncio.run(view_part(session,
            PartViewRequest(message_id="v", base_revision=revision, ledger_id=lid), emit))
    build.assert_not_called()
    assert session.source_view is None and not session.history
    registry.drop(session.id)


def test_result_read_failure_preserves_source_view_and_recovers(monkeypatch, plate_ledger):
    from cadenza_backend.contracts_bridge import CadenzaError
    from cadenza_backend.design import accept_design
    from cadenza_backend.geometry_jobs import build_design
    session = Session(primary_ledger_id=plate_ledger.project_id,
        stores={plate_ledger.project_id: LedgerStore(plate_ledger)})
    registry.register(session)
    frames = []
    async def emit(frame): frames.append(frame)
    async def seed():
        ledgers = session.stage({})
        await accept_design(session, ledgers, await build_design(ledgers, session.primary_ledger_id), "seed", emit)
    asyncio.run(seed())
    request = PartViewRequest(message_id="view", base_revision=session.revision, ledger_id=plate_ledger.project_id)
    asyncio.run(view_part(session, request, emit))
    prior = session.source_view
    before = session.snapshot(), session.revision, list(session.history), list(session.redo), session.last_geometry
    artifacts = [session.artifacts.get(session.revision, kind) for kind in ("glb", "step")]
    assert all(artifact is not None and artifact.data for artifact in artifacts)
    original_read = Path.read_text
    def read(path, *args, **kwargs):
        if path.name == "result.json": raise PermissionError(13, "injected read denial")
        return original_read(path, *args, **kwargs)
    try:
        frames.clear()
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", read)
            with pytest.raises(CadenzaError): asyncio.run(view_part(session, request, emit))
        assert session.source_view is prior and not frames
        assert (session.snapshot(), session.revision, list(session.history), list(session.redo), session.last_geometry) == before
        assert [session.artifacts.get(session.revision, kind) for kind in ("glb", "step")] == artifacts
        asyncio.run(view_part(session, request, emit))
        assert session.source_view is not prior and len(frames) == 1
    finally:
        registry.drop(session.id)


def test_context_view_is_revision_bound_and_read_only(plate_ledger):
    source = plate_ledger
    root = Ledger.model_validate({"assembly": {"components": [{"id": "comp_plate",
        "sub_assembly_id": source.project_id, "placement": {"origin": [80, 20, 0], "rotation_deg": [0, 0, 90]}}]}})
    session = Session(primary_ledger_id=root.project_id, stores={l.project_id: LedgerStore(l) for l in (root, source)})
    registry.register(session)
    frames = []
    async def emit(frame): frames.append(frame)
    before = session.snapshot()
    try:
        asyncio.run(view_part(session, PartViewRequest(message_id="view", base_revision=0,
            ledger_id=source.project_id, component_path=["comp_plate"]), emit))
        frame = frames[0]
        assert frame.component_path == ["comp_plate"] and frame.context_glb_url
        assert [frame.source_to_project[i] for i in (3, 7, 11)] == pytest.approx([80, 20, 0])
        assert bytes(asyncio.run(source_artifact(session.id, frame.view_id, "context.glb")).body).startswith(b"glTF")
        assert session.snapshot() == before and not session.history and session.last_geometry is None
        session.store.ledger.revision += 1
        with pytest.raises(HTTPException): asyncio.run(source_artifact(session.id, frame.view_id, "context.glb"))
    finally:
        registry.drop(session.id)


def test_invalid_context_rejects_before_build_and_context_failure_is_explicit(monkeypatch, plate_ledger):
    from unittest.mock import AsyncMock
    from cadenza_backend import part_views
    from cadenza_backend.contracts_bridge import CadenzaError, ErrorCode
    root = Ledger.model_validate({"assembly": {"components": [{"id": "comp_plate", "sub_assembly_id": plate_ledger.project_id}]}})
    session = Session(primary_ledger_id=root.project_id, stores={l.project_id: LedgerStore(l) for l in (root, plate_ledger)})
    request = dict(message_id="view", base_revision=0, ledger_id=plate_ledger.project_id)
    frames = []
    async def emit(frame): frames.append(frame)
    with monkeypatch.context() as patch:
        build = AsyncMock(); patch.setattr(part_views, "build_design", build)
        with pytest.raises(CadenzaError): asyncio.run(view_part(session, PartViewRequest.model_validate({**request, "component_path": ["comp_wrong"]}), emit))
        build.assert_not_called()
    monkeypatch.setattr(part_views, "inspect", AsyncMock(side_effect=CadenzaError(ErrorCode.GEOMETRY_TIMEOUT, "injected")))
    asyncio.run(view_part(session, PartViewRequest.model_validate({**request, "component_path": ["comp_plate"]}), emit))
    assert frames[0].glb_url and frames[0].context_warning
    assert frames[0].context_glb_url is None and session.source_view is not None and session.source_view.context_glb is None
