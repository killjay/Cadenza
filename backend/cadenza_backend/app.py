"""FastAPI application + AG-UI WebSocket transport.

One socket per client. The server owns the session and the ledger; the client
owns the camera and the prompt box. Every server frame is one of the types in
`wire.py`, JSON-encoded, one frame per WebSocket message.

Run it:
    uvicorn cadenza_backend.app:app --reload --port 8000
"""

import os
from contextlib import asynccontextmanager

import logging

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError
import sqlite3
from cadenza_contracts.project import ProjectDocument, document_version
from cadenza_backend.projects import ProjectRepository, SaveConflict
from cadenza_backend.surface_references import router as surface_reference_router
from cadenza_backend.edit_preview import router as preview_router, pending, preview_parameter, decide_preview
from cadenza_contracts.messages import NamedParameterEdit, ParameterEdit, SketchDimensionEdit, PreviewDecision, PreviewClosed, DesignCommand
from cadenza_contracts.sketch_draft import DraftStart, DraftUpdate, DraftAction, DraftState, DraftClosed, DraftPrompt, DraftProposalDecision
from cadenza_backend.sketch_drafts import handle_draft
from cadenza_backend.sketch_prompts import handle_sketch_prompt
from cadenza_backend.contracts_bridge import CadenzaError
from cadenza_backend.design_commands import handle_design_command
from cadenza_backend.design_prompts import handle_design_prompt
from cadenza_contracts.assembly_commands import AssemblyCommand, AssemblyCapabilitiesRequest
from cadenza_backend.assembly_commands import capabilities as assembly_capabilities, handle_assembly_command
from cadenza_contracts.messages import AssemblyPrompt
from cadenza_contracts.assembly_commands import AssemblyPromptCapabilitiesRequest
from cadenza_backend.assembly_prompts import discovery as assembly_prompt_capabilities, handle_assembly_prompt
from cadenza_backend.part_commands import create_part, prompt_part, rename_part
from cadenza_backend.part_views import router as part_view_router, view_part
from cadenza_contracts.messages import PartPrompt
from cadenza_contracts.part_commands import PartCreate, PartRename, PartViewRequest, PartCapabilitiesRequest, PartCapabilitiesResponse
from cadenza_contracts.messages import DesignPrompt, PromptCapabilitiesRequest, PromptCapabilitiesResponse
from cadenza_contracts.feature_capabilities import CapabilitiesRequest, CapabilitiesResponse, feature_capabilities
from cadenza_contracts.feature_capabilities import PatternCapabilitiesRequest, PatternCapabilitiesResponse

from cadenza_backend import diagnostics
from cadenza_backend.diagnostics_api import router as diagnostics_router
from cadenza_backend.evaluation_view_api import router as evaluation_view_router
from cadenza_backend import geometry_bridge, geometry_jobs, orchestrator
from cadenza_backend.design import accept_design, replay_geometry
from cadenza_backend.geometry_jobs import build_design, probe, GeometryJobError
from cadenza_backend.wire import Limits
from cadenza_backend.config import get_settings
from cadenza_backend.contracts_bridge import CONTRACT_VERSION, ErrorCode
from cadenza_backend.session import Session, LedgerStore, registry
from cadenza_backend.wire import (
    ClientHello,
    ClientLog,
    ErrorMessage,
    LedgerRequest,
    LedgerCreate,
    LedgerUpdated,
    Ping,
    Pong,
    SessionReady,
    SessionReset,
    SessionUndo,
    SessionRedo,
    SpatialHit,
    SpatialProbe,
    SpatialResult,
    UserPrompt,
    dump_message,
    parse_client_message,
    PROTOCOL_VERSION,
)

log = logging.getLogger("cadenza.transport")

settings = get_settings()
projects = ProjectRepository(settings.project_db_path)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        yield
    finally:
        geometry_jobs.runner.close()


