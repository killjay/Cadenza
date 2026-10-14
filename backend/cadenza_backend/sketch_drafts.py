"""Session-owned unfinished sketches; only a built preview can be accepted."""
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from cadenza_contracts.errors import CadenzaError, ErrorCode
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.parameters import resolve_parameters, dimension_edit_path, Relationships, Parameter, Binding
from cadenza_contracts.profiles import ProfileEntity, DimensionConstraint, CentreConstraint, validate_profile_region
from cadenza_contracts.sketch_draft import (
    DraftStart, DraftUpdate, DraftAction, DraftState, DraftClosed, DraftDefinition, SketchDraft,
    EntityAlias, ConstraintAlias, DraftPrompt, DraftProposalDecision,
)
from cadenza_backend.session import Session
from cadenza_backend.edit_preview import pending, publish_preview
from cadenza_backend.design import replay_geometry
from cadenza_backend.geometry_jobs import build_design


if TYPE_CHECKING:
    from cadenza_backend.sketch_prompts import PendingSketchProposal


@dataclass
class PendingDraft:
    base: dict[str, Ledger]
    state: SketchDraft
    proposal: "PendingSketchProposal | None" = None


def _document(draft: PendingDraft, state: SketchDraft) -> dict:
    doc = draft.base[state.ledger_id].model_dump(mode="json")
    doc["relationships"] = state.relationships.model_dump(mode="json")
    if state.is_new:
        doc["features"].append({"id": state.profile_id, "name": "Sketch", "kind": "profile",
            "parameters": state.definition.model_dump(mode="json")})
    else:
        feature = next(f for f in doc["features"] if f["id"] == state.profile_id)
        feature["parameters"] = state.definition.model_dump(mode="json")
    return doc


def _solve(draft: PendingDraft, state: SketchDraft) -> None:
    state.definition = DraftDefinition.model_validate(state.definition.model_dump())
    state.relationships = Relationships.model_validate(state.relationships.model_dump())
    state.diagnostics = []
    if not state.definition.entities:
        resolve_parameters(state.relationships)
        state.solution = None
        state.diagnostics = ["Add an outer rectangle or circle to start the sketch."]
        return
    ledger = Ledger.model_validate(_document(draft, state))
    feature = next(f for f in ledger.features if f.id == state.profile_id)
    assert feature.kind == "profile" and feature.parameters.solution is not None
    state.definition = DraftDefinition(entities=feature.parameters.entities, constraints=feature.parameters.constraints)
    state.solution = feature.parameters.solution
    try:
        validate_profile_region(state.solution)
    except ValueError as exc:
        state.diagnostics = [str(exc)]


def _current(session: Session, request: DraftUpdate | DraftAction | DraftPrompt | DraftProposalDecision) -> PendingDraft:
    draft = session.draft
    if (draft is None or draft.state.id != request.draft_id or draft.state.revision != request.draft_revision
        or request.base_revision != session.revision or draft.base != session.snapshot()):
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "This sketch draft changed or was discarded. Refresh before editing.")
    return draft


def _free_entities(state: SketchDraft, entity_id: str, parameter: str) -> set[str]:
    entity = next((e for e in state.definition.entities if e.id == entity_id), None)
    if entity is None or parameter not in (("x", "y", "width", "height") if entity.kind == "rectangle" else ("x", "y", "diameter")):
        raise ValueError("Select an existing sketch variable")
    related = {entity_id}
    if parameter in ("x", "y"):
        while True:
            before = len(related)
            for c in state.definition.constraints:
                if c.kind == "coincident" and related.intersection((c.entity_a, c.entity_b)):
                    related.update((c.entity_a, c.entity_b))
            if len(related) == before: break
    if any(c.kind == "dimension" and c.entity_id in related and c.parameter == parameter for c in state.definition.constraints):
        raise ValueError("This variable is constrained. Edit its dimension or named driver.")
    return related


