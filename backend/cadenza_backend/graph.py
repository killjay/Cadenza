import time
import json
import re
from typing import TypedDict, Callable, Any, Awaitable
from typing_extensions import NotRequired
from langgraph.graph import StateGraph, END
from pydantic import BaseModel

from cadenza_backend import geometry_bridge
from cadenza_backend.design import accept_design, stage_operations
from cadenza_backend.edit_preview import publish_preview
from cadenza_backend.geometry_jobs import build_design, probe
from cadenza_backend.agents import engineer
from cadenza_backend.agents.coordinator import AgentKind, route
from cadenza_backend.agents.architect import run_architect
from cadenza_backend.agents.librarian import run_librarian
from cadenza_backend.agents.draftsman import parse_assumptions, run_draftsman
from cadenza_backend.agents.machinist import run_machinist
from cadenza_backend.agents.assembler import run_assembler
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.contracts_bridge import Assumption, PatchOp, ErrorCode, CadenzaError
from cadenza_backend.session import Session
from cadenza_backend.surface_references import resolve_saved_target, resolve_surface_target
from cadenza_backend.profile_selection import verify_selected_diameter
from cadenza_backend.wire import (
    AgentMessage,
    AgentPhase,
    AgentStatus,
    ErrorMessage,
    GeometryFailed,
    SpatialHit,
    SpatialResult,
    Target,
)

class AgentState(TypedDict):
    preview: NotRequired[bool]
    session: Session
    message_id: str
    prompt: str
    targets: list[Target]
    images: list[tuple[str, bytes]]
    active_ledger_ids: list[str]
    emit: Callable[[BaseModel], Awaitable[None]]
    emit_binary: Callable[[bytes], Awaitable[None]] | None
    client: ModelClient
    
    started: float
    resolved_list: list[Any]
    enriched_prompt: str
    
    is_assembly: bool
    is_edit: bool
    bom: list[dict]
    catalog_matches: list[dict]
    
    ops: list[PatchOp]
    summary: str
    assumptions: list[Assumption]
    needs_clarification: bool
    clarification_text: str
    
    attempt: int
    candidate: Any
    built_ledgers: Any
    outcome: Any
    done: bool

async def emit_status(state: AgentState, phase: AgentPhase, detail: str = "") -> None:
    elapsed_ms = int((time.perf_counter() - state["started"]) * 1000)
    await state["emit"](
        AgentStatus(
            message_id=state["message_id"],
            phase=phase,
            detail=detail,
            elapsed_ms=elapsed_ms
        )
    )

async def setup_node(state: AgentState) -> dict:
    session = state["session"]
    message_id = state["message_id"]
    targets = state["targets"]
    emit = state["emit"]
    
    resolved_list = []
    if targets:
        await emit_status(state, AgentPhase.LOCATING, "Working out what you selected.")
        primary_store = session.stores[session.primary_ledger_id]
        for target in targets:
            documents = {lid: s.ledger for lid, s in session.stores.items()}
            identity = target.surface_reference
            if target.saved_reference is not None:
                owner = documents.get(target.saved_reference.ledger_id)
                identity = owner.surface_references.get(target.saved_reference.reference_id) if owner else None
            resolved = (await resolve_saved_target(documents, target.saved_reference) if target.saved_reference is not None
                        else await resolve_surface_target(documents, target.surface_reference) if target.surface_reference is not None
                        else await probe(primary_store.ledger, target.point, target.normal, all_ledgers=documents))
            if identity is not None and identity.feature_kind == "extrude":
                if identity.component_path and not re.match(r"\s*(?:all instances|every instance|shared source)\s*:", state["prompt"], re.I):
                    raise CadenzaError(ErrorCode.AGENT_NEEDS_CLARIFICATION,
                        "This part uses shared source geometry. To change every instance, begin the request with 'All instances:'. Instance-only shape editing is not supported yet.")
                if identity.entity_id is None and not any(word in state["prompt"].lower() for word in ("thick", "depth", "height", "width", "length", "extrus")):
                    raise CadenzaError(ErrorCode.AGENT_NEEDS_CLARIFICATION,
                        "You selected an extrusion cap. Which dimension should change? Select a bore to edit a particular opening.")
            if resolved.feature_id:
                await emit(SpatialResult(
                    message_id=message_id,
                    hit=SpatialHit(
                        component_id=resolved.component_id,
                        point=resolved.point, normal=resolved.normal, distance_mm=resolved.distance_mm,
                        feature_id=resolved.feature_id, feature_name=resolved.feature_name,
                        feature_kind=resolved.feature_kind, descriptor=resolved.descriptor,
                        confidence=resolved.confidence
                    )
                ))
            resolved_list.append(resolved)
            
    hints = engineer.hints_for(state["prompt"])
    hint_block = engineer.format_hints(hints)
    enriched_prompt = f"{state['prompt']}\n\n{hint_block}" if hint_block else state["prompt"]
    
    return {"resolved_list": resolved_list, "enriched_prompt": enriched_prompt, "attempt": 0, "done": False}

