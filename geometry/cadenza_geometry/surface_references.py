"""Capture exact primitive roles and resolve only unique surviving faces."""
import math
from typing import Any
from build123d import GeomType, Location

from cadenza_contracts.surface_reference import SurfaceReference
from cadenza_geometry.spatial import _closest_point, _normal_at
from cadenza_geometry.frames import dir_to_world


def _source(built: Any, component_path: tuple[str, ...]):
    if component_path:
        leaf = built.instances.get("/".join(component_path))
        return (leaf.plan, leaf.part_id) if leaf else (None, None)
    return (built.plan, None) if hasattr(built.plan, "features") else (None, None)


def resolve_surface_reference(built: Any, reference: SurfaceReference) -> dict:
    result = resolve_native_surface(built, reference)
    result.pop("native_face", None)
    return result


def resolve_native_surface(built: Any, reference: SurfaceReference) -> dict:
    unresolved = {"reference": reference.model_dump(mode="json")}
    if built.plan.project_id != reference.ledger_id:
        return {**unresolved, "status": "missing", "message": "Reference belongs to another ledger."}
    plan, part_id = _source(built, reference.component_path)
    if plan is None or plan.project_id != reference.source_ledger_id or part_id != reference.source_part_id:
        return {**unresolved, "status": "missing", "message": "Referenced component or source no longer exists at this path."}
    feature = next((f for f in plan.features if f.id == reference.feature_id), None)
    if feature is None:
        return {**unresolved, "status": "missing", "message": "Referenced feature is missing or suppressed."}
    if feature.kind != reference.feature_kind or feature.operation != reference.operation:
        return {**unresolved, "status": "unsupported", "message": "Referenced feature kind or operation changed."}
    if feature.p.get("draft_angle", 0):
        return {**unresolved, "status": "unsupported", "message": "Drafted primitive references are not supported."}
    if reference.feature_kind == "extrude":
        if feature.p.get("taper") or not feature.p.get("profile_normal"):
            return {**unresolved, "status": "unsupported", "message": "Only straight profile extrusions support this reference."}
        profile = plan.by_id(reference.profile_id)
        if feature.p.get("target_feature") != reference.profile_id or profile is None or profile.kind != "profile":
            return {**unresolved, "status": "missing", "message": "Source profile was removed or replaced."}
        if reference.entity_id is not None and not any(e["id"] == reference.entity_id for e in profile.p["entities"]):
            return {**unresolved, "status": "missing", "message": "Source sketch entity was removed."}

    component_id = "/".join(reference.component_path) or None
    matches = []
    uncertain = False
    for index, (cid, candidate, spec) in built.attribution.items():
        if cid != component_id:
            continue
        # A tied face might be attributed to a later feature. Never turn that
        # tie into a missing face or silently accept its chosen winner.
        if reference.feature_id in built.ambiguity.get(index, ()):
            uncertain = True
        if (candidate.id == reference.feature_id and spec.label == reference.role
                and getattr(spec, "profile_id", None) == reference.profile_id
                and getattr(spec, "entity_id", None) == reference.entity_id):
            matches.append(index)
            uncertain |= bool(built.ambiguity.get(index))
    if uncertain or len(matches) > 1:
        return {**unresolved, "status": "ambiguous", "candidate_count": len(matches),
                "message": "Surface role has multiple surviving faces or competing feature attribution; repair the reference."}
    if not matches:
        return {**unresolved, "status": "missing", "candidate_count": 0,
                "message": "The referenced surface no longer survives in the final solid."}
    face = built.faces[matches[0]]
    center = face.center()
    _, point = _closest_point(face, (center.X, center.Y, center.Z))
    return {**unresolved, "status": "resolved", "candidate_count": 1,
            "native_face": face,
            "face": {"center": [center.X, center.Y, center.Z], "point": list(point),
                     "normal": list(_normal_at(face, point)), "area_mm2": face.area,
                     "planar": face.geom_type == GeomType.PLANE}}