def update_candidate(draft: PendingDraft, request: DraftUpdate) -> SketchDraft:
    state = draft.state.model_copy(deep=True)
    aliases: dict[str, tuple[str, str]] = {}

    def reference(value: str | EntityAlias | ConstraintAlias) -> str:
        if isinstance(value, str): return value
        name = value.entity_alias if isinstance(value, EntityAlias) else value.constraint_alias
        kind = "entity" if isinstance(value, EntityAlias) else "constraint"
        resolved = aliases.get(name)
        if resolved is None or resolved[0] != kind:
            raise ValueError(f"Unknown or wrong-kind {kind} alias: {name}; reference an earlier creation in this batch")
        return resolved[1]

    def register(alias: str | None, kind: str, identity: str) -> None:
        if alias is None: return
        if alias in aliases: raise ValueError(f"Duplicate creation alias: {alias}")
        aliases[alias] = (kind, identity)

    for op in request.operations:
        entities, constraints = state.definition.entities, state.definition.constraints
        if op.op == "add_entity":
            identity = f"ent_{uuid4().hex}"
            register(op.alias, "entity", identity)
            entities.append(ProfileEntity(id=identity, **op.entity.model_dump()))
        elif op.op == "remove_entity":
            entity_id = reference(op.entity_id)
            entity = next((e for e in entities if e.id == entity_id), None)
            if entity is None: raise ValueError("Unknown sketch entity")
            if entity is entities[0] and len(entities) > 1:
                raise ValueError("Remove the openings before removing the outer entity")
            if any((c.entity_id == entity_id if c.kind == "dimension" else entity_id in (c.entity_a, c.entity_b)) for c in constraints):
                raise ValueError("Remove referencing constraints before removing this entity")
            entities.remove(entity)
        elif op.op == "set_seed":
            entity_id = reference(op.entity_id)
            related = _free_entities(state, entity_id, op.parameter)
            state.definition.entities = [ProfileEntity.model_validate({**e.model_dump(), op.parameter: op.value})
                if e.id in related else e for e in entities]
        elif op.op == "add_dimension":
            identity = f"con_{uuid4().hex}"
            register(op.alias, "constraint", identity)
            constraints.append(DimensionConstraint(id=identity, entity_id=reference(op.entity_id),
                parameter=op.parameter, value=op.value))
        elif op.op == "add_centre":
            constraints.append(CentreConstraint(id=f"con_{uuid4().hex}", entity_a=reference(op.entity_a), entity_b=reference(op.entity_b)))
        elif op.op == "remove_constraint":
            constraint_id = reference(op.constraint_id)
            constraint = next((c for c in constraints if c.id == constraint_id), None)
            if constraint is None: raise ValueError("Unknown sketch constraint")
            binding = next((b for b in state.relationships.bindings if b.feature_id == state.profile_id and b.constraint_id == constraint_id), None)
            if binding: raise ValueError(f"This dimension is bound to {binding.expression}; edit its named driver. Bound removal is not supported.")
            constraints.remove(constraint)
        elif op.op == "set_dimension":
            constraint_id = reference(op.constraint_id)
            constraint = next((c for c in constraints if c.id == constraint_id and c.kind == "dimension"), None)
            if constraint is None or constraint.kind != "dimension": raise ValueError("Select a dimension constraint")
            ledger = Ledger.model_validate(_document(draft, state))
            path = dimension_edit_path(ledger, state.profile_id, "value", constraint.id)
            if path.startswith("/relationships/"):
                state.relationships.parameters[path.split("/")[3]].value = op.value
            else:
                constraint.value = op.value
        elif op.op == "add_driver":
            if op.parameter in state.relationships.parameters:
                raise ValueError(f"Named driver already exists: {op.parameter}")
            state.relationships.parameters[op.parameter] = Parameter(unit=op.unit, value=op.value)
        elif op.op == "bind_dimension":
            constraint_id = reference(op.constraint_id)
            if not any(c.id == constraint_id and c.kind == "dimension" for c in constraints):
                raise ValueError("Select a dimension belonging to this sketch")
            if any(b.feature_id == state.profile_id and b.constraint_id == constraint_id for b in state.relationships.bindings):
                raise ValueError("This dimension already has a binding; edit its driver instead")
            state.relationships.bindings.append(Binding(feature_id=state.profile_id, target="value",
                constraint_id=constraint_id, expression=op.expression))
        elif op.op == "set_driver":
            driver = state.relationships.parameters.get(op.parameter)
            if driver is None or driver.expression is not None:
                raise ValueError("Select a literal named driver, not a derived expression")
            driver.value = op.value
        elif op.op == "set_extrusion":
            if not state.is_new: raise ValueError("Edit the existing extrusion after finishing the sketch")
            state.extrusion_amount = op.amount
        _solve(draft, state)
    state.revision += 1
    return state


