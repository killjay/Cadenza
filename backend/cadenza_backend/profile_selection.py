"""Source identity and driving dimensions for supported selected profile faces."""
import json
import re

from cadenza_contracts.ledger import Ledger
from cadenza_contracts.surface_reference import SurfaceReference
from cadenza_contracts.errors import CadenzaError, ErrorCode
from cadenza_contracts.parameters import dimension_edit_path
from cadenza_contracts.patch import apply_patch, PatchOp


def profile_context(reference: SurfaceReference, ledgers: dict[str, Ledger]) -> str:
    if reference.feature_kind != "extrude":
        return ""
    source = ledgers.get(reference.source_ledger_id)
    features = source.features if source else []
    if source is not None and reference.source_part_id is not None and source.assembly is not None:
        part = next((p for p in source.assembly.parts if p.id == reference.source_part_id), None)
        features = part.features if part else []
    profile = next((f for f in features if f.id == reference.profile_id and f.kind == "profile"), None)
    if profile is None or profile.kind != "profile":
        return " Source sketch is unavailable; ask for clarification."
    constraints = [c.model_dump(mode="json") for c in profile.parameters.constraints
                   if c.kind == "dimension" and c.entity_id == reference.entity_id]
    bindings = [b.model_dump(mode="json") for b in source.relationships.bindings
                if b.feature_id in {reference.profile_id, reference.feature_id}] if source else []
    return (f" Selected profile identity: extrusion {reference.feature_id}; profile {reference.profile_id}; "
        f"entity {reference.entity_id}; role {reference.role}. Source ledger: {reference.source_ledger_id}. "
        f"Driving dimensions: {json.dumps(constraints)}. Named bindings: {json.dumps(bindings)}. "
        "Edit the named driver when bound, otherwise the dimension constraint value, never the entity seed or solution cache. "
        "Preserve shared centres and unrelated entities. Caps select the extrusion, not a particular opening; "
        "ask for clarification if the requested dimension or entity is unclear.")


def verify_selected_diameter(before: dict[str, Ledger], after: dict[str, Ledger], ref: SurfaceReference, prompt: str) -> None:
    """Independent check for explicit single-opening diameter requests only."""
    match = re.fullmatch(r"\s*(?:(?:enlarge|resize|change|set) (?:this|the selected) (?:opening|hole|bore)(?: diameter)? to |set (?:the )?diameter to )"
        r"([0-9]+(?:\.[0-9]+)?)\s*mm(?: and keep it cent(?:re|er)ed)?[.!]?\s*", prompt, re.I)
    if not match or ref.feature_kind != "extrude" or ref.role != "bore" or ref.source_part_id is not None:
        return
    ledger = before[ref.source_ledger_id]
    profile = next(f for f in ledger.features if f.id == ref.profile_id)
    if profile.kind != "profile":
        return  # Reference resolution rejects this before the model is called.
    dimensions = [c for c in profile.parameters.constraints if c.kind == "dimension"
                  and c.entity_id == ref.entity_id and c.parameter == "diameter"]
    if len(dimensions) != 1:
        raise CadenzaError(ErrorCode.AGENT_NEEDS_CLARIFICATION, "Choose one driving diameter constraint for the selected opening.")
    try:
        path = dimension_edit_path(ledger, profile.id, "value", dimensions[0].id)
        expected = {**before, ledger.project_id: apply_patch(ledger, [PatchOp(op="replace", path=path, value=float(match[1]))])}
    except ValueError as exc:
        raise CadenzaError(ErrorCode.PATCH_INVALID, str(exc)) from exc
    def content(documents):
        return {lid: doc.model_dump(mode="json", exclude={"revision"}) for lid, doc in documents.items()}
    if content(expected) != content(after):
        raise CadenzaError(ErrorCode.PATCH_INVALID,
            "The proposal does not match the selected opening's requested diameter, or changes unrelated design data.")