async def architect_node(state: AgentState) -> dict:
    session = state["session"]
    primary_store = session.stores[session.primary_ledger_id]
    decision = route(primary_store.ledger, state["enriched_prompt"],
                     has_target=bool(state["targets"]), has_images=bool(state["images"]))
    await emit_status(state, AgentPhase.ROUTING, decision.reason)
    if decision.agent is AgentKind.MACHINIST:
        return {"is_edit": True, "is_assembly": False, "bom": []}
        
    out = await run_architect(state["enriched_prompt"], primary_store.ledger, state["client"],
        images=state["images"], all_ledgers={lid: store.ledger for lid, store in session.stores.items()},
        active_ledger_ids=state["active_ledger_ids"])
    return {
        "is_assembly": out.is_assembly,
        "is_edit": out.is_edit,
        "bom": [b.model_dump() for b in out.bom]
    }

async def librarian_node(state: AgentState) -> dict:
    from uuid import uuid4
    from cadenza_contracts.ledger import Part

    if not any(item.get("is_standard") for item in state["bom"]):
        return {"catalog_matches": [], "ops": []}
    await emit_status(state, AgentPhase.THINKING, "Sourcing standard parts from catalog.")
    _, matches = await run_librarian(state["enriched_prompt"] + "\n\nRequested BOM:\n" + json.dumps(state["bom"]),
                                    state["client"], images=state["images"])
    sourced = []
    problems = []
    if not matches:
        problems.append("No standard parts were sourced.")
    for match in matches:
        choices = match.get("matches") or []
        if not choices:
            problems.append("A requested catalog part has no match.")
            continue
        part_def = choices[0]
        if part_def.get("type") != "Hexagon Head Bolt":
            problems.append(f"Catalog geometry for {part_def.get('type', 'this part')} is not supported yet.")
            continue
        params = part_def.get("parameters") or {}
        try:
            designation = params["thread"]
            if not isinstance(designation, str) or not designation.startswith("M"):
                raise ValueError("Expected a metric thread designation")
            part = Part.model_validate({
                "id": f"part_{uuid4().hex}", "name": str(part_def["sku"]),
                "features": [{"id": f"feat_{uuid4().hex}", "kind": "fastener", "operation": "add",
                    "parameters": {"designation": designation, "diameter": float(designation[1:]),
                                   "length": params["length"], "head_type": "hex"}}],
            })
        except (KeyError, TypeError, ValueError):
            problems.append("A catalog bolt is missing valid thread or length dimensions.")
            continue
        sourced.append({**match, "generated_part": part.model_dump(mode="json")})
    if problems:
        return {"catalog_matches": [], "ops": [], "needs_clarification": True,
                "clarification_text": " ".join(problems) + " Please choose supported parts or provide existing part ledgers."}
    return {"catalog_matches": sourced, "ops": []}

async def draftsman_node(state: AgentState) -> dict:
    await emit_status(state, AgentPhase.THINKING, "Drafting part geometry." if state["attempt"] == 0 else "Fixing geometry conflict.")
    
    prompt = state["enriched_prompt"]
    if state["attempt"] > 0 and state.get("outcome"):
        prompt += f"\n\n[SYSTEM: Your previous attempt failed with CAD error: {state['outcome'].detail}. Fix it.]"
        
    target_ledger_id = state["active_ledger_ids"][0] if state["active_ledger_ids"] else state["session"].primary_ledger_id
    features, output = await run_draftsman(prompt, images=state["images"], client=state["client"])
    ops = [PatchOp(op="add", path="/features/-", value=f.model_dump(mode="json"), ledger_id=target_ledger_id) for f in features]
    if output.relationships is not None:
        existing = state["session"].stores[target_ledger_id].ledger.relationships
        if existing.parameters or existing.bindings or existing.constraints:
            return {"ops": [], "needs_clarification": True,
                    "clarification_text": "This part already has relationships. Edit its existing parameters instead of replacing them."}
        ops.append(PatchOp(op="add", path="/relationships", value=output.relationships, ledger_id=target_ledger_id))
    
    return {
        "ops": ops,
        "summary": output.summary,
        "assumptions": parse_assumptions(output.assumptions),
        "needs_clarification": bool(output.needs_clarification),
        "clarification_text": output.needs_clarification or ""
    }

