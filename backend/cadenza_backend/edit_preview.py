"""Transient candidates shared by prompt and direct edits; only Apply commits."""
from dataclasses import dataclass, field
import math
import re
from time import monotonic
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Response

from cadenza_backend.contracts_bridge import CadenzaError, ErrorCode, Ledger, PatchOp
from cadenza_backend.design import accept_design, replay_geometry, stage_operations
from cadenza_backend.geometry_bridge import BuildOutcome
from cadenza_backend.geometry_jobs import build_design, inspect
from cadenza_backend.session import Session, registry
from cadenza_contracts.messages import NamedParameterEdit, ParameterEdit, SketchDimensionEdit, PreviewChange, PreviewClosed, PreviewDecision, PreviewReady
from cadenza_contracts.parameters import dimension_edit_path
from cadenza_contracts.patch import apply_patch
from cadenza_contracts.sketch_draft import DraftState
from cadenza_contracts.patterns import PATTERN_KINDS, pattern_reason

router = APIRouter()
PARAMETERS = {"box": {"length", "width", "height"}, "cylinder": {"diameter", "height"}, "hole": {"diameter", "depth"}, "extrude": {"amount"}}


@dataclass
class PendingPreview:
    id: str
    base: dict[str, Ledger]
    ledgers: dict[str, Ledger]
    outcome: BuildOutcome
    message: PreviewReady
    patch: list[dict]
    created: float = field(default_factory=monotonic)
    display_outcome: BuildOutcome | None = None


def pending(session: Session) -> PendingPreview | None:
    candidate = session.preview
    if candidate and (monotonic() - candidate.created > 900 or candidate.message.base_revision != session.revision):
        from cadenza_backend.diagnostics import event
        event("preview.expired", preview_id=candidate.id, originating_message_id=candidate.message.message_id)
        session.preview = None
    return session.preview


def changes_between(before: dict[str, Ledger], after: dict[str, Ledger]) -> list[PreviewChange]:
    changes = []
    def visit(lid, path, left, right):
        if left == right:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() | right.keys()):
                if path == "" and key == "revision":
                    continue
                visit(lid, f"{path}/{key}", left.get(key), right.get(key))
        elif isinstance(left, list) and isinstance(right, list) and path in ("/features", "/assembly/components", "/assembly/joints"):
            old, new = {f["id"]: f for f in left}, {f["id"]: f for f in right}
            for key in sorted(old.keys() | new.keys()):
                visit(lid, f"{path}/{key}", old.get(key), new.get(key))
            if [f["id"] for f in left if f["id"] in new] != [f["id"] for f in right if f["id"] in old]:
                changes.append(PreviewChange(ledger_id=lid, path=f"{path}/order",
                    before=list(old), after=list(new)))
        else:
            changes.append(PreviewChange(ledger_id=lid, path=path, before=left, after=right))
    for lid in sorted(before.keys() | after.keys()):
        visit(lid, "", before[lid].model_dump(mode="json") if lid in before else None,
              after[lid].model_dump(mode="json") if lid in after else None)
    return changes