async def handle_draft(session: Session, request: DraftStart | DraftUpdate | DraftAction, emit, emit_binary=None) -> None:
    """Caller holds the session lock; publish complete draft states atomically."""
    if request.base_revision != session.revision:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The accepted design changed. Start a new sketch draft.")
    try:
        if isinstance(request, DraftStart):
            if session.draft or pending(session):
                raise CadenzaError(ErrorCode.BUSY, "Finish or discard the current draft/preview first.")
            ledger = session.store.ledger
            if len(session.stores) != 1 or ledger.assembly is not None:
                raise ValueError("Sketch drafts currently support a standalone primary part only")
            feature = next((f for f in ledger.features if f.id == request.profile_id), None)
            if request.profile_id is not None and (feature is None or feature.kind != "profile" or feature.suppressed):
                raise ValueError("Select an active profile to edit as a draft")
            if request.profile_id is None and ledger.features:
                raise ValueError("Start a new sketch in an empty part, or edit an existing profile")
            state = SketchDraft(id=uuid4().hex, base_revision=session.revision, revision=0,
                ledger_id=ledger.project_id, profile_id=feature.id if feature else f"feat_{uuid4().hex}",
                is_new=feature is None, definition=DraftDefinition.model_validate(feature.parameters.model_dump(exclude={"solution"}))
                    if feature is not None and feature.kind == "profile" else DraftDefinition(),
                relationships=ledger.relationships.model_copy(deep=True),
                extrusion_id=f"feat_{uuid4().hex}" if feature is None else None)
            draft = PendingDraft(session.snapshot(), state)
            _solve(draft, state)
            session.draft = draft
        else:
            draft = _current(session, request)
            if isinstance(request, DraftAction) and request.action == "discard":
                session.preview = None
                session.draft = None
                await emit(DraftClosed(message_id=request.message_id, session_id=session.id, draft_id=draft.state.id))
                await replay_geometry(session, emit, emit_binary)
                return
            if pending(session): raise CadenzaError(ErrorCode.BUSY, "Apply the review or go back to the sketch before editing.")
            if isinstance(request, DraftUpdate):
                draft.proposal = None
                draft.state = update_candidate(draft, request)
            else:
                draft.proposal = None
                state = draft.state
                if state.solution is None: raise ValueError("The draft is empty")
                validate_profile_region(state.solution)
                doc = _document(draft, state)
                if state.is_new:
                    if state.extrusion_amount is None: raise ValueError("Enter an extrusion amount before Finish")
                    doc["features"].append({"id": state.extrusion_id, "kind": "extrude", "name": "Extrusion",
                        "parameters": {"target_feature": state.profile_id, "amount": state.extrusion_amount}})
                ledgers = session.stage({state.ledger_id: Ledger.model_validate(doc)})
                outcome = await build_design(ledgers, session.primary_ledger_id)
                if not outcome.ok: raise CadenzaError(outcome.code or ErrorCode.GEOMETRY_KERNEL_FAILURE, outcome.detail)
                await publish_preview(session, ledgers, outcome, request.message_id, emit, summary="Review sketch changes.",
                    draft_id=state.id, draft_revision=state.revision)
                return
        assert session.draft is not None
        await emit(DraftState(message_id=request.message_id, session_id=session.id, draft=session.draft.state))
    except ValueError as exc:
        raise CadenzaError(ErrorCode.PATCH_INVALID, str(exc)) from exc