async def machinist_node(state: AgentState) -> dict:
    await emit_status(state, AgentPhase.THINKING, "Working out the edit." if state["attempt"] == 0 else "Fixing geometry conflict.")
    
    prompt = state["enriched_prompt"]
    if state.get("preview"):
        prompt += ("\nPropose a candidate for user review. Preserve unrelated features and dimensions. "
            "For added holes, clarify missing diameter or placement rather than inventing critical dimensions. "
            "Describe assumptions explicitly. Do not claim the candidate is already applied.")
    if state["attempt"] > 0 and state.get("outcome"):
        prompt += f"\n\n[SYSTEM: Your previous attempt failed with CAD error: {state['outcome'].detail}. Fix it.]"
        
    combined_descriptor = "The user did not click anything."
    if state["resolved_list"]:
        descriptors = [r.descriptor + f"\nResolved surface in ledger {r.ledger_id or state['session'].primary_ledger_id}: "
                       f"point={r.point}; normal={r.normal}; confidence={r.confidence}."
                       for r in state["resolved_list"] if r.descriptor]
        if len(descriptors) == 1:
            combined_descriptor = descriptors[0]
        elif len(descriptors) > 1:
            combined_descriptor = "Selected multiple features:\n" + "\n".join(f"- {d}" for d in descriptors)

    session = state["session"]
    if state["active_ledger_ids"]:
        target_ledgers = {lid: session.stores[lid].ledger for lid in state["active_ledger_ids"] if lid in session.stores}
    else:
        target_ledgers = {lid: store.ledger for lid, store in session.stores.items()}

    if not target_ledgers:
        target_ledgers = {session.primary_ledger_id: session.stores[session.primary_ledger_id].ledger}

    out = await run_machinist(target_ledgers, prompt, combined_descriptor, client=state["client"],
                            all_ledgers={lid: s.ledger for lid, s in session.stores.items()}, images=state["images"])
    return {
        "ops": list(out.patch),
        "summary": out.summary,
        "assumptions": parse_assumptions(out.assumptions),
        "needs_clarification": bool(out.needs_clarification),
        "clarification_text": out.needs_clarification or ""
    }

async def assembler_node(state: AgentState) -> dict:
    if state["needs_clarification"]:
        return {"ops": []}
    await emit_status(state, AgentPhase.THINKING, "Assembling components.")
    session = state["session"]
    primary_ledger = session.stores[session.primary_ledger_id].ledger
    sourced_parts = [match["generated_part"] for match in state["catalog_matches"]]
    context = {
        "primary_ledger": primary_ledger.model_dump(mode="json"),
        "sourced_parts": sourced_parts,
        "secondary_ledgers": {lid: store.ledger.model_dump(mode="json")
                              for lid, store in session.stores.items() if lid != session.primary_ledger_id},
        "bom": state["bom"],
    }
    prompt = state["enriched_prompt"]
    if state["attempt"] > 0 and state.get("outcome"):
        prompt += "\n\nPrevious attempt was rejected: " + state["outcome"].detail
        context["rejected_operations"] = [op.model_dump(mode="json") for op in state["ops"]]
    out = await run_assembler(prompt, json.dumps(context), state["client"], images=state["images"])

    referenced_parts = {component.part_id for component in out.components}
    missing_parts = [part["name"] for part in sourced_parts if part["id"] not in referenced_parts]
    if missing_parts and not out.needs_clarification:
        return {"ops": [], "needs_clarification": True,
                "clarification_text": "The proposed assembly omitted sourced parts: " + ", ".join(missing_parts)
                    + ". Please clarify which parts should be included."}

    # Every attempt starts at the accepted base, never the previous attempt's ops.
    ops = []
    if not out.needs_clarification and (out.components or out.joints):
        if primary_ledger.assembly is None:
            ops.append(PatchOp(op="add", path="/assembly", value={"parts": [], "components": [], "joints": []},
                               ledger_id=session.primary_ledger_id))
        ops.extend(PatchOp(op="add", path="/assembly/parts/-", value=part, ledger_id=session.primary_ledger_id)
                   for part in sourced_parts)
        ops.extend(PatchOp(op="add", path="/assembly/components/-", value=c.model_dump(mode="json"),
                           ledger_id=session.primary_ledger_id) for c in out.components)
        ops.extend(PatchOp(op="add", path="/assembly/joints/-", value=j.model_dump(mode="json"),
                           ledger_id=session.primary_ledger_id) for j in out.joints)
    return {
        "ops": ops, "summary": out.summary, "assumptions": parse_assumptions(out.assumptions),
        "needs_clarification": bool(out.needs_clarification),
        "clarification_text": out.needs_clarification or "",
    }

