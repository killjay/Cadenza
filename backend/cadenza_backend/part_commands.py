"""One private source per request; Apply remains the sole acceptance owner."""
import asyncio

from httpx import HTTPError
from pydantic import ValidationError

from cadenza_backend.agents.design_intents import Clarification, Unsupported, interpret
from cadenza_backend.agents.model_client import AIError, AINotConfigured, AIRefused, AIProviderError
from cadenza_backend.config import get_settings
from cadenza_backend.contracts_bridge import CadenzaError, ErrorCode, PatchOp
from cadenza_backend.design_commands import compile_source_command
from cadenza_backend.edit_preview import pending, publish_preview
from cadenza_backend.geometry_jobs import build_design
from cadenza_backend.ledger_store import LedgerStore, apply_patch
from cadenza_contracts.design_commands import DesignCommand
from cadenza_contracts.messages import AgentMessage, AgentPhase, AgentStatus, DesignPrompt, PartPrompt
from cadenza_contracts.part_commands import MAX_SOURCE_PARTS, PartCreate, PartRequest, PartRename


def admit_creation(session, request: PartRequest) -> None:
    if request.base_revision != session.revision:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The project changed. Refresh before creating a part.")
    if session.draft or pending(session):
        raise CadenzaError(ErrorCode.BUSY, "Finish the draft or apply/cancel the preview first.")
    sources = [s.ledger for s in session.stores.values() if s.ledger.assembly is None and s.ledger.features]
    if len(sources) >= MAX_SOURCE_PARTS:
        raise CadenzaError(ErrorCode.BAD_MESSAGE, "New part creation is limited to 32 source parts.")
    names = [s.ledger.metadata.name for lid, s in session.stores.items()
             if s.ledger.assembly is None and (s.ledger.features or lid != session.primary_ledger_id)]
    if any(name.strip().casefold() == request.name.casefold() for name in names):
        raise CadenzaError(ErrorCode.BAD_MESSAGE, "A source part already has this name. Choose another name.")


async def create_part(session, request: PartCreate, emit, *, prompt="", assumptions=None):
    admit_creation(session, request)
    source = LedgerStore.new(request.name).ledger
    command = DesignCommand.model_validate(dict(message_id=request.message_id, base_revision=request.base_revision,
        ledger_id=source.project_id, operations=[op.model_dump(mode="json") for op in request.operations]))
    source = apply_patch(source, compile_source_command(source, command)).ledger
    ledgers = session.stage({source.project_id: source})
    outcome = await build_design(ledgers, session.primary_ledger_id)
    if not outcome.ok:
        raise CadenzaError(outcome.code or ErrorCode.GEOMETRY_KERNEL_FAILURE, outcome.detail)
    await publish_preview(session, ledgers, outcome, request.message_id, emit,
        summary=f"Create source part: {request.name}. No assembly instances are added.",
        prompt=prompt, assumptions=assumptions, display_ledger_id=source.project_id,
        verification_scope=(source.project_id, None))


async def rename_part(session, request: PartRename, emit):
    if request.base_revision != session.revision:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The project changed. Refresh before renaming a part.")
    if session.draft or pending(session):
        raise CadenzaError(ErrorCode.BUSY, "Finish the draft or apply/cancel the preview first.")
    store = session.stores.get(request.ledger_id)
    if store is None or store.ledger.assembly is not None or not store.ledger.features:
        raise CadenzaError(ErrorCode.BAD_MESSAGE, "Choose an existing source part with geometry to rename.")
    source = store.ledger
    if source.metadata.name == request.name:
        raise CadenzaError(ErrorCode.BAD_MESSAGE, "Enter a different part name.")
    if any(lid != request.ledger_id and other.ledger.assembly is None
           and (other.ledger.features or lid != session.primary_ledger_id)
           and other.ledger.metadata.name.strip().casefold() == request.name.casefold()
           for lid, other in session.stores.items()):
        raise CadenzaError(ErrorCode.BAD_MESSAGE, "A source part already has this name. Choose another name.")
    patch = [PatchOp(op="replace", ledger_id=request.ledger_id, path="/metadata/name", value=request.name)]
    renamed = apply_patch(source, patch).ledger
    ledgers = session.stage({request.ledger_id: renamed})
    outcome = await build_design(ledgers, session.primary_ledger_id)
    if not outcome.ok:
        raise CadenzaError(outcome.code or ErrorCode.GEOMETRY_KERNEL_FAILURE, outcome.detail)
    await publish_preview(session, ledgers, outcome, request.message_id, emit,
        patch=[op.model_dump(mode="json", by_alias=True) for op in patch],
        summary=f"Rename part: {source.metadata.name} to {request.name}. Instance names and geometry stay unchanged.",
        display_ledger_id=request.ledger_id, verification_scope=(request.ledger_id, None))


async def prompt_part(session, request: PartPrompt, emit, *, client=None):
    admit_creation(session, request)
    if request.images and not get_settings().vision_available and client is None:
        raise CadenzaError(ErrorCode.AGENT_UNAVAILABLE, "No image-capable provider is configured.")
    base = session.snapshot()
    source = LedgerStore.new(request.name).ledger
    interpretation = DesignPrompt(message_id=request.message_id, base_revision=request.base_revision,
        ledger_id=source.project_id, scope="source", prompt=request.prompt, images=request.images)
    await emit(AgentStatus(message_id=request.message_id, phase=AgentPhase.THINKING, detail="Interpreting a new independent part."))
    try:
        result = await interpret(interpretation, source, client, new_part=True)
    except AINotConfigured as exc:
        raise CadenzaError(ErrorCode.AGENT_UNAVAILABLE, "No AI provider is configured.") from exc
    except AIProviderError as exc:
        raise CadenzaError(exc.code, str(exc)) from exc
    except AIRefused as exc:
        raise CadenzaError(ErrorCode.AGENT_REFUSED, "The provider declined this request.") from exc
    except asyncio.TimeoutError as exc:
        raise CadenzaError(ErrorCode.AGENT_TIMEOUT, "Part interpretation timed out. Try again later.") from exc
    except (AIError, ValidationError, ValueError, HTTPError, KeyError, IndexError, TypeError) as exc:
        raise CadenzaError(ErrorCode.AGENT_INVALID_OUTPUT, "No valid supported part proposal was returned. Revise the request.") from exc
    if session.snapshot() != base or session.revision != request.base_revision:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The project changed during interpretation.")
    if isinstance(result, (Clarification, Unsupported)):
        await emit(AgentMessage(message_id=request.message_id,
            text=result.question if isinstance(result, Clarification) else result.reason,
            needs_clarification=isinstance(result, Clarification)))
    else:
        try:
            creation = PartCreate.model_validate(dict(message_id=request.message_id,
                base_revision=request.base_revision, name=request.name,
                operations=[op.model_dump(mode="json") for op in result.operations]))
        except ValidationError as exc:
            raise CadenzaError(ErrorCode.AGENT_INVALID_OUTPUT,
                "New parts require primitive creation operations only. Add dependent features after Apply.") from exc
        await create_part(session, creation, emit, prompt=request.prompt, assumptions=result.assumptions)
    await emit(AgentStatus(message_id=request.message_id, phase=AgentPhase.DONE, detail="Review the response before continuing."))
