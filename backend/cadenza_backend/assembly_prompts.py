"""Pinned assembly interpretation; the existing compiler and preview own changes."""
import asyncio
import math
import re

from httpx import HTTPError
from pydantic import ValidationError

from cadenza_backend.agents.assembly_intents import Clarification, Unsupported, interpret
from cadenza_backend.agents.model_client import AIError, AINotConfigured, AIRefused
from cadenza_backend.assembly_commands import capabilities, resolve_owner, source_uses, validate_source, handle_assembly_command
from cadenza_backend.config import get_settings
from cadenza_backend.edit_preview import pending
from cadenza_contracts.assembly_commands import (AssemblyCommand, AssemblyCapabilitiesRequest,
    AssemblyPromptCapabilitiesRequest, AssemblyPromptCapabilitiesResponse, InstanceTarget, AttachmentTarget)
from cadenza_contracts.errors import CadenzaError, ErrorCode
from cadenza_contracts.messages import AssemblyPrompt, AgentMessage, AgentStatus, AgentPhase


def discovery(session, request: AssemblyPromptCapabilitiesRequest):
    cap = capabilities(session, AssemblyCapabilitiesRequest(message_id=request.message_id, version=2,
        ledger_id=request.ledger_id, owner_path=request.owner_path))
    reason = ("The design changed. Refresh the assembly context." if request.base_revision != session.revision else
              cap.reason or (None if get_settings().ai_available else "No AI provider is configured."))
    return AssemblyPromptCapabilitiesResponse(message_id=request.message_id, session_id=session.id,
        revision=session.revision, ledger_id=request.ledger_id, owner_path=request.owner_path,
        available=reason is None, vision_available=get_settings().vision_available, reason=reason)


def validate_target(owner, target):
    components = {c.id: c for c in owner.assembly.components} if owner.assembly else {}
    joints = {j.id: j for j in owner.assembly.joints} if owner.assembly else {}
    if isinstance(target, InstanceTarget):
        if target.component_id not in components or any(jid not in joints or target.component_id not in
                (joints[jid].component_a, joints[jid].component_b) for jid in target.detach_joint_ids):
            raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "Select an existing direct instance and its incident attachments.")
    if isinstance(target, AttachmentTarget):
        joint = joints.get(target.joint_id) if target.joint_id else None
        if not {target.component_a, target.component_b} <= components.keys() or (target.joint_id and
                (joint is None or (joint.component_a, joint.component_b) != (target.component_a, target.component_b))):
            raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "Select an existing ordered pair and matching attachment.")


def validate_operations(target, operations):
    for operation in operations:
        allowed = False
        if target.kind == "definition":
            allowed = operation.op in ("initialize_assembly", "add_instance", "create_subassembly")
        elif target.kind == "instance":
            if operation.op in ("rename_instance", "set_instance_placement", "duplicate_instance", "remove_instance"):
                allowed = operation.component_id == target.component_id
            elif operation.op == "remove_joint":
                allowed = operation.joint_id in target.detach_joint_ids
        else:
            if operation.op == "remove_joint":
                allowed = operation.joint_id == target.joint_id
            elif operation.op == "add_rigid_joint":
                allowed = (operation.component_a, operation.component_b) == (target.component_a, target.component_b)
        if not allowed:
            raise CadenzaError(ErrorCode.AGENT_INVALID_OUTPUT, "The proposal changed something outside the selected assembly target.")
    if target.kind == "attachment":
        added = sum(op.op == "add_rigid_joint" for op in operations)
        removed = sum(op.op == "remove_joint" for op in operations)
        if added > 1 or (target.joint_id and removed != 1):
            raise CadenzaError(ErrorCode.AGENT_INVALID_OUTPUT, "Replace or detach the selected attachment explicitly.")


