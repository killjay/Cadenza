"""FastAPI application + AG-UI WebSocket transport.

One socket per client. The server owns the session and the ledger; the client
owns the camera and the prompt box. Every server frame is one of the types in
`wire.py`, JSON-encoded, one frame per WebSocket message.

Run it:
    uvicorn cadenza_backend.app:app --reload --port 8000
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ValidationError

from cadenza_backend import geometry_bridge, orchestrator
from cadenza_backend.config import get_settings
from cadenza_backend.contracts_bridge import CONTRACT_VERSION, ErrorCode
from cadenza_backend.session import Session, registry
from cadenza_backend.wire import (
    PROTOCOL_VERSION,
    ClientHello,
    ErrorMessage,
    LedgerRequest,
    LedgerUpdated,
    Ping,
    Pong,
    SessionReady,
    SessionReset,
    SpatialHit,
    SpatialProbe,
    SpatialResult,
    UserPrompt,
    dump_message,
    parse_client_message,
)

log = logging.getLogger("cadenza.transport")

settings = get_settings()

app = FastAPI(
    title="CADenza backend",
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
        await websocket.send_text(dump_message(message))

    async def emit_binary(frame: bytes) -> None:
        await websocket.send_bytes(frame)

    session: Session | None = None

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                frame = parse_client_message(raw)
            except ValidationError as exc:
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
                await emit(
                    SessionReady(
                        session_id=session.id,
                        contract_version=CONTRACT_VERSION,
                        ledger=session.store.ledger,
                    )
                )
                continue

            # A client that skips the handshake still gets a session — being
            # strict here would make the transport harder to exercise by hand
            # for no safety gain in a prototype.
            if session is None:
                session = registry.get_or_create(frame.session_id)
                await emit(
                    SessionReady(
                        session_id=session.id,
                        contract_version=CONTRACT_VERSION,
                        ledger=session.store.ledger,
                    )
                )

            session.touch()

            # ── routing ─────────────────────────────────────────────────────
            if isinstance(frame, Ping):
                # The contract's Ping/Pong carry the client's timestamp, not a
                # session id — echoing `t` is what lets the client measure RTT.
                await emit(Pong(t=frame.t))

            elif isinstance(frame, LedgerRequest):
                await emit(
                    LedgerUpdated(
                        message_id=frame.message_id,
                        ledger=session.store.ledger,
                        patch=[],
                        revision=session.store.ledger.revision,
                    )
                )

            elif isinstance(frame, SessionReset):
                session = registry.create(frame.name)
                await emit(
                    SessionReady(
                        session_id=session.id,
                        contract_version=CONTRACT_VERSION,
                        ledger=session.store.ledger,
                    )
                )

            elif isinstance(frame, SpatialProbe):
                resolved = geometry_bridge.probe(session.store.ledger, frame.point)
                await emit(
                    SpatialResult(
                        message_id=frame.message_id,
                        hit=SpatialHit(
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

            elif isinstance(frame, UserPrompt):
                await orchestrator.handle_prompt(
                    session,
                    frame.message_id,
                    frame.prompt,
                    frame.target,
                    emit,
                    images=frame.decoded_images(),
                    emit_binary=emit_binary,
                )

            else:  # pragma: no cover — the union is exhaustive
                await emit(
                    ErrorMessage(
                        code=ErrorCode.BAD_MESSAGE,
                        message=f"Unhandled frame type: {getattr(frame, 'type', '?')}",
                    )
                )

    except WebSocketDisconnect:
        # The session deliberately outlives the socket so a reconnect can resume.
        log.info("client disconnected (session=%s)", session.id if session else None)
    except Exception:  # noqa: BLE001
        log.exception("transport error")
        try:
            await websocket.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
