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
from cadenza_contracts.revolve import revolve_reason
from cadenza_contracts.gear_authoring import gear_reason


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
        if feature is None or feature.kind not in (DIMENSION_FIELDS.keys() | PATTERN_KINDS | {"revolve", "gear"}):
            raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "Choose a supported source feature.")
        if feature.kind in PATTERN_KINDS:
            reason = pattern_reason(feature.model_dump(mode="json"), source.ledger.model_dump(mode="json"))
            if reason:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, reason)
        if feature.kind == "gear":
            reason = gear_reason(feature.model_dump(mode="json"), source.ledger.model_dump(mode="json"))
            if reason:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, reason)
        if feature.kind == "revolve":
            reason = revolve_reason(feature.model_dump(mode="json"))
            if reason:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, reason)
    if request.images and not get_settings().vision_available and client is None:
        raise CadenzaError(ErrorCode.AGENT_UNAVAILABLE, "No image-capable provider is configured.")
    base = session.snapshot()
    context_path = (request.edit_context.component_path if request.edit_context else None) or (request.face.component_path if request.face else None)
    try:
        uses = [row for row in assembly_instances(base, session.primary_ledger_id) if row.source_id == request.ledger_id]
    except ValueError as exc:
        raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "The assembly context cannot be resolved. Repair it before editing.") from exc
    selected = next((row for row in uses if row.path == tuple(context_path)), None) if context_path else None
    if context_path is not None and selected is None:
        raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "This occurrence no longer uses the selected part. Select it again.")
    confirmed = bool(request.edit_context and request.edit_context.shared_source_confirmed)
    action = request.face.mode if request.face else "edit_feature" if request.feature_id else "add_to_part"
    event("source_scope.admitted", ledger_id=request.ledger_id, component_path=context_path,
        revision=request.base_revision, action=action, affected_count=len(uses), shared_source_confirmed=confirmed,
        allowed=len(uses) <= 1 or confirmed)
    if len(uses) > 1 and not confirmed:
        await emit(AgentMessage(message_id=request.message_id, needs_clarification=True,
            text=f"{source.ledger.metadata.name} is used by {len(uses)} placed parts. Confirm shared geometry editing in the Assistant, or use Make independent for one selected part."))
        await emit(AgentStatus(message_id=request.message_id, phase=AgentPhase.DONE, detail="Choose the edit scope before continuing."))
        return
    scope_context = dict(source_name=source.ledger.metadata.name, action=action,
        selected_occurrence=dict(path=list(selected.path), names=list(selected.names)) if selected else None,
        affected_occurrences=[dict(path=list(row.path), names=list(row.names)) for row in uses],
        shared_source_confirmed=confirmed, units="mm", coordinate_frame="source_local")
    face_context = None
    if request.face:
        face = request.face
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
        result = await interpret(request, source.ledger.model_copy(deep=True), client,
            face_context=face_context, scope_context=scope_context)
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
    if (request.face and request.face.mode == "edit_feature" and not isinstance(result, (Clarification, Unsupported))
            and any(op.op.startswith("add_") for op in result.operations)):
        raise CadenzaError(ErrorCode.BAD_MESSAGE, "This action edits the selected feature. Choose Add on this face for additions.")
    if isinstance(result, (Clarification, Unsupported)):
        await emit(AgentMessage(message_id=request.message_id,
            text=result.question if isinstance(result, Clarification) else result.reason,
            needs_clarification=isinstance(result, Clarification)))
    elif any((request.feature_id is not None if op.op in ("add_feature", "add_linear_pattern", "add_circular_pattern", "add_revolve", "add_gear")
              else request.feature_id is None or op.feature_id != request.feature_id) for op in result.operations):
        await emit(AgentMessage(message_id=request.message_id, needs_clarification=True,
            text="Edit with AI changes only the selected feature. Use Add with AI for additions; other existing features cannot change in this request."))
    else:
        if request.face:
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
            context_path=context_path, scope_summary=(
                f"{'Edit selected feature' if request.feature_id else 'Add on selected face' if request.face else 'Add to part'}: {source.ledger.metadata.name}. "
                + (f"Selected part: {' / '.join(selected.names)}. " if selected else "")
                + (f"Shared geometry: all {len(uses)} placed uses change on Apply. " if len(uses) > 1 else
                   "Geometry used by one placed part. " if uses else "Standalone or unplaced source geometry. ")
                + "Attachments may reposition connected parts. Review before Apply."))
    await emit(AgentStatus(message_id=request.message_id, phase=AgentPhase.DONE, detail="Review the response before continuing."))
