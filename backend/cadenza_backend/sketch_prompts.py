"""Revision-bound sketch interpretation, clarification and explicit draft acceptance."""
import asyncio
import json
from dataclasses import dataclass
from time import monotonic
from uuid import uuid4

from cadenza_backend.agents.model_client import ModelClient, AIError, AINotConfigured, AIRefused
from cadenza_backend.agents.sketch_editor import CARD, SketchIntent
from cadenza_backend.edit_preview import changes_between, pending
from cadenza_backend.session import Session
from cadenza_backend.sketch_drafts import _current, _document, PendingDraft
from cadenza_backend.sketch_intents import compile_intent
from cadenza_contracts.errors import CadenzaError, ErrorCode
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.sketch_draft import (
    DraftPrompt, DraftProposalDecision, DraftProposal, DraftChange, DraftState, SketchDraft,
)

PROMPT_TIMEOUT_S = 60


@dataclass
class PendingSketchProposal:
    before: SketchDraft
    candidate: SketchDraft | None
    message: DraftProposal
    prompt: str
    created: float


def proposal_changes(draft: PendingDraft, candidate: SketchDraft) -> list[DraftChange]:
    before = Ledger.model_validate(_document(draft, draft.state)) if draft.state.definition.entities else draft.base[draft.state.ledger_id]
    after = Ledger.model_validate(_document(draft, candidate))
    changes = changes_between({before.project_id: before}, {after.project_id: after})
    result: list[DraftChange] = []
    def display(value): return "none" if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    def record(label, left, right):
        if left != right: result.append(DraftChange(label=label, before=display(left), after=display(right)))
    old_entities = draft.state.solution.entities if draft.state.solution else draft.state.definition.entities
    new_entities = candidate.solution.entities if candidate.solution else candidate.definition.entities
    old = {e.id: e for e in old_entities}
    new = {e.id: e for e in new_entities}
    names = {e.id: "Outer" if i == 0 else f"Opening {i}" for i, e in enumerate(new_entities)}
    for i, e in enumerate(old_entities): names.setdefault(e.id, "Outer" if i == 0 else f"Opening {i} (removed)")
    record("Sketch entity count", len(old), len(new))
    for identity in dict.fromkeys([*old, *new]):
        left, right = old.get(identity), new.get(identity)
        for field in ("kind", "x", "y", "width", "height", "diameter"):
            record(f"{names[identity]} {field}" + (" (mm)" if field != "kind" else ""),
                   getattr(left, field, None), getattr(right, field, None))
    old_constraints = {c.id: c for c in draft.state.definition.constraints}
    new_constraints = {c.id: c for c in candidate.definition.constraints}
    def constraint_name(c):
        return f"{names[c.entity_id]} {c.parameter}" if c.kind == "dimension" else f"{names[c.entity_a]} / {names[c.entity_b]} shared centre"
    for identity in old_constraints.keys() ^ new_constraints.keys():
        c = new_constraints.get(identity) or old_constraints[identity]
        record(f"Constraint: {constraint_name(c)}", "present" if identity in old_constraints else None,
               "present" if identity in new_constraints else None)
    for name in draft.state.relationships.parameters.keys() | candidate.relationships.parameters.keys():
        left, right = draft.state.relationships.parameters.get(name), candidate.relationships.parameters.get(name)
        record(f"Named parameter: {name}", left.model_dump(exclude_none=True) if left else None, right.model_dump(exclude_none=True) if right else None)
    old_bindings = {(b.feature_id, b.target, b.constraint_id): b.expression for b in draft.state.relationships.bindings}
    for binding in candidate.relationships.bindings:
        c = new_constraints.get(binding.constraint_id or "")
        record(f"Binding: {constraint_name(c) if c else binding.target}",
               old_bindings.get((binding.feature_id, binding.target, binding.constraint_id)), binding.expression)
    for change in changes:
        # Include dependent feature effects outside the sketch; sketch and driver details are above.
        parts = change.path.split("/")
        if len(parts) < 3 or parts[1] != "features" or parts[2] == candidate.profile_id: continue
        feature = next((f for f in after.features if f.id == parts[2]), None)
        label = feature.name or feature.kind if feature else "Dependent feature"
        record(f"{label}: {' '.join(parts[3:]).replace('_', ' ')}", change.before, change.after)
    if draft.state.extrusion_amount != candidate.extrusion_amount:
        result.append(DraftChange(label="New extrusion amount (mm)", before=str(draft.state.extrusion_amount), after=str(candidate.extrusion_amount)))
    return result