def verify_numeric_request(request, owner, operations):
    if request.target.kind != "instance":
        return
    number = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    text = request.prompt.strip()
    absolute = re.fullmatch(rf"set ([xyz]) to {number} mm[.!]?", text, re.I)
    offset = re.fullmatch(rf"move by {number} mm along assembly ([xyz])[.!]?", text, re.I)
    rotation = re.fullmatch(rf"set rotation ([xyz]) to {number} degrees[.!]?", text, re.I)
    if not (absolute or offset or rotation):
        return
    component = next(c for c in owner.assembly.components if c.id == request.target.component_id)
    expected = {"origin": list(component.placement.origin), "rotation_deg": list(component.placement.rotation_deg)}
    match = absolute or rotation
    if match:
        axis, value = match[1].lower(), float(match[2])
    else:
        assert offset is not None
        axis, value = offset[2].lower(), float(offset[1])
    field = "rotation_deg" if rotation else "origin"
    index = "xyz".index(axis)
    expected[field][index] = expected[field][index] + value if offset else value
    moves = [op for op in operations if op.op == "set_instance_placement"]
    if len(moves) != 1 or any(op.op not in ("set_instance_placement", "remove_joint") for op in operations) or any(
        not math.isclose(a, b, rel_tol=0, abs_tol=1e-7)
        for key, values in expected.items() for a, b in zip(values, getattr(moves[0].placement, key))
    ):
        raise CadenzaError(ErrorCode.AGENT_INVALID_OUTPUT, "The proposal must match the requested axis and value and preserve other placement values.")


async def handle_assembly_prompt(session, request: AssemblyPrompt, emit, *, client=None):
    if request.base_revision != session.revision:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The design changed. Refresh before editing.")
    if session.draft or pending(session):
        raise CadenzaError(ErrorCode.BUSY, "Finish the draft or apply/cancel the preview first.")
    cap = capabilities(session, AssemblyCapabilitiesRequest(message_id=request.message_id, version=2,
        ledger_id=request.ledger_id, owner_path=request.owner_path))
    if cap.reason:
        raise CadenzaError(ErrorCode.BAD_MESSAGE, cap.reason)
    owner = resolve_owner(session, request.ledger_id, request.owner_path)
    validate_target(owner, request.target)
    settings = get_settings()
    if client is None and (not settings.ai_available or (request.images and not settings.vision_available)):
        raise CadenzaError(ErrorCode.AGENT_UNAVAILABLE, "No compatible AI provider is configured.")
    base = session.snapshot()
    sources = []
    for lid, ledger in base.items():
        if lid == owner.project_id:
            continue
        try: validate_source(ledger)
        except CadenzaError: continue
        sources.append(dict(id=lid, name=ledger.metadata.name, kind="assembly" if ledger.assembly else "part"))
    context = dict(owner_id=owner.project_id, owner_path=request.owner_path, units="mm/degrees",
        assembly=owner.assembly.model_dump(mode="json", exclude={"parts"}) if owner.assembly else None,
        sources=sources, affected_occurrences=source_uses(base, session.primary_ledger_id, owner.project_id),
        capabilities=cap.model_dump(mode="json"))
    await emit(AgentStatus(message_id=request.message_id, phase=AgentPhase.THINKING, detail="Interpreting the selected assembly change."))
    try:
        result = await interpret(request, context, client)
    except AINotConfigured as exc:
        raise CadenzaError(ErrorCode.AGENT_UNAVAILABLE, "No AI provider is configured.") from exc
    except AIRefused as exc:
        raise CadenzaError(ErrorCode.AGENT_REFUSED, "The provider declined this request.") from exc
    except asyncio.TimeoutError as exc:
        raise CadenzaError(ErrorCode.AGENT_INVALID_OUTPUT, "Assembly interpretation timed out. Revise and try again.") from exc
    except (AIError, ValidationError, ValueError, HTTPError, KeyError, IndexError, TypeError) as exc:
        raise CadenzaError(ErrorCode.AGENT_INVALID_OUTPUT, "The interpreter did not return a valid supported assembly proposal.") from exc
    if session.snapshot() != base or session.revision != request.base_revision:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The design changed during interpretation. Request a new preview.")
    if isinstance(result, (Clarification, Unsupported)):
        await emit(AgentMessage(message_id=request.message_id, text=result.question if isinstance(result, Clarification) else result.reason,
            needs_clarification=isinstance(result, Clarification)))
    else:
        validate_operations(request.target, result.operations)
        verify_numeric_request(request, owner, result.operations)
        command = AssemblyCommand(message_id=request.message_id, base_revision=request.base_revision,
            ledger_id=request.ledger_id, owner_path=request.owner_path, scope="assembly_definition", operations=result.operations)
        await handle_assembly_command(session, command, emit, prompt=request.prompt, assumptions=result.assumptions)
    await emit(AgentStatus(message_id=request.message_id, phase=AgentPhase.DONE, detail="Review the assembly response before continuing."))