app = FastAPI(
    title="CADenza backend",
    lifespan=lifespan,
    version="0.1.0",
    description="AG-UI WebSocket transport, JSON ledger, and the agent routing layer.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(surface_reference_router)
app.include_router(preview_router)
app.include_router(part_view_router)
app.include_router(diagnostics_router)
app.include_router(evaluation_view_router)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


@app.get("/health")
async def health() -> dict:
    """Enough to tell, at a glance, whether a failure is config or code."""
    from cadenza_backend.contracts_bridge import CONTRACTS_FULLY_LANDED

    return {
        "status": "ok",
        "contract_version": CONTRACT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "contracts_fully_landed": CONTRACTS_FULLY_LANDED,
        "ai_available": settings.ai_available,
        "providers_configured": settings.configured_providers(),
        "agent_routing": {
            "coordinator": settings.stage_model("coordinator"),
            "draftsman": settings.stage_model("draftsman"),
            "engineer": "deterministic table (no model call)",
            "machinist": settings.stage_model("machinist"),
        },
        "sessions": len(registry),
        "geometry": geometry_bridge.GEOMETRY_DETAIL,
        "vision_available": settings.vision_available,
        "stubbed": (
            [] if geometry_bridge.REAL_GEOMETRY else ["geometry build", "reverse spatial lookup"]
        )
        + ["wire envelope"],
    }


class SaveProject(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    base_revision: int = Field(ge=0)


@app.get("/api/projects")
async def list_projects() -> list[dict]:
    try:
        return projects.list()
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(503, "Project storage is unavailable.") from exc


@app.post("/api/sessions/{session_id}/save")
async def save_project(session_id: str, request: SaveProject) -> dict:
    with diagnostics.request_trace(registry.get(session_id), dict(type="project.save", **request.model_dump(mode="json"))):
        session = registry.get(session_id)
        if session is None:
            raise HTTPException(404, "Session no longer exists.")
        async with session.lock:
            if session.revision != request.base_revision:
                raise HTTPException(409, "Design changed before saving. Retry with the current revision.")
            document = ProjectDocument(document_version=document_version([session.snapshot(), *session.history, *session.redo]), name=request.name.strip() or "Untitled Part",
                primary_ledger_id=session.primary_ledger_id, ledgers=session.snapshot(),
                history=session.history, redo=session.redo)
            try:
                pid, version = projects.save(document, session.project_id, session.project_version)
            except SaveConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except (sqlite3.Error, OSError) as exc:
                raise HTTPException(503, "Save failed. The previous saved project is preserved.") from exc
            session.project_id, session.project_version = pid, version
            session.project_name, session.saved_revision = document.name, session.revision
            diagnostics.event("project.saved", project_id=pid, storage_version=version, revision=session.revision)
            return {"id": pid, "name": document.name, "revision": session.revision, "version": version}


@app.post("/api/projects/{project_id}/open")
async def open_project(project_id: str) -> dict:
    with diagnostics.request_trace(None, dict(type="project.open", project_id=project_id)):
        try:
            document, version = projects.load(project_id)
        except KeyError as exc:
            raise HTTPException(404, "Project not found.") from exc
        except (ValueError, sqlite3.Error, OSError) as exc:
            raise HTTPException(422, "Cannot read this project or its document version.") from exc
        candidate = Session(primary_ledger_id=document.primary_ledger_id,
            stores={lid: LedgerStore(ledger=ledger) for lid, ledger in document.ledgers.items()})
        ledgers = candidate.stage({})
        outcome = await build_design(ledgers, candidate.primary_ledger_id)
        if outcome.code in (ErrorCode.BUSY, ErrorCode.GEOMETRY_TIMEOUT):
            raise HTTPException(503 if outcome.code == ErrorCode.BUSY else 504, outcome.detail)
        if not outcome.ok or outcome.stub:
            raise HTTPException(422, "Project could not be rebuilt. Your current design is preserved.")

        async def discard(_message: BaseModel) -> None:
            pass

        await accept_design(candidate, ledgers, outcome, "msg_open", discard)
        candidate.history, candidate.redo = document.history, document.redo
        candidate.project_id, candidate.project_version = project_id, version
        candidate.project_name, candidate.saved_revision = document.name, candidate.revision
        registry.register(candidate)
        diagnostics.event("project.opened", project_id=project_id, storage_version=version,
            restored_session_id=candidate.id, revision=candidate.revision)
        trace = diagnostics.CURRENT.get()
        candidate.logger.log("project.restored", project_id=project_id, storage_version=version,
            originating_session_id=trace.session_id if trace else None, originating_trace_id=trace.id if trace else None)
        return {"session_id": candidate.id}


@app.get("/sessions/{session_id}/ledger")
async def get_ledger(session_id: str) -> dict:
    """Debug convenience — the WebSocket is the real interface."""
    session = registry.get(session_id)
    if session is None:
        return {"error": ErrorCode.UNKNOWN_SESSION.value, "session_id": session_id}
    return session.store.ledger.model_dump(mode="json")


@app.get("/sessions/{session_id}/artifacts/{revision}/model.{kind}")
async def get_artifact(session_id: str, revision: int, kind: str) -> Response:
    """The download path. The viewport gets the same bytes pushed over the socket.

    Artifacts are per-session because ledgers are: revision 2 of one part has
    nothing to do with revision 2 of another, and serving them from a global
    namespace would let one client fetch another's geometry by guessing a number.
    """
    session = registry.get(session_id)
    if session is None:
        return Response(status_code=404, content=ErrorCode.UNKNOWN_SESSION.value)
    artifact = session.artifacts.get(revision, kind)
    if artifact is None:
        return Response(status_code=404, content=f"no {kind} for revision {revision}")
    return Response(
        content=artifact.data,
        media_type=artifact.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="model-r{revision}.{kind}"',
            "ETag": artifact.sha256,
        },
    )


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #


def _major(version: str) -> str:
    return version.split(".", 1)[0]


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()

    async def emit(message: BaseModel) -> None:
        trace = diagnostics.CURRENT.get()
        if trace and getattr(message, "type", "") == "error":
            trace.rejected = True
        if trace and getattr(message, "type", "") not in ("pong", "session.ready", "ledger.updated"):
            diagnostics.event("transport.outgoing", frame=message.model_dump(mode="json"))
        await websocket.send_text(dump_message(message))

    async def emit_binary(frame: bytes) -> None:
        await websocket.send_bytes(frame)

    async def send_ready(current: Session) -> None:
        current.logger.log("session.connected", revision=current.revision, project_id=current.project_id)
        async with current.lock:
            current.source_view = None
            current.preview = None
            current.draft = None
            await emit(SessionReady(session_id=current.id, contract_version=CONTRACT_VERSION, primary_ledger_id=current.primary_ledger_id,
                store={lid: s.ledger for lid, s in current.stores.items()},
                revision=current.revision, can_undo=bool(current.history),
                can_redo=bool(current.redo), project_id=current.project_id,
                project_name=current.project_name, saved_revision=current.saved_revision,
                agents_available=settings.ai_available,
                limits=Limits(build_timeout_s=settings.geometry_timeout_s,
                              startup_timeout_s=settings.geometry_startup_timeout_s)))
            await replay_geometry(current, emit, emit_binary)

    session: Session | None = None

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                frame = parse_client_message(raw)
            except ValidationError as exc:
                if session:
                    session.logger.log("transport.invalid_frame", bytes=len(raw.encode()), error_type="ValidationError")
                await emit(
                    ErrorMessage(
                        code=ErrorCode.BAD_MESSAGE,
                        message="Could not parse that frame.",
                        detail=str(exc),
                    )
                )
                continue

            # ── handshake ───────────────────────────────────────────────────
            if isinstance(frame, ClientHello):
                if _major(frame.contract_version) != _major(CONTRACT_VERSION):
                    await emit(
                        ErrorMessage(
                            code=ErrorCode.PROTOCOL_MISMATCH,
                            message=(
                                f"Client speaks contract {frame.contract_version}, "
                                f"server speaks {CONTRACT_VERSION}."
                            ),
                            recoverable=False,
                        )
                    )
                    await websocket.close(code=1002)
                    return
                session = registry.get_or_create(frame.session_id)
                await send_ready(session)
                continue

            # A client that skips the handshake still gets a session — being
            # strict here would make the transport harder to exercise by hand
            # for no safety gain in a prototype.
            if session is None:
                session = registry.get_or_create(getattr(frame, "session_id", None))
                await send_ready(session)

            with diagnostics.request_trace(session, frame.model_dump(mode="json")):
                session.touch()

                if session.draft and isinstance(frame, SessionReset):
                    await emit(ErrorMessage(message_id=frame.message_id, code=ErrorCode.BUSY,
                        message="Discard the sketch draft before starting another design."))
                    continue

                if isinstance(frame, (UserPrompt, SessionUndo, SessionRedo, LedgerCreate, ParameterEdit, NamedParameterEdit, SketchDimensionEdit, PreviewDecision,
                                      DraftStart, DraftUpdate, DraftAction, DraftPrompt, DraftProposalDecision, DesignCommand, DesignPrompt, PartCreate, PartRename, PartPrompt, PartViewRequest, AssemblyCommand, AssemblyPrompt)):
                    if session.lock.locked():
                        await emit(ErrorMessage(message_id=frame.message_id, code=ErrorCode.BUSY,
                            message="Please wait for the current action to finish."))
                        continue
                    if session.draft and isinstance(frame, (UserPrompt, DesignPrompt, AssemblyPrompt, SessionUndo, SessionRedo, LedgerCreate,
                                                             ParameterEdit, NamedParameterEdit, SketchDimensionEdit)):
                        await emit(ErrorMessage(message_id=frame.message_id, code=ErrorCode.BUSY,
                            message="Finish or discard the sketch draft before changing the accepted design."))
                        continue
                    if (not isinstance(frame, (DraftUpdate, DraftAction, DraftPrompt, DraftProposalDecision))
                        and frame.base_revision is not None and frame.base_revision != session.revision):
                        await emit(ErrorMessage(message_id=frame.message_id, code=ErrorCode.REVISION_CONFLICT,
                            message="This design changed. Refresh before editing.",
                            detail=f"Expected revision {session.revision}; received {frame.base_revision}."))
                        if isinstance(frame, PreviewDecision) and session.preview is None:
                            await emit(PreviewClosed(message_id=frame.message_id, preview_id=frame.preview_id))
                            await emit(LedgerUpdated(message_id=frame.message_id, revision=session.revision,
                                can_undo=bool(session.history), can_redo=bool(session.redo),
                                store={lid: store.ledger for lid, store in session.stores.items()}))
                            await replay_geometry(session, emit, emit_binary)
                        continue
                    if isinstance(frame, UserPrompt) and any(lid not in session.stores for lid in frame.active_ledger_ids):
                        await emit(ErrorMessage(message_id=frame.message_id, code=ErrorCode.BAD_MESSAGE,
                            message="An active ledger no longer exists."))
                        continue
                    if isinstance(frame, (UserPrompt, DesignPrompt, AssemblyPrompt, ParameterEdit, NamedParameterEdit, SketchDimensionEdit)) and pending(session):
                        await emit(ErrorMessage(message_id=frame.message_id, code=ErrorCode.BUSY,
                            message="Apply or cancel the current preview before requesting another edit."))
                        continue
                    if isinstance(frame, UserPrompt) and frame.preview and frame.base_revision is None:
                        await emit(ErrorMessage(message_id=frame.message_id, code=ErrorCode.BAD_MESSAGE,
                            message="Preview requests require the accepted base revision."))
                        continue

                # ── routing ─────────────────────────────────────────────────────
                if isinstance(frame, Ping):
                    # The contract's Ping/Pong carry the client's timestamp, not a
                    # session id — echoing `t` is what lets the client measure RTT.
                    await emit(Pong(t=frame.t))

                elif isinstance(frame, CapabilitiesRequest):
                    await emit(CapabilitiesResponse(message_id=frame.message_id, session_id=session.id,
                        features=feature_capabilities()))

                elif isinstance(frame, PromptCapabilitiesRequest):
                    await emit(PromptCapabilitiesResponse(message_id=frame.message_id, session_id=session.id))

                elif isinstance(frame, PatternCapabilitiesRequest):
                    await emit(PatternCapabilitiesResponse(message_id=frame.message_id, session_id=session.id))

                elif isinstance(frame, AssemblyCapabilitiesRequest):
                    await emit(assembly_capabilities(session, frame))

                elif isinstance(frame, AssemblyPromptCapabilitiesRequest):
                    await emit(assembly_prompt_capabilities(session, frame))

                elif isinstance(frame, AssemblyPrompt):
                    async with session.lock:
                        try:
                            await handle_assembly_prompt(session, frame, emit)
                        except CadenzaError as exc:
                            await emit(ErrorMessage(message_id=frame.message_id, code=exc.code, message=exc.message, interference=exc.interference))

                elif isinstance(frame, AssemblyCommand):
                    async with session.lock:
                        try:
                            await handle_assembly_command(session, frame, emit)
                        except CadenzaError as exc:
                            await emit(ErrorMessage(message_id=frame.message_id, code=exc.code, message=exc.message, interference=exc.interference))

                elif isinstance(frame, PartCapabilitiesRequest):
                    await emit(PartCapabilitiesResponse(message_id=frame.message_id, session_id=session.id))

                elif isinstance(frame, (PartCreate, PartRename, PartPrompt, PartViewRequest)):
                    async with session.lock:
                        try:
                            if isinstance(frame, PartCreate):
                                await create_part(session, frame, emit)
                            elif isinstance(frame, PartRename):
                                await rename_part(session, frame, emit)
                            elif isinstance(frame, PartPrompt):
                                await prompt_part(session, frame, emit)
                            else:
                                await view_part(session, frame, emit)
                        except CadenzaError as exc:
                            await emit(ErrorMessage(message_id=frame.message_id, code=exc.code, message=exc.message, interference=exc.interference))

                elif isinstance(frame, DesignPrompt):
                    async with session.lock:
                        try:
                            await handle_design_prompt(session, frame, emit)
                        except CadenzaError as exc:
                            await emit(ErrorMessage(message_id=frame.message_id, code=exc.code, message=exc.message, interference=exc.interference))

                elif isinstance(frame, LedgerRequest):
                    async with session.lock:
                        await emit(
                            LedgerUpdated(
                                message_id=frame.message_id,
                                revision=session.revision, can_undo=bool(session.history),
                                can_redo=bool(session.redo),
                                store={pid: s.ledger for pid, s in session.stores.items()},
                            )
                        )
                        await replay_geometry(session, emit, emit_binary)

                        candidate = pending(session)
                        if candidate:
                            await emit(candidate.message)

                elif isinstance(frame, LedgerCreate):
                    async with session.lock:
                        new_store = LedgerStore.new(frame.name)
                        ledgers = session.stage({new_store.ledger.project_id: new_store.ledger})
                        outcome = await build_design(ledgers, session.primary_ledger_id)
                        if outcome.ok:
                            await accept_design(session, ledgers, outcome, frame.message_id, emit, emit_binary,
                                                summary="Created a part ledger.")
                        else:
                            await emit(ErrorMessage(message_id=frame.message_id, code=outcome.code or ErrorCode.GEOMETRY_KERNEL_FAILURE,
                                message="Could not rebuild the design.", detail=outcome.detail))

                elif isinstance(frame, SessionReset):
                    previous_session_id = session.id
                    session = registry.create(frame.name)
                    diagnostics.event("session.reset", previous_session_id=previous_session_id, new_session_id=session.id)
                    session.logger.log("session.reset_from", previous_session_id=previous_session_id)
                    await send_ready(session)

                elif isinstance(frame, SpatialProbe):
                    try:
                        resolved = await probe(session.store.ledger, frame.point, all_ledgers={lid: s.ledger for lid, s in session.stores.items()})
                    except GeometryJobError as exc:
                        await emit(ErrorMessage(message_id=frame.message_id, code=exc.code, message=exc.message, interference=exc.interference))
                        continue
                    await emit(
                        SpatialResult(
                            message_id=frame.message_id,
                            hit=SpatialHit(
                                component_id=resolved.component_id,
                                point=resolved.point,
                                normal=resolved.normal,
                                distance_mm=resolved.distance_mm,
                                feature_id=resolved.feature_id,
                                feature_name=resolved.feature_name,
                                feature_kind=resolved.feature_kind,
                                descriptor=resolved.descriptor,
                                confidence=resolved.confidence,
                            )
                            if resolved.feature_id
                            else None,
                        )
                    )

                elif isinstance(frame, SessionRedo):
                    async with session.lock:
                        ledgers = session.stage_redo()
                        if ledgers is None:
                            await emit(ErrorMessage(message_id=frame.message_id, code=ErrorCode.BAD_MESSAGE,
                                message="Nothing to redo."))
                            continue
                        outcome = await build_design(ledgers, session.primary_ledger_id)
                        if outcome.ok:
                            await accept_design(session, ledgers, outcome, frame.message_id, emit, emit_binary,
                                summary="Restored the next design.", redo=True)
                        else:
                            await emit(ErrorMessage(message_id=frame.message_id, code=outcome.code or ErrorCode.GEOMETRY_KERNEL_FAILURE,
                                message="Could not redo; current design preserved.", detail=outcome.detail))

                elif isinstance(frame, SessionUndo):
                    async with session.lock:
                        candidate = session.stage_undo(frame.revision)
                        if candidate is None:
                            await emit(ErrorMessage(message_id=frame.message_id, code=ErrorCode.BAD_MESSAGE,
                                message="Nothing to undo at that revision."))
                            continue
                        ledgers, index = candidate
                        outcome = await build_design(ledgers, session.primary_ledger_id)
                        if outcome.ok:
                            await accept_design(session, ledgers, outcome, frame.message_id, emit, emit_binary,
                                                summary="Restored the previous design.", undo_index=index)
                        else:
                            await emit(ErrorMessage(message_id=frame.message_id, code=outcome.code or ErrorCode.GEOMETRY_KERNEL_FAILURE,
                                message="Could not rebuild the previous design; current design preserved.", detail=outcome.detail))

                elif isinstance(frame, (DraftStart, DraftUpdate, DraftAction, DraftPrompt, DraftProposalDecision)):
                    async with session.lock:
                        try:
                            if isinstance(frame, (DraftPrompt, DraftProposalDecision)):
                                await handle_sketch_prompt(session, frame, emit)
                            else:
                                await handle_draft(session, frame, emit, emit_binary)
                        except CadenzaError as exc:
                            await emit(ErrorMessage(message_id=frame.message_id, code=exc.code, message=exc.message, detail=exc.detail, interference=exc.interference))
                            if session.draft and (isinstance(frame, DraftStart) or session.draft.state.id == frame.draft_id):
                                await emit(DraftState(message_id=frame.message_id, session_id=session.id, draft=session.draft.state))
                            elif not isinstance(frame, DraftStart):
                                await emit(DraftClosed(message_id=frame.message_id, session_id=session.id, draft_id=frame.draft_id))
                                await replay_geometry(session, emit, emit_binary)
                            if frame.base_revision != session.revision:
                                await emit(LedgerUpdated(message_id=frame.message_id, revision=session.revision,
                                    can_undo=bool(session.history), can_redo=bool(session.redo),
                                    store={lid: store.ledger for lid, store in session.stores.items()}))
                                await replay_geometry(session, emit, emit_binary)

                elif isinstance(frame, (ParameterEdit, NamedParameterEdit, SketchDimensionEdit, PreviewDecision)):
                    async with session.lock:
                        try:
                            if isinstance(frame, (ParameterEdit, NamedParameterEdit, SketchDimensionEdit)):
                                await preview_parameter(session, frame, emit)
                            else:
                                await decide_preview(session, frame, emit, emit_binary)
                        except CadenzaError as exc:
                            await emit(ErrorMessage(message_id=frame.message_id, code=exc.code,
                                message=exc.message, detail=exc.detail, interference=exc.interference))
                            if isinstance(frame, PreviewDecision) and pending(session) is None:
                                await emit(PreviewClosed(message_id=frame.message_id, preview_id=frame.preview_id))
                                await replay_geometry(session, emit, emit_binary)
                                if session.draft:
                                    await emit(DraftState(message_id=frame.message_id, session_id=session.id, draft=session.draft.state))
                                else:
                                    await emit(LedgerUpdated(message_id=frame.message_id, revision=session.revision,
                                        can_undo=bool(session.history), can_redo=bool(session.redo),
                                        store={lid: store.ledger for lid, store in session.stores.items()}))

                elif isinstance(frame, DesignCommand):
                    async with session.lock:
                        try:
                            if session.draft is not None:
                                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Cannot process direct commands while a sketch is active.")
                            if pending(session) is not None:
                                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Cannot submit new commands while preview is pending.")
                            await handle_design_command(session, frame, emit)
                        except CadenzaError as exc:
                            await emit(ErrorMessage(message_id=frame.message_id, code=exc.code,
                                message=exc.message, detail=exc.detail, interference=exc.interference))
                            if pending(session) is None:
                                await replay_geometry(session, emit, emit_binary)

                elif isinstance(frame, UserPrompt):
                    async with session.lock:
                        await orchestrator.handle_prompt(
                            session,
                            frame.message_id,
                            frame.prompt,
                            frame.targets,
                            emit,
                            images=frame.decoded_images(),
                            emit_binary=emit_binary,
                            active_ledger_ids=frame.active_ledger_ids,
                            preview=frame.preview,
                        )

                elif isinstance(frame, ClientLog):
                    session.logger.log("client_log", level=frame.level, message_id=frame.message_id, message=frame.message, data=frame.data)

                else:  # pragma: no cover — the union is exhaustive
                    await emit(
                        ErrorMessage(
                            code=ErrorCode.BAD_MESSAGE,
                            message=f"Unhandled frame type: {getattr(frame, 'type', '?')}",
                        )
                    )

    except WebSocketDisconnect:
        # The session deliberately outlives the socket so a reconnect can resume.
        if session:
            session.logger.log("session.disconnected", revision=session.revision)
        log.info("client disconnected (session=%s)", session.id if session else None)
    except Exception:  # noqa: BLE001
        log.exception("transport error")
        try:
            await websocket.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