async def verify_edit(before: dict[str, Ledger], after: dict[str, Ledger], prompt: str, *, verification_scope=None,
                      prompt_scope: Literal["source", "assembly"] = "source", warnings: list[str] | None = None) -> list[str]:
    checks = ["The complete design built successfully."]
    added_holes = []
    for lid, ledger in after.items():
        old = {f.id: f for f in before[lid].features} if lid in before else {}
        live = [f for f in ledger.features if not f.suppressed]
        if lid not in before or before[lid].features != ledger.features:
            from cadenza_contracts.gear_authoring import gear_reason
            source = ledger.model_dump(mode="json")
            gears = [f.id for f in live if f.kind == "gear" and gear_reason(f.model_dump(mode="json"), source) is None]
            if gears:
                effects = await inspect(ledger, {"query_type": "gear_effects", "feature_ids": gears}, after)
                if effects.get("error") or len(effects.get("gears", [])) != len(gears):
                    raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER, effects.get("error", "Gear effects could not be verified."))
                checks.append("Gear tooth count, radial surfaces, width and material effect verified; existing bores checked against the changed stock.")
                if warnings is not None:
                    warnings.extend(effects.get("warnings", []))
        if verification_scope is not None and verification_scope[0] == lid and (lid not in before or before[lid].features != ledger.features):
            from cadenza_contracts.revolve import revolve_reason
            revolves = [f.id for f in live if f.kind == "revolve" and revolve_reason(f.model_dump(mode="json")) is None]
            if revolves:
                effects = await inspect(ledger, {"query_type": "revolve_effects", "feature_ids": revolves}, after)
                if effects.get("error") or len(effects.get("revolves", [])) != len(revolves):
                    raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER, effects.get("error", "Revolve effects could not be verified."))
                checks.append("Supported revolve profiles and angles match measured tool volumes; every revolve has a material effect and the source is connected.")
        pattern_sources = []
        if lid not in before or before[lid].features != ledger.features:
            pattern_sources.append(ledger)
        if ledger.assembly is not None:
            previous_assembly = before[lid].assembly if lid in before else None
            previous_parts = {part.id: part for part in previous_assembly.parts} if previous_assembly else {}
            for part in ledger.assembly.parts:
                previous = previous_parts.get(part.id)
                if (previous is None or previous.features != part.features) and any(f.kind in PATTERN_KINDS for f in part.features):
                    # Inspect embedded definitions in source coordinates, without
                    # adding a ledger to the session or rebuilding the assembly.
                    pattern_sources.append(Ledger(project_id=f"{lid}:{part.id}", features=part.features,
                        metadata=ledger.metadata.model_copy(update={"name": f"{ledger.metadata.name} / {part.name}"})))
        for pattern_ledger in pattern_sources:
            source = pattern_ledger.model_dump(mode="json")
            pattern_ids = []
            for feature in source["features"]:
                if feature["kind"] not in PATTERN_KINDS or feature["suppressed"]:
                    continue
                reason = pattern_reason(feature, source)
                if reason:
                    checks.append(f"Legacy pattern {feature['id']}: effects not verified ({reason})")
                else:
                    pattern_ids.append(feature["id"])
            if pattern_ids:
                effects = await inspect(pattern_ledger, {"query_type": "pattern_effects", "feature_ids": pattern_ids},
                    {**after, pattern_ledger.project_id: pattern_ledger})
                if effects.get("error") or len(effects.get("patterns", [])) != len(pattern_ids):
                    raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER,
                        effects.get("error", "Pattern effects could not be verified."))
                for effect in effects["patterns"]:
                    pattern = next(f for f in source["features"] if f["id"] == effect["feature_id"])
                    seed = next(f for f in source["features"] if f["id"] == pattern["parameters"]["target_feature"])
                    detail = f"; {effect['measured_complete_bores']} complete bores measured" if effect["measured_complete_bores"] is not None else ""
                    checks.append(f"{pattern_ledger.metadata.name} / {pattern['name'] or pattern['id']}, seed {seed['name'] or seed['id']}: "
                        f"{effect['requested_total']} total instances; every added copy has material effect{detail}.")
        # In a single profile/additive-extrusion part, each enclosed circular
        # opening becomes a bore. More complex downstream booleans need separate
        # topology verification and are intentionally not inferred here.
        if len(live) == 2 and live[0].kind == "profile" and live[1].kind == "extrude" and live[1].operation == "add" and live[1].parameters.target_feature == live[0].id:
            previous = old.get(live[0].id)
            previous_entities = {e.id for e in previous.parameters.entities} if previous is not None and previous.kind == "profile" else set()
            added_holes.extend(e for e in live[0].parameters.entities if e.operation == "subtract" and e.id not in previous_entities)
        for feature in ledger.features:
            if feature.kind != "hole" or feature.suppressed:
                continue
            if feature.id not in old:
                added_holes.append(feature)
            if old.get(feature.id) == feature and (not ledger.relationships.bindings or before[lid].features == ledger.features):
                continue
            result = await inspect(ledger, {"query_type": "resolve_surface_reference", "reference": {
                "version": 1, "ledger_id": lid, "source_ledger_id": lid, "source_part_id": None,
                "component_path": [], "feature_id": feature.id, "feature_kind": "hole",
                "operation": "subtract", "role": "bore"}}, after)
            if result.get("status") != "resolved":
                raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER,
                    f"Hole {feature.name or feature.id} does not produce a distinct supported bore. "
                    "Check its placement, overlap and dimensions.")
            checks.append(f"Distinct bore verified: {feature.name or feature.id}.")
    # Only explicit supported wording is machine-checked. The report below
    # never claims complete interpretation of arbitrary prose or drawings.
    source_prompt = prompt if prompt_scope == "source" else ""
    count = re.search(r"\badd\s+(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+(?:(?:mounting|through|new)\s+)*holes?\b", source_prompt, re.I)
    if count:
        words: dict[str, int] = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
        token = count[1].lower()
        expected = int(token) if token.isdigit() else words[token]
        if len(added_holes) != expected:
            raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER,
                f"Requested {expected} added holes; candidate has {len(added_holes)} supported new holes.")
        checks.append(f"Requested added-hole count verified: {expected}.")
    dimension = re.fullmatch(r"\s*(?:make (?:this(?: plate)?|the plate|it) ([0-9]+(?:\.[0-9]+)?)\s*mm thick|set (?:the )?(height|thickness|width|length) to ([0-9]+(?:\.[0-9]+)?)\s*mm)[.!]?\s*", source_prompt, re.I)
    if dimension and not (verification_scope is not None and verification_scope[0] not in before):
        parameter = "height" if dimension[1] or dimension[2].lower() == "thickness" else dimension[2].lower()
        expected_value = float(dimension[1] or dimension[3])
        scoped = [f for lid, ledger in after.items() for f in ledger.features
                  if verification_scope is None or (lid == verification_scope[0]
                      and (verification_scope[1] is None or f.id == verification_scope[1]))]
        boxes = [f for f in scoped if f.kind == "box" and not f.suppressed]
        constraint_id = None
        edit_parameter = parameter
        if verification_scope is not None and verification_scope[1] is not None:
            if len(scoped) != 1 or scoped[0].suppressed:
                raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER, "The selected feature is not available for this dimension check.")
            selected = scoped[0]
            edit_parameter = "amount" if selected.kind == "extrude" and parameter == "height" else parameter
            if edit_parameter not in PARAMETERS.get(selected.kind, set()) and not (selected.kind == "gear" and edit_parameter == "height"):
                raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER, "This dimension does not belong to the selected feature.")
            feature_id, actual_value = selected.id, getattr(selected.parameters, edit_parameter)
        elif len(boxes) == 1:
            feature_id, actual_value = boxes[0].id, getattr(boxes[0].parameters, parameter)
        elif not boxes:
            profiles = [f for f in scoped if f.kind == "profile" and not f.suppressed]
            if len(profiles) != 1 or profiles[0].parameters.solution is None:
                raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER, "Choose a single plate or sketch before this dimension edit.")
            profile = profiles[0]
            if dimension[1] or dimension[2].lower() == "thickness":
                extrusions = [f for ledger in after.values() for f in ledger.features
                    if f.kind == "extrude" and not f.suppressed and f.parameters.target_feature == profile.id]
                if len(extrusions) != 1:
                    raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER, "Choose one sketch extrusion to change thickness.")
                feature_id, actual_value, edit_parameter = extrusions[0].id, extrusions[0].parameters.amount, "amount"
            else:
                assert profile.parameters.solution is not None
                outer = profile.parameters.solution.entities[0]
                dimensions = [c for c in profile.parameters.constraints
                    if c.kind == "dimension" and c.entity_id == outer.id and c.parameter == parameter]
                if len(dimensions) != 1:
                    raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER, "Choose one sketch driving dimension for this edit.")
                feature_id, actual_value = profile.id, getattr(outer, parameter)
                edit_parameter, constraint_id = "value", dimensions[0].id
        else:
            raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER, "Choose a single plate before this dimension edit.")
        if not math.isclose(actual_value, expected_value, abs_tol=1e-7):
            raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER, f"Requested {parameter} {expected_value} mm was not produced.")
        expected_documents = {lid: ledger.model_copy(deep=True) for lid, ledger in before.items()}
        matching = [(lid, feature) for lid, ledger in expected_documents.items() for feature in ledger.features
                    if feature.id == feature_id and (verification_scope is None or lid == verification_scope[0])]
        if len(matching) != 1:
            raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER, "Could not identify the original plate for this edit.")
        lid, original = matching[0]
        try:
            path = dimension_edit_path(expected_documents[lid], original.id, edit_parameter, constraint_id)
            expected_documents[lid] = apply_patch(expected_documents[lid], [PatchOp(op="replace", path=path, value=expected_value)])
        except ValueError as exc:
            raise CadenzaError(ErrorCode.PATCH_INVALID, str(exc)) from exc
        if changes_between(expected_documents, after):
            raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER,
                "This dimension request also changed unrelated design data. Please propose only the requested dimension.")
        checks.append(f"Requested {parameter} verified in the feature definition: {expected_value} mm.")
    if prompt:
        checks.append("Review the actual changes and assumptions; other language or drawing requirements are not independently verified.")
    return checks