def validate_face_additions(built: Any, reference: SurfaceReference, operations: list[dict]) -> dict:
    """Check origins on the trimmed selected face and local construction direction."""
    resolved = resolve_surface_reference(built, reference)
    if resolved["status"] != "resolved" or not resolved["face"]["planar"]:
        return {"status": "unsupported", "message": "Choose a single planar face for this addition."}
    normal = resolved["face"]["normal"]
    for op in operations:
        if op.get("op") != "add_feature" or op.get("kind") not in ("box", "cylinder", "hole"):
            return {"status": "unsupported", "message": "Face additions support boxes, cylinders and holes. Use feature controls for other operations."}
        placement = op["placement"]
        captured = capture_surface_reference(built, placement["origin"], normal)
        if captured.get("reference") != reference.model_dump(mode="json"):
            return {"status": "missing", "message": "The proposed origin is not on the selected face. Specify its position on that face."}
        axis = dir_to_world(Location((0, 0, 0), tuple(placement["rotation_deg"])), (0, 0, 1))
        # Holes cut along -Z; additive primitives grow along +Z. Subtractive
        # boxes/cylinders must instead point their +Z into the selected solid.
        sign = -1 if op["operation"] == "subtract" and op["kind"] != "hole" else 1
        if sum(a * b for a, b in zip(axis, normal)) * sign < 1 - 1e-7:
            return {"status": "unsupported", "message": "The proposed direction does not match the selected face. Specify an outward addition or inward cut."}
    return {"status": "resolved"}


def capture_surface_reference(built: Any, point: list[float], normal: list[float] | None = None) -> dict:
    if len(point) != 3 or not all(math.isfinite(v) for v in point):
        raise ValueError("Capture requires a finite 3D point in ledger coordinates.")
    if normal is not None:
        if len(normal) != 3 or not all(math.isfinite(v) for v in normal) or math.hypot(*normal) < 1e-12:
            raise ValueError("Capture normal must be a finite nonzero 3D vector.")
        magnitude = math.hypot(*normal)
        normal = [v / magnitude for v in normal]
    indices = []
    for index, face in enumerate(built.faces):
        distance, on_face = _closest_point(face, (point[0], point[1], point[2]))
        # Capture must touch the actual final surface. This is deliberately
        # stricter than ordinary point-and-speak's nearest-face convenience.
        if distance > 1e-4:
            continue
        if normal is not None and sum(a * b for a, b in zip(_normal_at(face, on_face), normal)) < 0.99:
            continue
        indices.append(index)
    if not indices:
        return {"status": "missing", "message": "No surface matches the capture point and normal."}
    if len(indices) != 1 or any(built.ambiguity.get(i) for i in indices):
        return {"status": "ambiguous", "message": "Capture touches multiple faces or competing features; choose a clear face interior."}
    entry = built.attribution.get(indices[0])
    if entry is None:
        return {"status": "unsupported", "message": "Selected face has no supported feature attribution."}
    cid, feature, spec = entry
    path = tuple(cid.split("/")) if cid else ()
    plan, part_id = _source(built, path)
    if plan is None:
        return {"status": "missing", "message": "Selected component source is unavailable."}
    try:
        reference = SurfaceReference.model_validate({
            "ledger_id": built.plan.project_id, "component_path": path,
            "source_ledger_id": plan.project_id, "source_part_id": part_id,
            "feature_id": feature.id, "feature_kind": feature.kind,
            "operation": feature.operation, "role": spec.label,
            "profile_id": getattr(spec, "profile_id", None), "entity_id": getattr(spec, "entity_id", None),
        })
    except ValueError:
        return {"status": "unsupported", "message": "This feature does not support stable surface references."}
    result = resolve_surface_reference(built, reference)
    if result["status"] != "resolved":
        result.pop("reference", None)
    return result
