"""The turn: prompt (and optionally a sketch) in, streamed frames out.

This is `ARCHITECTURE.md` §2 "The turn, exactly", implemented. It is
transport-agnostic — it takes `emit` for JSON frames and `emit_binary` for blob
frames and never touches a WebSocket — so a test, a CLI or an SSE endpoint can
drive the same pipeline.

Two ordering rules are load-bearing and easy to get backwards:

  1. **Build before commit.** The candidate ledger is staged, handed to
     geometry, and adopted only once geometry confirms it builds. A failed build
     therefore leaves the store untouched — there is no rollback path because
     there is nothing to roll back, and no window in which the ledger has
     advanced past the last built revision.
  2. **`ledger.updated` is never sent for a revision whose geometry failed.**
     It is emitted after the build, not before. The client is entitled to assume
     that a revision it has been told about has geometry behind it.

`revision` is bumped by `mark_built()` and by nothing else.
"""

from __future__ import annotations

import time
from typing import Awaitable, Callable

from pydantic import BaseModel

from cadenza_backend import geometry_bridge
from cadenza_backend.agents import engineer
from cadenza_backend.agents.coordinator import AgentKind, route
from cadenza_backend.agents.draftsman import parse_assumptions, run_draftsman
from cadenza_backend.agents.machinist import run_machinist
from cadenza_backend.agents.model_client import (
    AIError,
    AINotConfigured,
    AIRefused,
    ModelClient,
)
from cadenza_backend.contracts_bridge import (
    AgentPatchOutput,
    CadenzaError,
    ErrorCode,
    PatchOp,
)
from cadenza_backend.session import Session
from cadenza_backend.wire import (
    AgentMessage,
    AgentPhase,
    AgentStatus,
    BlobRef,
    BuildStats,
    ErrorMessage,
    GeometryFailed,
    GeometryReady,
    LedgerUpdated,
    SpatialHit,
    SpatialResult,
    Target,
)

Emit = Callable[[BaseModel], Awaitable[None]]
EmitBinary = Callable[[bytes], Awaitable[None]]