async def publish_preview(session, ledgers, outcome, message_id, emit, *, summary="", patch=None, prompt="", assumptions=None,
                          draft_id=None, draft_revision=None, verification_scope=None, display_ledger_id=None,
                          prompt_scope: Literal["source", "assembly"] = "source", context_path=None,
                          independence=None, extra_checks=None):
    if not outcome.ok or outcome.stub:
        raise CadenzaError(ErrorCode.GEOMETRY_KERNEL_FAILURE, "A real successful geometry build is required for preview.")
    base = session.snapshot()
    changes = changes_between(base, ledgers)
    if not changes:
        raise CadenzaError(ErrorCode.PATCH_INVALID, "The candidate contains no changes.")
    warnings = list(outcome.warnings or [])
    checks = await verify_edit(base, ledgers, prompt, verification_scope=verification_scope, prompt_scope=prompt_scope, warnings=warnings)
    checks.extend(extra_checks or [])
    from cadenza_contracts.messages import AttachmentContact
    from cadenza_backend.assembly_commands import source_uses
    attachment_contacts = []
    for lid, ledger in ledgers.items():
        planar = [j for j in ledger.assembly.joints if j.target_a.mode == "planar_face"] if ledger.assembly else []
        if not planar:
            continue
        evidence = await inspect(ledger, {"query_type": "attachment_contacts"}, ledgers)
        contacts = [AttachmentContact.model_validate(item) for item in evidence.get("contacts", []) if item.get("definition_id") == lid]
        if evidence.get("interference_checked") is not True or {c.joint_id for c in contacts} != {j.id for j in planar}:
            raise CadenzaError(ErrorCode.GEOMETRY_INVALID_PARAMETER, "Attachment contact verification is incomplete.")
        attachment_contacts.extend(contacts)
        names = {c.id: c.name or c.id for c in ledger.assembly.components}
        for joint in planar:
            check = next(c for c in contacts if c.joint_id == joint.id)
            checks.append(f"Face contact verified in {ledger.metadata.name}: {names[joint.component_a]} -> {names[joint.component_b]}; {check.area_mm2:.3f} mm? contact area. Interference checked.")

    for lid, ledger in ledgers.items():
        if (lid in base
                and ledger.model_dump(exclude={"revision"}) != base[lid].model_dump(exclude={"revision"})):
            try:
                uses = source_uses(ledgers, session.primary_ledger_id, lid)
                if uses:
                    checks.append(f"Shared source {ledger.metadata.name}: affects {len(uses)} instance(s): " + "; ".join(uses))
            except ValueError:
                checks.append(f"Shared source {ledger.metadata.name}: the complete design was rebuilt; occurrence listing exceeds supported authoring limits or contains unresolved definitions.")
            if ledger.assembly is None and any(l.assembly and any(j.target_a.mode == "coordinate" for j in l.assembly.joints) for l in ledgers.values()):
                checks.append("Coordinate attachments stay at their recorded anchors after source edits. A successful build does not prove face contact; review gaps and alignment.")
        if lid not in base and ledger.assembly is not None:
            checks.append(f"New shared subassembly {ledger.metadata.name}: {len(ledger.assembly.components)} child instances; created atomically on Apply.")
        previous = base.get(lid)
        if previous and previous.assembly and ledger.assembly:
            retained = {j.id for j in ledger.assembly.joints}
            if any(j.id not in retained for j in previous.assembly.joints):
                checks.append(f"{ledger.metadata.name}: detached instances return to stored placement; retained downstream attachments solve again. Review the resulting pose.")
            old_ids = {j.id for j in previous.assembly.joints}
            names = {c.id: c.name or c.id for c in [*previous.assembly.components, *ledger.assembly.components]}
            for action, joints in (("Detach", [j for j in previous.assembly.joints if j.id not in retained]),
                    ("Attach", [j for j in ledger.assembly.joints if j.id not in old_ids])):
                for joint in joints:
                    if joint.target_a.mode == "planar_face":
                        checks.append(f"{action} face attachment: {names[joint.component_a]} -> {names[joint.component_b]} in {ledger.metadata.name}.")
                        continue
                    checks.append(f"{action} {names[joint.component_a]} -> {names[joint.component_b]} in {ledger.metadata.name}: "
                        f"A point {joint.target_a.point}, normal {joint.target_a.normal}; B point {joint.target_b.point}, normal {joint.target_b.normal}. "
                        "Points are stored owner coordinates in mm, before joint solving.")
    for ledger in ledgers.values():
        for feature in ledger.features:
            if feature.kind == "profile" and not feature.suppressed and feature.parameters.solution is not None:
                checks.append(f"Sketch {feature.name or feature.id}: {feature.parameters.solution.status}; "
                    f"{feature.parameters.solution.degrees_of_freedom} remaining degrees of freedom.")
        relationships = ledger.relationships
        if relationships.parameters:
            checks.append(f"Resolved {len(relationships.parameters)} named parameters and {len(relationships.bindings)} bindings; "
                f"{len(relationships.constraints)} relationship constraints passed in {ledger.metadata.name}.")
    display_outcome = None
    if display_ledger_id == session.primary_ledger_id:
        display_ledger_id = None  # Existing primary edits retain project-view behavior.
    if display_ledger_id is not None:
        source = ledgers.get(display_ledger_id)
        if source is None or source.assembly is not None:
            raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "Choose a source part for display.")
        display_outcome = await build_design(ledgers, display_ledger_id)
        if not display_outcome.ok or display_outcome.stub:
            raise CadenzaError(display_outcome.code or ErrorCode.GEOMETRY_KERNEL_FAILURE, display_outcome.detail)
    matrix = context_warning = None
    view = session.source_view
    if context_path is None and view and view.ledger_id == display_ledger_id and view.revision == session.revision:
        context_path = view.component_path
    if display_ledger_id and context_path:
        try:
            context = await inspect(ledgers[session.primary_ledger_id], dict(query_type="instance_context",
                component_path=context_path, source_ledger_id=display_ledger_id), ledgers)
            matrix = context["source_to_project"]
        except CadenzaError:
            context_warning = "Part context could not be resolved. Review the whole candidate assembly instead."
            context_path = None
    preview_id = uuid4().hex
    url = f"/api/sessions/{session.id}/previews/{preview_id}"
    message = PreviewReady(message_id=message_id, preview_id=preview_id, base_revision=session.revision,
        summary=summary, prompt=prompt, changes=changes, checks=checks, warnings=list(dict.fromkeys(warnings)), attachment_contacts=attachment_contacts, independence=independence, assumptions=assumptions or [],
        glb_url=f"{url}/model.glb" if outcome.glb else None,
        step_url=f"{url}/model.step" if outcome.step else None, empty=outcome.empty,
        draft_id=draft_id, draft_revision=draft_revision, display_ledger_id=display_ledger_id,
        source_glb_url=f"{url}/source.glb" if display_outcome and display_outcome.glb else None,
        source_step_url=f"{url}/source.step" if display_outcome and display_outcome.step else None,
        source_empty=display_outcome.empty if display_outcome else False,
        context_component_path=context_path, source_to_project=matrix, context_warning=context_warning)
    session.preview = PendingPreview(preview_id, base, ledgers, outcome, message, patch or [], display_outcome=display_outcome)
    from cadenza_backend.diagnostics import event, snapshot
    event("preview.published", preview_id=preview_id, originating_message_id=message_id,
        display_ledger_id=display_ledger_id, context_component_path=context_path,
        candidate=snapshot(ledgers), changes=[c.model_dump(mode="json") for c in changes],
        checks=checks, warnings=message.warnings, attachment_contacts=[c.model_dump(mode="json") for c in attachment_contacts], independence=independence, volume_mm3=outcome.volume_mm3, bbox=outcome.bbox)
    await emit(message)


