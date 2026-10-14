"""Transient source display artifacts, separate from accepted primary artifacts."""
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response

from cadenza_backend.contracts_bridge import CadenzaError, ErrorCode
from cadenza_backend.edit_preview import pending
from cadenza_backend.geometry_bridge import BuildOutcome
from cadenza_backend.geometry_jobs import build_design
from cadenza_backend.session import registry
from cadenza_contracts.part_commands import PartViewReady, PartViewRequest

router = APIRouter()


@dataclass
class SourceView:
    id: str
    ledger_id: str
    revision: int
    outcome: BuildOutcome
    created: float = field(default_factory=monotonic)


async def view_part(session, request: PartViewRequest, emit):
    if request.base_revision != session.revision:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The project changed. Select the part again.")
    if session.draft or pending(session):
        raise CadenzaError(ErrorCode.BUSY, "Finish the current draft or preview before changing views.")
    source = session.stores.get(request.ledger_id)
    if source is None or source.ledger.assembly is not None:
        raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "Choose an existing source part.")
    outcome = await build_design(session.snapshot(), request.ledger_id)
    if not outcome.ok or outcome.stub:
        raise CadenzaError(outcome.code or ErrorCode.GEOMETRY_KERNEL_FAILURE, outcome.detail)
    if session.revision != request.base_revision:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The project changed while loading the part.")
    view = SourceView(uuid4().hex, request.ledger_id, session.revision, outcome)
    session.source_view = view
    url = f"/api/sessions/{session.id}/part-views/{view.id}"
    await emit(PartViewReady(message_id=request.message_id, session_id=session.id,
        revision=view.revision, ledger_id=view.ledger_id, view_id=view.id,
        glb_url=f"{url}/model.glb" if outcome.glb else None,
        step_url=f"{url}/model.step" if outcome.step else None, empty=outcome.empty))


@router.get("/api/sessions/{session_id}/part-views/{view_id}/{filename}")
async def source_artifact(session_id: str, view_id: str, filename: str):
    session = registry.get(session_id)
    view = session.source_view if session else None
    if view is None or session is None or view.id != view_id or view.revision != session.revision or monotonic() - view.created > 900:
        if session and view and (view.revision != session.revision or monotonic() - view.created > 900):
            session.source_view = None
        raise HTTPException(404, "This source view expired. Select the part again.")
    data = view.outcome.glb if filename == "model.glb" else view.outcome.step if filename == "model.step" else None
    if data is None:
        raise HTTPException(404, "Source artifact is unavailable.")
    source_name = session.stores[view.ledger_id].ledger.metadata.name
    download_name = quote(f"{source_name}.{filename.rsplit('.', 1)[1]}", safe="")
    return Response(content=data, media_type="model/gltf-binary" if filename.endswith("glb") else "application/step",
        headers={"Cache-Control": "no-store", "Content-Disposition": f"attachment; filename*=UTF-8''{download_name}"})