async def handle_sketch_prompt(session: Session, request: DraftPrompt | DraftProposalDecision, emit,
                               *, client: ModelClient | None = None) -> None:
    """Caller holds the session lock, including the bounded interpretation call."""
    draft = _current(session, request)
    if pending(session): raise CadenzaError(ErrorCode.BUSY, "Go back to the sketch before requesting changes")
    selected = request.selected_entity_id
    if selected is not None and not any(e.id == selected for e in draft.state.definition.entities):
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The selected sketch entity no longer exists")
    proposal = draft.proposal
    if proposal and (monotonic() - proposal.created > 900 or proposal.before != draft.state):
        draft.proposal = proposal = None
    if isinstance(request, DraftProposalDecision):
        if proposal is None or proposal.message.proposal_id != request.proposal_id or proposal.message.selected_entity_id != selected:
            raise CadenzaError(ErrorCode.REVISION_CONFLICT, "This sketch proposal expired or its target changed; request a new proposal")
        if request.action == "update":
            if proposal.candidate is None: raise CadenzaError(ErrorCode.BAD_MESSAGE, "Answer the clarification before updating the sketch")
            draft.state = proposal.candidate.model_copy(deep=True)
        draft.proposal = None
        await emit(DraftState(message_id=request.message_id, session_id=session.id, draft=draft.state))
        return

    prompt = request.prompt.strip()
    if not prompt: raise CadenzaError(ErrorCode.BAD_MESSAGE, "Describe the sketch change")
    if request.reply_to:
        if proposal is None or proposal.message.proposal_id != request.reply_to or not proposal.message.question or proposal.message.selected_entity_id != selected:
            raise CadenzaError(ErrorCode.REVISION_CONFLICT, "That clarification is obsolete; start a new request")
        prompt = f"{proposal.prompt}\nClarification: {proposal.message.question}\nAnswer: {prompt}"
        if len(prompt) > 8000: raise CadenzaError(ErrorCode.BAD_MESSAGE, "Start a new concise request including the clarified dimensions")
    draft.proposal = None
    before = draft.state.model_copy(deep=True)
    context = {"request": prompt, "selected_entity_id": selected, "units": "mm; sketch-local coordinates",
               "draft": before.model_dump(mode="json", exclude={"diagnostics", "extrusion_id"})}
    related = {b.feature_id for b in before.relationships.bindings}
    context["related_features"] = [f.model_dump(mode="json") for f in draft.base[before.ledger_id].features
        if f.id != before.profile_id and (f.id in related or (f.kind == "extrude" and f.parameters.target_feature == before.profile_id))]
    started = monotonic()
    try:
        raw = await asyncio.wait_for((client or ModelClient(session.logger)).complete_json(
            stage="machinist", agent_card=CARD, user=json.dumps(context), max_tokens=8000,
            effort="high"), timeout=PROMPT_TIMEOUT_S)
        # Recheck after awaiting: a disconnected/replaced session must not receive old work.
        if _current(session, request) is not draft or draft.state != before:
            raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The draft changed during interpretation; request a new proposal")
        intent = SketchIntent.model_validate(raw)
        candidate, interpretation = (None, []) if intent.question else compile_intent(draft, intent)
        message = DraftProposal(message_id=request.message_id, session_id=session.id,
            draft_id=before.id, draft_revision=before.revision, base_revision=before.base_revision,
            selected_entity_id=selected, proposal_id=uuid4().hex, summary=intent.summary,
            question=intent.question, assumptions=intent.assumptions, interpretation=interpretation,
            changes=proposal_changes(draft, candidate) if candidate else [],
            checks=[] if candidate is None else ["Interpreted dimensions, entity count and relationships checked.",
                "Fully constrained valid sketch region; solid build happens at Finish.",
                "Review the interpretation: these checks do not prove arbitrary-language intent."])
        draft.proposal = PendingSketchProposal(before, candidate, message, prompt, monotonic())
        session.logger.log("sketch_interpretation", duration_ms=(monotonic() - started) * 1000,
                           outcome="clarification" if intent.question else "proposal", action_count=len(intent.actions))
        await emit(message)
    except asyncio.TimeoutError as exc:
        raise CadenzaError(ErrorCode.AGENT_TIMEOUT, "Sketch interpretation timed out; the draft is unchanged") from exc
    except AINotConfigured as exc:
        raise CadenzaError(ErrorCode.AGENT_UNAVAILABLE, "No AI provider is configured; direct sketch controls remain available") from exc
    except AIRefused as exc:
        raise CadenzaError(ErrorCode.AGENT_REFUSED, "The provider declined this request; the draft is unchanged") from exc
    except AIError as exc:
        raise CadenzaError(ErrorCode.AGENT_INVALID_OUTPUT, "The provider could not return a sketch proposal; the draft is unchanged") from exc
    except (ValueError, KeyError) as exc:
        raise CadenzaError(ErrorCode.PATCH_INVALID, str(exc)) from exc