async def preview_parameter(session: Session, request: ParameterEdit | NamedParameterEdit | SketchDimensionEdit, emit) -> None:
    ledger = session.stores.get(request.ledger_id)
    if request.base_revision != session.revision:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The design changed. Refresh before editing.")
    if ledger is None:
        raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "Unknown parameter ledger.")
    if isinstance(request, NamedParameterEdit):
        parameter = ledger.ledger.relationships.parameters.get(request.parameter)
        if parameter is None or parameter.expression is not None:
            raise CadenzaError(ErrorCode.PATCH_INVALID, "Select a named driving value, not a derived expression.")
        path = f"/relationships/parameters/{request.parameter}/value"
        label, unit = request.parameter, parameter.unit
    elif isinstance(request, SketchDimensionEdit):
        feature = next((f for f in ledger.ledger.features if f.id == request.feature_id), None)
        if feature is None or feature.kind != "profile" or feature.suppressed:
            raise CadenzaError(ErrorCode.PATCH_INVALID, "Select an active sketch profile.")
        constraint = next((c for c in feature.parameters.constraints if c.id == request.constraint_id), None)
        if constraint is None or constraint.kind != "dimension":
            raise CadenzaError(ErrorCode.PATCH_INVALID, "Select a sketch driving dimension.")
        try:
            path = dimension_edit_path(ledger.ledger, feature.id, "value", constraint.id)
        except ValueError as exc:
            raise CadenzaError(ErrorCode.PATCH_INVALID, str(exc)) from exc
        label, unit = f"{feature.name or 'Sketch'} {constraint.entity_id}.{constraint.parameter}", "mm"
        
    else:
        # Legacy design.parameter adapter to shared compiler
        from cadenza_contracts.design_commands import DesignCommand, SetDimension
        from cadenza_backend.design_commands import handle_design_command
        
        cmd = DesignCommand(
            message_id=request.message_id,
            base_revision=request.base_revision,
            ledger_id=request.ledger_id,
            operations=[SetDimension(feature_id=request.feature_id, parameter=request.parameter, value=request.value)]
        )
        await handle_design_command(session, cmd, emit)
        return

    op = PatchOp(op="replace", ledger_id=request.ledger_id, path=path, value=request.value)
    _, ledgers = stage_operations(session, [op])
    outcome = await build_design(ledgers, session.primary_ledger_id)
    if not outcome.ok:
        raise CadenzaError(outcome.code or ErrorCode.GEOMETRY_KERNEL_FAILURE, outcome.detail, interference=outcome.interference)
    await publish_preview(session, ledgers, outcome, request.message_id, emit,
        summary=f"Set {label} to {request.value:g} {unit}.", display_ledger_id=request.ledger_id,
        patch=[op.model_dump(mode="json", by_alias=True)])



