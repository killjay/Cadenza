"""Targeted prompt admission; accepted-state ownership stays in design/preview."""
import asyncio
from httpx import HTTPError
from pydantic import ValidationError

from cadenza_backend.agents.design_intents import Clarification, Unsupported, interpret
from cadenza_backend.agents.model_client import AIError, AINotConfigured, AIRefused, AIProviderError
from cadenza_backend.config import get_settings
from cadenza_backend.design_commands import handle_design_command
from cadenza_backend.edit_preview import pending
from cadenza_backend.geometry_jobs import inspect
from cadenza_backend.assembly_commands import assembly_instances
from cadenza_backend.diagnostics import event
from cadenza_contracts.design_commands import DesignCommand
from cadenza_contracts.errors import CadenzaError, ErrorCode
from cadenza_contracts.feature_capabilities import DIMENSION_FIELDS
from cadenza_contracts.messages import AgentMessage, AgentStatus, AgentPhase, DesignPrompt
from cadenza_contracts.patterns import PATTERN_KINDS, pattern_reason


async def handle_design_prompt(session, request: DesignPrompt, emit, *, client=None):
    if request.base_revision != session.revision:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The design changed. Refresh before editing.")
    if session.draft or pending(session):
        raise CadenzaError(ErrorCode.BUSY, "Finish the draft or apply/cancel the preview first.")
    source = session.stores.get(request.ledger_id)
    if source is None or source.ledger.assembly is not None:
        raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "Choose an existing source part, not an assembly root.")
    if request.feature_id is not None:
        feature = next((f for f in source.ledger.features if f.id == request.feature_id), None)
        if feature is None or feature.kind not in (DIMENSION_FIELDS.keys() | PATTERN_KINDS):
            raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "Choose a supported source feature.")
        if feature.kind in PATTERN_KINDS:
            reason = pattern_reason(feature.model_dump(mode="json"), source.ledger.model_dump(mode="json"))
            if reason:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, reason)
    if request.images and not get_settings().vision_available and client is None:
        raise CadenzaError(ErrorCode.AGENT_UNAVAILABLE, "No image-capable provider is configured.")
    base = session.snapshot()
    face_context = None
    if request.face:
        face = request.face
        if face.component_path is not None:
            try:
                valid = any(row.path == tuple(face.component_path) and row.source_id == request.ledger_id
                    for row in assembly_instances(base, session.primary_ledger_id))
            except ValueError:
                valid = False
            if not valid:
                raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "This occurrence no longer uses the selected part. Select it again.")
        resolved = await inspect(source.ledger, dict(query_type="capture_surface_reference",
            point=face.point, normal=face.normal), base)
        event("source_face.resolved", ledger_id=request.ledger_id, component_path=face.component_path,
            face_mode=face.mode, status=resolved.get("status"))
        if resolved.get("status") != "resolved" or resolved.get("reference") != face.reference.model_dump(mode="json"):
            raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "The selected surface is missing, ambiguous or changed. Select a clear face again.")
        if face.mode == "add_on_face" and not resolved["face"].get("planar"):
            raise CadenzaError(ErrorCode.BAD_MESSAGE, "Add on face requires a planar surface. Use feature controls for other additions.")
        face_context = dict(mode=face.mode, **resolved)
        if session.snapshot() != base or session.revision != request.base_revision:
            raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The design changed while checking the surface. Select it again.")
    await emit(AgentStatus(message_id=request.message_id, phase=AgentPhase.THINKING, detail="Interpreting the selected source edit."))
    try:
        if face_context:
            result = await interpret(request, source.ledger.model_copy(deep=True), client, face_context=face_context)
        else:
            result = await interpret(request, source.ledger.model_copy(deep=True), client)
    except AINotConfigured as exc:
        raise CadenzaError(ErrorCode.AGENT_UNAVAILABLE, "No AI provider is configured.") from exc
    except AIProviderError as exc:
        raise CadenzaError(exc.code, str(exc)) from exc
    except AIRefused as exc:
        raise CadenzaError(ErrorCode.AGENT_REFUSED, "The provider declined this request.") from exc
    except asyncio.TimeoutError as exc:
        raise CadenzaError(ErrorCode.AGENT_TIMEOUT, "Interpretation timed out. Revise the request and try again.") from exc
    except (AIError, ValidationError, ValueError, HTTPError, KeyError, IndexError, TypeError) as exc:
        raise CadenzaError(ErrorCode.AGENT_INVALID_OUTPUT, "The interpreter did not return a valid supported proposal. Revise the request.") from exc
    if session.snapshot() != base or session.revision != request.base_revision:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The design changed during interpretation. Request a new preview.")
    if isinstance(result, (Clarification, Unsupported)):
        await emit(AgentMessage(message_id=request.message_id,
            text=result.question if isinstance(result, Clarification) else result.reason,
            needs_clarification=isinstance(result, Clarification)))
    elif any(op.op not in ("add_feature", "add_linear_pattern", "add_circular_pattern") and op.feature_id != request.feature_id for op in result.operations):
        await emit(AgentMessage(message_id=request.message_id, needs_clarification=True,
            text="Select the exact feature to modify using Edit with AI. Other existing features cannot change in this request."))
    else:
        if request.face:
            if request.face.mode == "edit_feature" and any(op.op.startswith("add_") for op in result.operations):
                raise CadenzaError(ErrorCode.BAD_MESSAGE, "This action edits the selected feature. Choose Add on this face for additions.")
            if request.face.mode == "add_on_face":
                check = await inspect(source.ledger, dict(query_type="validate_face_additions",
                    reference=request.face.reference.model_dump(mode="json"),
                    operations=[op.model_dump(mode="json") for op in result.operations]), base)
                event("source_face.addition_checked", ledger_id=request.ledger_id, status=check.get("status"))
                if check.get("status") != "resolved":
                    raise CadenzaError(ErrorCode.BAD_MESSAGE, check.get("message", "This addition cannot be verified on the selected face."))
            if session.snapshot() != base or session.revision != request.base_revision:
                raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The design changed during validation. Request a new preview.")
        command = DesignCommand(message_id=request.message_id, base_revision=request.base_revision,
            ledger_id=request.ledger_id, scope="source", operations=result.operations)
        await handle_design_command(session, command, emit, prompt=request.prompt,
            assumptions=result.assumptions, verification_scope=(request.ledger_id, request.feature_id),
            context_path=request.face.component_path if request.face else None)
    await emit(AgentStatus(message_id=request.message_id, phase=AgentPhase.DONE, detail="Review the response before continuing."))