async def build_node(state: AgentState) -> dict:
    session = state["session"]
    message_id = state["message_id"]
    emit = state["emit"]
    
    if state["needs_clarification"]:
        await emit(AgentMessage(
            message_id=message_id,
            text=state["clarification_text"] or state["summary"] or "Need more details.",
            assumptions=state["assumptions"],
            needs_clarification=True,
        ))
        await emit_status(state, AgentPhase.DONE)
        return {"done": True, "ops": []}
        
    if not state["ops"]:
        await emit(ErrorMessage(
            message_id=message_id, code=ErrorCode.AGENT_INVALID_OUTPUT,
            message="The agent returned no changes and no question.", detail=state["summary"]
        ))
        return {"done": True}
        
    await emit_status(state, AgentPhase.PATCHING, f"Applying {len(state['ops'])} change(s).")
    
    candidates, all_ledgers = stage_operations(session, state["ops"])
    if len(state["targets"]) == 1:
        target = state["targets"][0]
        ref = target.surface_reference
        if target.saved_reference is not None:
            ref = session.stores[target.saved_reference.ledger_id].ledger.surface_references[target.saved_reference.reference_id]
        if ref is not None:
            verify_selected_diameter(session.snapshot(), all_ledgers, ref, state["prompt"])
    
    await emit_status(state, AgentPhase.BUILDING, "Rebuilding geometry.")
    outcome = await build_design(all_ledgers, session.primary_ledger_id)
    return {"candidate": candidates, "built_ledgers": all_ledgers, "outcome": outcome}

async def review_node(state: AgentState) -> dict:
    outcome = state["outcome"]
    session = state["session"]
    message_id = state["message_id"]
    emit = state["emit"]
    candidates = state["candidate"]
    
    if outcome.ok:
        if state.get("preview"):
            await publish_preview(session, state["built_ledgers"], outcome, message_id, emit,
                summary=state["summary"], prompt=state["prompt"], assumptions=state["assumptions"],
                patch=[dict(op, ledger_id=lid) for lid, candidate in candidates.items() for op in candidate.ops])
            await emit_status(state, AgentPhase.DONE, "Preview ready. Apply to accept the changes.")
            return {"done": True}
        # Validate all model-authored response fields before accepting any state.
        response = AgentMessage(message_id=message_id, text=state["summary"], assumptions=state["assumptions"])
        await emit(response)
        await emit_status(state, AgentPhase.EXPORTING, "Sending the model.")
        all_patches = [dict(op, ledger_id=lid) for lid, candidate in candidates.items() for op in candidate.ops]
        await accept_design(session, state["built_ledgers"], outcome, message_id, emit,
                            state["emit_binary"], summary=state["summary"], patch=all_patches)
        await emit_status(state, AgentPhase.DONE, outcome.detail)
        return {"done": True}
        
    # Failed
    attempt = state["attempt"] + 1
    if attempt > 2 or outcome.code in (ErrorCode.GEOMETRY_KERNEL_FAILURE, ErrorCode.GEOMETRY_TIMEOUT, ErrorCode.BUSY):
        primary_store = session.stores[session.primary_ledger_id]
        await emit(GeometryFailed(
            message_id=message_id, current_revision=primary_store.ledger.revision,
            code=outcome.code, message=outcome.detail if outcome.code in (
                ErrorCode.GEOMETRY_TIMEOUT, ErrorCode.BUSY, ErrorCode.GEOMETRY_KERNEL_FAILURE
            ) else "Geometry conflict.",
            detail={"reason": outcome.detail} if isinstance(outcome.detail, str) else outcome.detail,
            feature_id=outcome.feature_id
        ))
        await emit_status(state, AgentPhase.DONE)
        return {"done": True, "attempt": attempt}
        
    return {"attempt": attempt, "done": False}

def route_after_architect(state: AgentState) -> str:
    if state["is_edit"]: return "machinist"
    if state["is_assembly"]: return "librarian"
    return "draftsman"

def route_after_librarian(state: AgentState) -> str:
    return "assembler"

def route_after_build(state: AgentState) -> str:
    if state.get("done"): return END
    return "review"

def route_after_review(state: AgentState) -> str:
    if state["done"]: return END
    if state["is_edit"]: return "machinist"
    if state["is_assembly"]: return "assembler"
    return "draftsman"

workflow = StateGraph(AgentState)
workflow.add_node("setup", setup_node)
workflow.add_node("architect", architect_node)
workflow.add_node("librarian", librarian_node)
workflow.add_node("draftsman", draftsman_node)
workflow.add_node("machinist", machinist_node)
workflow.add_node("assembler", assembler_node)
workflow.add_node("build", build_node)
workflow.add_node("review", review_node)

workflow.set_entry_point("setup")
workflow.add_edge("setup", "architect")
workflow.add_conditional_edges("architect", route_after_architect)
workflow.add_conditional_edges("librarian", route_after_librarian)

workflow.add_edge("draftsman", "build")
workflow.add_edge("machinist", "build")
workflow.add_edge("assembler", "build")

workflow.add_conditional_edges("build", route_after_build)
workflow.add_conditional_edges("review", route_after_review)

app = workflow.compile()