async def decide_preview(session: Session, request: PreviewDecision, emit, emit_binary=None) -> None:
    from cadenza_backend.diagnostics import event
    event("preview.decision", preview_id=request.preview_id, action=request.action,
        originating_message_id=session.preview.message.message_id if session.preview else None)
    candidate = pending(session)
    if candidate is None or candidate.id != request.preview_id:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "This preview expired or was replaced. Generate a new preview.")
    if request.base_revision != session.revision or candidate.base != session.snapshot():
        session.preview = None
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The design changed. Generate a new preview.")
    if candidate.message.draft_id is not None and (session.draft is None
        or session.draft.state.id != candidate.message.draft_id
        or session.draft.state.revision != candidate.message.draft_revision):
        session.preview = None
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The sketch changed. Finish it again before applying.")
    if request.action == "apply":
        await accept_design(session, candidate.ledgers, candidate.outcome, request.message_id, emit, emit_binary,
            summary=candidate.message.summary, patch=candidate.patch)
    else:
        session.preview = None
    await emit(PreviewClosed(message_id=request.message_id, preview_id=request.preview_id))
    if request.action == "cancel":
        await replay_geometry(session, emit, emit_binary)
        if session.draft:
            await emit(DraftState(message_id=request.message_id, session_id=session.id, draft=session.draft.state))


@router.get("/api/sessions/{session_id}/previews/{preview_id}/{filename}")
async def preview_artifact(session_id: str, preview_id: str, filename: str):
    session = registry.get(session_id)
    candidate = pending(session) if session else None
    if candidate is None or candidate.id != preview_id:
        raise HTTPException(404, "Preview is no longer available.")
    outcome = candidate.display_outcome if filename in ("source.glb", "source.step") else candidate.outcome
    data = (outcome.glb if filename in ("model.glb", "source.glb") else outcome.step
            if filename in ("model.step", "source.step") else None) if outcome else None
    if data is None:
        raise HTTPException(404, "Preview artifact is unavailable.")
    return Response(data, media_type="model/gltf-binary" if filename.endswith(".glb") else "application/step",
                    headers={"Cache-Control": "no-store"})
