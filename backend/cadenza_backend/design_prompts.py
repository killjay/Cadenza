"""Targeted prompt admission; accepted-state ownership stays in design/preview."""
import asyncio
from httpx import HTTPError
from pydantic import ValidationError

from cadenza_backend.agents.design_intents import Clarification, Unsupported, interpret
from cadenza_backend.agents.model_client import AIError, AINotConfigured, AIRefused
from cadenza_backend.config import get_settings
from cadenza_backend.design_commands import handle_design_command
from cadenza_backend.edit_preview import pending
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
    await emit(AgentStatus(message_id=request.message_id, phase=AgentPhase.THINKING, detail="Interpreting the selected source edit."))
    try:
        result = await interpret(request, source.ledger.model_copy(deep=True), client)
    except AINotConfigured as exc:
        raise CadenzaError(ErrorCode.AGENT_UNAVAILABLE, "No AI provider is configured.") from exc
    except AIRefused as exc:
        raise CadenzaError(ErrorCode.AGENT_REFUSED, "The provider declined this request.") from exc
    except asyncio.TimeoutError as exc:
        raise CadenzaError(ErrorCode.AGENT_INVALID_OUTPUT, "Interpretation timed out. Revise the request and try again.") from exc
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
        command = DesignCommand(message_id=request.message_id, base_revision=request.base_revision,
            ledger_id=request.ledger_id, scope="source", operations=result.operations)
        await handle_design_command(session, command, emit, prompt=request.prompt,
            assumptions=result.assumptions, verification_scope=(request.ledger_id, request.feature_id))
    await emit(AgentStatus(message_id=request.message_id, phase=AgentPhase.DONE, detail="Review the response before continuing."))
