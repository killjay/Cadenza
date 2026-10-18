"""DesignCommand compiler mapping semantic operations to PatchOp."""
from typing import Any, Callable

from cadenza_backend.contracts_bridge import CadenzaError, ErrorCode, PatchOp, feature_by_id, new_feature_id
from cadenza_backend.design import stage_operations
from cadenza_backend.ledger_store import apply_patch
from cadenza_backend.geometry_jobs import build_design
from cadenza_backend.edit_preview import publish_preview
from cadenza_contracts.design_commands import AddFeatureBase, DesignCommand, RemoveFeature, SetDimension, SetOrigin, SetSuppressed
from cadenza_contracts.feature_capabilities import is_addable, is_origin_editable, is_supported_edit
from cadenza_contracts.parameters import dimension_edit_path
from cadenza_contracts.design_commands import AddLinearPattern, AddCircularPattern, UpdateLinearPattern, UpdateCircularPattern
from cadenza_contracts.patterns import DIRECTIONS, actual_radius, pattern_reason, seed_reason


def compile_command(session: Any, request: DesignCommand) -> list[PatchOp]:
    """Pure compiler producing ordered patch operations from a semantic command."""
    return compile_source_command(session.stores[request.ledger_id].ledger, request)


def compile_source_command(ledger, request: DesignCommand) -> list[PatchOp]:
    """Compile against an accepted or private source without mutating its owner."""
    patches: list[PatchOp] = []
    
    added_features: set[str] = set()
    modified_targets: set[str] = set()
    removed_feature_ids: set[str] = {op.feature_id for op in request.operations if isinstance(op, RemoveFeature)}
    
    for op in request.operations:
        if isinstance(op, (AddLinearPattern, AddCircularPattern)):
            if ledger.assembly is not None:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Patterns belong to a source part, not an assembly.")
            seed = feature_by_id(ledger, op.target_feature)
            if seed is None:
                raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "Select an existing pattern seed.")
            seed_data = seed.model_dump(mode="json")
            reason = seed_reason(seed_data)
            if reason:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, reason)
            fid = new_feature_id()
            added_features.add(fid)
            value: dict[str, Any] = dict(id=fid, name=op.name, kind="linear_pattern" if isinstance(op, AddLinearPattern) else "circular_pattern",
                         operation=seed.operation, parameters=dict(target_feature=seed.id, count=op.count),
                         placement=dict(origin=[0, 0, 0], rotation_deg=[0, 0, 0], relative_to=None))
            if isinstance(op, AddLinearPattern):
                value["parameters"].update(spacing=op.spacing, axis=DIRECTIONS[op.direction])
            else:
                value["placement"]["origin"] = [*op.center_xy, 0]
                value["parameters"].update(radius=actual_radius(value, seed_data), sweep_angle=360, axis=[0, 0, 1])
            patches.append(PatchOp(op="add", ledger_id=request.ledger_id, path="/features/-", value=value))
            continue
        if isinstance(op, AddFeatureBase):
            if getattr(ledger, "assembly", None) is not None and op.kind in ("box", "cylinder", "hole", "extrude"):
                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Cannot add solid primitives to an assembly")

            if not is_addable(op.kind, op.operation):
                raise CadenzaError(ErrorCode.BAD_MESSAGE, f"Cannot {op.operation} {op.kind}")
            
            fid = new_feature_id()
            added_features.add(fid)
            
            params = op.parameters.model_dump()
            if op.kind in ("box", "cylinder"):
                params["through"] = False
                params["draft_angle"] = 0.0
            elif op.kind == "hole":
                if not params.get("through") and params.get("depth") is None:
                    raise CadenzaError(ErrorCode.BAD_MESSAGE, "Blind hole requires depth")
            elif op.kind == "extrude":
                params["taper"] = 0.0
                params["twist"] = 0.0
                target = feature_by_id(ledger, params.get("target_feature", ""))
                if target is None:
                    raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, f"Extrude target missing: {params.get('target_feature')}")
                if getattr(target, "suppressed", False) or (isinstance(target, dict) and target.get("suppressed")):
                    raise CadenzaError(ErrorCode.BAD_MESSAGE, f"Extrude target is suppressed: {params.get('target_feature')}")
                target_kind = str(getattr(target, "kind", target.get("kind") if isinstance(target, dict) else ""))
                if target_kind not in ("sketch", "profile"):
                    raise CadenzaError(ErrorCode.BAD_MESSAGE, f"Extrude target must be a profile or sketch, got {target_kind}")
            
            value = {
                "id": fid,
                "name": op.name,
                "kind": op.kind,
                "operation": op.operation,
                "suppressed": False,
                "placement": op.placement.model_dump(),
                "parameters": params,
                "targets": [],
                "ai_context": {}
            }
            patches.append(PatchOp(op="add", ledger_id=request.ledger_id, path="/features/-", value=value))
            continue
            
        if op.feature_id in added_features:
            raise CadenzaError(ErrorCode.BAD_MESSAGE, "Cannot target a feature added in the same request")
            
        if op.feature_id in removed_feature_ids and not isinstance(op, RemoveFeature):
            raise CadenzaError(ErrorCode.BAD_MESSAGE, "Cannot edit a feature that is also being removed")
            
        feature = feature_by_id(ledger, op.feature_id)
        if feature is None:
            raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, f"Unknown feature {op.feature_id}")

        is_suppressed = getattr(feature, "suppressed", feature.get("suppressed") if isinstance(feature, dict) else False)

        if isinstance(op, (UpdateLinearPattern, UpdateCircularPattern)):
            expected = "linear_pattern" if isinstance(op, UpdateLinearPattern) else "circular_pattern"
            if feature.kind != expected or is_suppressed:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Select an active pattern of the requested kind.")
            reason = pattern_reason(feature.model_dump(mode="json"), ledger.model_dump(mode="json"))
            if reason:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, reason)
            path = f"/features/{op.feature_id}"
            if path in modified_targets or f"{path}/suppressed" in modified_targets:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Conflicting pattern updates")
            modified_targets.add(path)
            value = feature.model_dump(mode="json")
            value["parameters"]["count"] = op.count
            if isinstance(op, UpdateLinearPattern):
                value["parameters"].update(spacing=op.spacing, axis=DIRECTIONS[op.direction])
            else:
                value["placement"]["origin"] = [*op.center_xy, 0]
            patches.append(PatchOp(op="replace", ledger_id=request.ledger_id, path=path, value=value))

        elif isinstance(op, SetDimension):
            if is_suppressed:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Cannot edit a suppressed feature")
            kind = str(getattr(feature, "kind", feature.get("kind") if isinstance(feature, dict) else ""))
            if not is_supported_edit(kind, op.parameter):
                raise CadenzaError(ErrorCode.BAD_MESSAGE, f"Cannot edit {op.parameter} on {kind}")
            
            params = getattr(feature, "parameters", feature.get("parameters", {}) if isinstance(feature, dict) else None)
            if op.parameter == "depth" and (getattr(params, "through", False) or (isinstance(params, dict) and params.get("through"))):
                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Cannot edit depth of a through-hole")
                
            try:
                path = dimension_edit_path(ledger, op.feature_id, op.parameter)
            except ValueError as exc:
                raise CadenzaError(ErrorCode.PATCH_INVALID, str(exc)) from exc
            target_key = path
            if target_key in modified_targets:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, f"Conflicting writes to {path}")
            modified_targets.add(target_key)
            patches.append(PatchOp(op="replace", ledger_id=request.ledger_id, path=path, value=op.value))
            
        elif isinstance(op, SetOrigin):
            if is_suppressed:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Cannot edit a suppressed feature")
            kind = str(getattr(feature, "kind", feature.get("kind") if isinstance(feature, dict) else ""))
            if not is_origin_editable(kind):
                raise CadenzaError(ErrorCode.BAD_MESSAGE, f"Cannot edit origin on {kind}")
            
            placement = getattr(feature, "placement", feature.get("placement", {}) if isinstance(feature, dict) else None)
            if (getattr(placement, "relative_to", None) is not None) or (isinstance(placement, dict) and placement.get("relative_to") is not None):
                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Cannot edit origin of relative placement in B1")
                
            try:
                path = dimension_edit_path(ledger, op.feature_id, op.axis)
            except ValueError as exc:
                raise CadenzaError(ErrorCode.PATCH_INVALID, str(exc)) from exc
            target_key = path
            if target_key in modified_targets:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, f"Conflicting writes to {path}")
            modified_targets.add(target_key)
            patches.append(PatchOp(op="replace", ledger_id=request.ledger_id, path=path, value=op.value))
            
        elif isinstance(op, SetSuppressed):
            target_key = f"/features/{op.feature_id}/suppressed"
            if target_key in modified_targets or f"/features/{op.feature_id}" in modified_targets:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Conflicting suppress writes")
            modified_targets.add(target_key)
            patches.append(PatchOp(op="replace", ledger_id=request.ledger_id, path=target_key, value=op.suppressed))
            
        elif isinstance(op, RemoveFeature):
            target_key = f"remove_{op.feature_id}"
            if target_key in modified_targets:
                raise CadenzaError(ErrorCode.BAD_MESSAGE, "Duplicate remove")
            modified_targets.add(target_key)
            patches.append(PatchOp(op="remove", ledger_id=request.ledger_id, path=f"/features/{op.feature_id}"))

    # Reuse the normal patch/binding/reference validators on the complete result.
    # Checking dependencies against the old ledger incorrectly rejects grouped edits.
    candidate = apply_patch(ledger, patches).ledger
    has_solid = any(not f.suppressed and f.operation == "add" and f.kind != "profile"
        and (f.kind != "sketch" or f.parameters.height > 0) for f in candidate.features)
    if not has_solid and (ledger.features or added_features):
        raise CadenzaError(ErrorCode.BAD_MESSAGE, "Cannot remove or suppress the last solid feature")

    return patches


async def handle_design_command(session: Any, request: DesignCommand, emit: Callable, *,
                                prompt="", assumptions=None, verification_scope=None, context_path=None) -> None:
    if request.base_revision != session.revision:
        raise CadenzaError(ErrorCode.REVISION_CONFLICT, "The design changed. Refresh before editing.")
    if request.ledger_id not in session.stores:
        raise CadenzaError(ErrorCode.PATCH_UNKNOWN_TARGET, "Unknown parameter ledger.")
        
    patches = compile_command(session, request)
    _, ledgers = stage_operations(session, patches)
    
    outcome = await build_design(ledgers, session.primary_ledger_id)
    if not outcome.ok:
        raise CadenzaError(outcome.code or ErrorCode.GEOMETRY_KERNEL_FAILURE, outcome.detail, interference=outcome.interference)
        
    ops_summary = f"Preview of {len(request.operations)} source feature operation(s). Reused instances update together on Apply."
    await publish_preview(session, ledgers, outcome, request.message_id, emit,
                          summary=ops_summary, context_path=context_path,
                          prompt=prompt, assumptions=assumptions, verification_scope=verification_scope, display_ledger_id=request.ledger_id,
                          patch=[p.model_dump(mode="json", by_alias=True) for p in patches])