async def handle_prompt(
    session: Session,
    message_id: str,
    prompt: str,
    target: Target | None,
    emit: Emit,
    *,
    images: list[tuple[str, bytes]] | None = None,
    emit_binary: EmitBinary | None = None,
    client: ModelClient | None = None,
) -> None:
    """Run one user instruction end to end, streaming progress through `emit`."""
    client = client or ModelClient()
    images = images or []
    started = time.perf_counter()

    async def status(phase: AgentPhase, detail: str = "") -> None:
        await emit(
            AgentStatus(
                message_id=message_id,
                phase=phase,
                detail=detail,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        )

    try:
        # ── 1. Resolve what the user clicked ────────────────────────────────
        resolved = None
        if target is not None:
            await status(AgentPhase.LOCATING, "Working out what you selected.")
            resolved = geometry_bridge.probe(
                session.store.ledger, target.point, target.normal
            )
            await emit(
                SpatialResult(
                    message_id=message_id,
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

        # ── 2. Route. A rule, not a model call ──────────────────────────────
        decision = route(
            session.store.ledger,
            prompt,
            has_target=target is not None,
            has_images=bool(images),
        )
        await status(AgentPhase.ROUTING, decision.reason)

        # ── 3. Engineer's smart defaults, folded into the prompt ────────────
        hints = engineer.hints_for(prompt)
        hint_block = engineer.format_hints(hints)
        enriched_prompt = f"{prompt}\n\n{hint_block}" if hint_block else prompt

        # ── 4. Run the agent. Both paths produce a patch ────────────────────
        if decision.agent is AgentKind.DRAFTSMAN:
            await status(
                AgentPhase.THINKING,
                "Reading the sketch." if images else "Drafting the part.",
            )
            features, output = await run_draftsman(
                enriched_prompt, images=images, client=client
            )
            summary = output.summary
            assumptions = parse_assumptions(output.assumptions)
            needs_clarification = bool(output.needs_clarification)
            clarification_text = output.needs_clarification or ""
            ops = [
                PatchOp(op="add", path="/features/-", value=f.model_dump(mode="json"))
                for f in features
            ]
        else:
            await status(AgentPhase.THINKING, "Working out the edit.")
            patch_output: AgentPatchOutput = await run_machinist(
                session.store.ledger,
                enriched_prompt,
                resolved.descriptor if resolved else "",
                client=client,
            )
            summary = patch_output.summary
            assumptions = parse_assumptions(patch_output.assumptions)
            needs_clarification = bool(patch_output.needs_clarification)
            clarification_text = summary
            ops = list(patch_output.patch)

        # ── 5. The agent could not proceed — say so and stop ────────────────
        if needs_clarification and not ops:
            await emit(
                AgentMessage(
                    message_id=message_id,
                    text=clarification_text
                    or summary
                    or "I need one more detail before I can build that.",
                    assumptions=assumptions,
                    needs_clarification=True,
                )
            )
            await status(AgentPhase.DONE)
            return

        if not ops:
            await emit(
                ErrorMessage(
                    message_id=message_id,
                    code=ErrorCode.AGENT_INVALID_OUTPUT,
                    message="The agent returned no changes and no question.",
                    detail=summary,
                )
            )
            return

        # ── 6. Stage the patch. The store is NOT touched yet ────────────────
        await status(AgentPhase.PATCHING, f"Applying {len(ops)} change(s).")
        candidate = session.store.stage(ops)

        # ── 7. Build the candidate. Only a success gets committed ───────────
        await status(AgentPhase.BUILDING, "Rebuilding geometry.")
        outcome = geometry_bridge.build(candidate.ledger)

        if not outcome.ok:
            # The store never saw the candidate, so the client stays exactly
            # where it was — which is what `current_revision` tells it.
            await emit(
                GeometryFailed(
                    message_id=message_id,
                    current_revision=session.store.ledger.revision,
                    code=outcome.code or ErrorCode.GEOMETRY_KERNEL_FAILURE,
                    message=outcome.detail,
                    feature_id=outcome.feature_id,
                )
            )
            await status(AgentPhase.DONE)
            return

        session.store.commit(candidate)
        revision = session.store.mark_built()

        await emit(
            AgentMessage(message_id=message_id, text=summary, assumptions=assumptions)
        )
        await emit(
            LedgerUpdated(
                message_id=message_id,
                revision=revision,
                ledger=session.store.ledger,
                patch=candidate.ops,
                summary=summary,
            )
        )

        # ── 8. Publish the bytes ────────────────────────────────────────────
        await status(AgentPhase.EXPORTING, "Sending the model.")
        blobs = _store_artifacts(session, revision, outcome)
        await emit(
            GeometryReady(
                message_id=message_id,
                revision=revision,
                blobs=blobs,
                stats=BuildStats(
                    bbox=outcome.bbox,
                    volume_mm3=outcome.volume_mm3,
                    face_count=outcome.face_count,
                    build_ms=outcome.build_ms,
                ),
                warnings=outcome.warnings,
                stub=outcome.stub,
            )
        )
        if emit_binary is not None:
            for frame in _blob_frames(revision, outcome):
                await emit_binary(frame)

        await status(AgentPhase.DONE, outcome.detail)

    # ── failure mapping: every error reaches the client as a coded frame ────
    except AINotConfigured as exc:
        await emit(
            ErrorMessage(
                message_id=message_id,
                code=ErrorCode.AGENT_UNAVAILABLE,
                message=str(exc),
                recoverable=False,
            )
        )
    except AIRefused as exc:
        await emit(
            ErrorMessage(
                message_id=message_id, code=ErrorCode.AGENT_REFUSED, message=str(exc)
            )
        )
    except AIError as exc:
        await emit(
            ErrorMessage(
                message_id=message_id,
                code=ErrorCode.AGENT_INVALID_OUTPUT,
                message=str(exc),
            )
        )
    except CadenzaError as exc:
        # Patch/ledger failures land here. The ledger is unchanged by construction.
        await emit(
            ErrorMessage(
                message_id=message_id,
                code=exc.code,
                message=exc.message,
                detail=exc.detail,
                recoverable=exc.recoverable,
            )
        )
    except Exception as exc:  # noqa: BLE001 — the socket must never die silently
        await emit(
            ErrorMessage(
                message_id=message_id,
                code=ErrorCode.INTERNAL,
                message="Unexpected server error.",
                detail=f"{type(exc).__name__}: {exc}",
            )
        )


def _store_artifacts(
    session: Session, revision: int, outcome: geometry_bridge.BuildOutcome
) -> list[BlobRef]:
    """Put the bytes in the session store and describe them for the manifest."""
    refs: list[BlobRef] = []
    for kind, data, content_type in (
        ("glb", outcome.glb, geometry_bridge.GLB_CONTENT_TYPE),
        ("step", outcome.step, geometry_bridge.STEP_CONTENT_TYPE),
    ):
        if not data:
            continue
        artifact = session.artifacts.put(revision, kind, content_type, data)
        refs.append(
            BlobRef(
                artifact=kind,
                content_type=content_type,
                bytes=len(data),
                sha256=artifact.sha256,
                url=artifact.url_for(session.id),
            )
        )
    return refs


def _blob_frames(revision: int, outcome: geometry_bridge.BuildOutcome) -> list[bytes]:
    """Length-prefixed binary frames, using the contract's shared codec.

    Hand-rolling this framing is explicitly forbidden by CONTRACTS.md §2.4 —
    both ends must use the same tested encoder, which lives in the shared
    package alongside the TypeScript mirror the browser decodes with.
    """
    from cadenza_contracts.messages import BlobHeader, encode_blob_frame

    frames: list[bytes] = []
    for kind, data in (("glb", outcome.glb), ("step", outcome.step)):
        if not data:
            continue
        frames.append(
            encode_blob_frame(
                BlobHeader(revision=revision, artifact=kind, bytes=len(data)), data
            )
        )
    return frames
