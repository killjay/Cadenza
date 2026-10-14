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

`revision` is assigned by the whole-design transaction in `design.py`.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

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
    Feature,
    PatchOp,
)
from cadenza_backend.session import Session
from cadenza_backend.wire import (
    AgentMessage,
    AgentPhase,
    AgentStatus,
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
    targets: list[Target],
    emit: Emit,
    *,
    images: list[tuple[str, bytes]] | None = None,
    emit_binary: EmitBinary | None = None,
    client: ModelClient | None = None,
    active_ledger_ids: list[str] | None = None,
    preview: bool = False,
) -> None:
    """Run one user instruction end to end, streaming progress through `emit`."""
    client = client or ModelClient(logger=session.logger)
    images = images or []
    targets = targets or []
    active_ledger_ids = active_ledger_ids or []
    started = time.perf_counter()
    session.logger.log("prompt_received", message_id=message_id, prompt=prompt, targets=[t.model_dump() for t in targets])
    
    from cadenza_backend.graph import app
    
    initial_state = {
        "preview": preview,
        "session": session,
        "message_id": message_id,
        "prompt": prompt,
        "targets": targets,
        "images": images,
        "active_ledger_ids": active_ledger_ids,
        "emit": emit,
        "emit_binary": emit_binary,
        "client": client,
        "started": started,
        "resolved_list": [],
        "enriched_prompt": prompt,
        "is_assembly": False,
        "is_edit": False,
        "bom": [],
        "catalog_matches": [],
        "ops": [],
        "summary": "",
        "assumptions": [],
        "needs_clarification": False,
        "clarification_text": "",
        "attempt": 0,
        "candidate": None,
        "outcome": None,
        "done": False,
    }
    
    try:
        await app.ainvoke(initial_state)
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
        await emit(
            ErrorMessage(
                message_id=message_id,
                code=exc.code,
                message=exc.message,
                detail=exc.detail,
                recoverable=exc.recoverable,
            )
        )
    except Exception as exc:  # noqa: BLE001
        await emit(
            ErrorMessage(
                message_id=message_id,
                code=ErrorCode.INTERNAL,
                message="Unexpected server error.",
                detail=f"{type(exc).__name__}: {exc}",
            )
        )


def _assumption_relates_to_failure(feature: Feature | None, assumptions: list[dict[str, Any]]) -> bool:
    if not assumptions:
        return False
    if not feature:
        return len(assumptions) > 0

    for a in assumptions:
        field = str(a.get("field", "")).lower()
        
        # 1. Direct ID match (Machinist patch path)
        if feature.id in field:
            return True
            
        # 2. Name match (Draftsman might include the feature name, e.g., "base plate length")
        if feature.name and feature.name.lower() in field:
            return True
            
        # 3. Unscoped parameter match (Draftsman bare parameter, e.g., "diameter")
        if not ("/" in field or "feat_" in field):
            # Check if this parameter actually exists on this feature
            if field in type(feature.parameters).model_fields or field in type(feature.placement).model_fields:
                return True
            # Also handle if they prefixed it with the kind, e.g., "hole diameter"
            if feature.kind in field:
                return True
                
    return False
