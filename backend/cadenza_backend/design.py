"""Build and publish one whole-design transaction, shared by prompts and undo."""
from typing import Any, Awaitable, Callable, cast

from pydantic import BaseModel

from cadenza_backend import geometry_bridge
from cadenza_backend.artifacts import Artifact
from cadenza_backend.contracts_bridge import Ledger
from cadenza_backend.session import Session
from cadenza_backend.wire import BlobRef, BuildStats, GeometryReady, LedgerUpdated, dump_message
from cadenza_contracts.messages import ArtifactKind, BlobHeader, encode_blob_frame

Emit = Callable[[BaseModel], Awaitable[None]]
EmitBinary = Callable[[bytes], Awaitable[None]]


def stage_operations(session: Session, ops):
    """Shared atomic patch preparation for model and direct parameter edits."""
    from cadenza_backend.contracts_bridge import CadenzaError, ErrorCode
    grouped = {}
    for op in ops:
        lid = op.ledger_id or session.primary_ledger_id
        if lid not in session.stores:
            raise CadenzaError(ErrorCode.AGENT_INVALID_OUTPUT, f"Unknown ledger {lid}.")
        grouped.setdefault(lid, []).append(op)
    candidates = {lid: session.stores[lid].stage(items) for lid, items in grouped.items()}
    return candidates, session.stage({lid: candidate.ledger for lid, candidate in candidates.items()})


def build_design(ledgers: dict[str, Ledger], primary_id: str) -> geometry_bridge.BuildOutcome:
    from cadenza_contracts.references import validate_references
    from cadenza_backend.contracts_bridge import ErrorCode
    try:
        validate_references(ledgers[primary_id].model_dump(mode="json"), ledgers)
    except ValueError as exc:
        return geometry_bridge.BuildOutcome(ok=False, detail=str(exc), stub=False,
                                            code=ErrorCode.LEDGER_INVALID)
    # Build every secondary: a disconnected part or an intermediate dependent
    # assembly must also be valid before the snapshot can be accepted.
    for lid, ledger in ledgers.items():
        if lid != primary_id and (ledger.features or ledger.assembly is not None):
            outcome = geometry_bridge.build(ledger, all_ledgers=ledgers)
            if not outcome.ok:
                return outcome
    primary = ledgers[primary_id]
    if not primary.features and primary.assembly is None:
        return geometry_bridge.BuildOutcome(ok=True, detail="Empty design.", stub=False, empty=True)
    return geometry_bridge.build(primary, all_ledgers=ledgers)


async def accept_design(
    session: Session, ledgers: dict[str, Ledger], outcome: geometry_bridge.BuildOutcome,
    message_id: str, emit: Emit, emit_binary: EmitBinary | None = None, *,
    summary: str = "", patch: list[dict[str, Any]] | None = None, undo_index: int | None = None,
    redo: bool = False,
) -> None:
    """Prepare messages first, then commit without awaits. Delivery can be replayed."""
    if not outcome.ok:
        raise ValueError("Cannot accept a failed build")
    revision = ledgers[session.primary_ledger_id].revision
    artifacts = [Artifact(revision, kind, content_type, data) for kind, data, content_type in (
        ("glb", outcome.glb, geometry_bridge.GLB_CONTENT_TYPE),
        ("step", outcome.step, geometry_bridge.STEP_CONTENT_TYPE),
    ) if data]
    updated = LedgerUpdated(message_id=message_id, revision=revision, store=ledgers,
                            ledger_id=session.primary_ledger_id, patch=patch or [], summary=summary,
                            can_undo=True if undo_index is None else undo_index > 0,
                            can_redo=(len(session.redo) > 1 if redo else undo_index is not None))
    ready = GeometryReady(message_id=message_id, revision=revision,
        blobs=[BlobRef(artifact=cast(ArtifactKind, a.kind), content_type=a.content_type, bytes=len(a.data),
                       sha256=a.sha256, url=a.url_for(session.id)) for a in artifacts],
        stats=BuildStats(bbox=outcome.bbox, volume_mm3=outcome.volume_mm3,
                         face_count=outcome.face_count, build_ms=outcome.build_ms),
        warnings=outcome.warnings, stub=outcome.stub, empty=outcome.empty)
    dump_message(updated)
    dump_message(ready)
    session.commit(ledgers, undo_index=undo_index, redo=redo)
    from cadenza_backend.diagnostics import CURRENT, event, snapshot
    trace = CURRENT.get()
    if trace:
        trace.committed = True
    event("design.committed", revision=revision, session_id=session.id,
        snapshot=snapshot(ledgers), undo_index=undo_index, redo=redo,
        artifacts=[dict(kind=a.kind, sha256=a.sha256, bytes=len(a.data)) for a in artifacts])
    for artifact in artifacts:
        session.artifacts.put(revision, artifact.kind, artifact.content_type, artifact.data)
    session.last_geometry = ready
    await emit(updated)
    await replay_geometry(session, emit, emit_binary, ready=ready)


async def replay_geometry(session: Session, emit: Emit, emit_binary: EmitBinary | None = None,
                          *, ready: GeometryReady | None = None) -> None:
    ready = ready or session.last_geometry
    if ready is None:
        return
    # Capture the bytes before yielding; another socket may accept a newer build.
    frames = []
    for blob in ready.blobs:
        artifact = session.artifacts.get(ready.revision, blob.artifact)
        if artifact is not None:
            frames.append(encode_blob_frame(BlobHeader(revision=ready.revision,
                artifact=cast(ArtifactKind, blob.artifact), bytes=len(artifact.data)), artifact.data))
    await emit(ready)
    if emit_binary:
        for frame in frames:
            await emit_binary(frame)
