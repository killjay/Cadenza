"""In-memory export. Both functions return `bytes`; neither touches the disk.

STEP goes through build123d's own `export_step`, which does accept a BytesIO
(it branches to `STEPCAFControl_Writer.WriteStream`). GLB goes through our own
writer -- see the header of glb.py for why build123d's cannot be used.
"""

from __future__ import annotations

import io

from build123d import Face, Shape, export_step  # type: ignore

from cadenza_geometry.contracts import GeometryError, GeometryErrorCode
from cadenza_geometry.glb import MeshGroup, tessellate_face, write_glb
from cadenza_geometry.plan import AssemblyPlan, BuildPlan, RFeature
from cadenza_geometry.provenance import Spec

# Tessellation quality. Linear deflection is in mm and is what actually drives
# triangle count; 0.05 keeps a Ø10 bore visually round while staying small.
LINEAR_DEFLECTION = 0.05
ANGULAR_DEFLECTION = 0.25

UNATTRIBUTED = "_unattributed"


def export_step_bytes(shape: Shape) -> bytes:
    """Exact B-rep as STEP AP214, in memory. This is what occt-wasm reloads."""
    buf = io.BytesIO()
    try:
        export_step(shape, buf)
    except Exception as exc:
        raise GeometryError(
            GeometryErrorCode.EXPORT_FAILURE,
            "STEP export failed",
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    data = buf.getvalue()
    if not data.startswith(b"ISO-10303-21"):
        raise GeometryError(
            GeometryErrorCode.EXPORT_FAILURE,
            "STEP export produced something that is not a STEP file",
        )
    return data


def export_glb_bytes(
    faces: list[Face],
    attribution: dict[int, tuple[str | None, RFeature, Spec]],
    plan: BuildPlan | AssemblyPlan,
    linear_deflection: float = LINEAR_DEFLECTION,
    angular_deflection: float = ANGULAR_DEFLECTION,
    component_parts: dict[str, str | None] | None = None,
    component_sources: dict[str, str] | None = None,
    ambiguity: dict[int, list[str]] | None = None,
) -> tuple[bytes, dict[str, list[str]]]:
    """Tessellate, grouped by ledger feature and side, and pack into a GLB.

    Returns (glb_bytes, entities) where `entities` maps feature_id -> the
    semantic labels that actually survived into the final solid.
    """
    # Build mapping from component_id to part_id if this is an AssemblyPlan
    comp_to_part: dict[str, str | None] = {}
    if isinstance(plan, AssemblyPlan):
        for comp in plan.components:
            comp_to_part[comp.id] = comp.part_id

    comp_to_part.update(component_parts or {})

    # Group by (feature_id, label)
    groups: dict[tuple[str, str, str], MeshGroup] = {}
    profile_faces: dict[tuple[str, str, str], list[int]] = {}

    def get_group(fid: str, label: str, comp_id: str | None = None, entity_id: str = "") -> MeshGroup:
        k = (fid, label, entity_id)
        if k not in groups:
            part_id = comp_to_part.get(comp_id) if comp_id else None
            groups[k] = MeshGroup(name=fid, label=label, component_id=comp_id, part_id=part_id)
        return groups[k]

    for i, face in enumerate(faces):
        hit = attribution.get(i)
        if hit is None:
            group = get_group(UNATTRIBUTED, "")
            group.selection_error = "This face has no reliable source identity. Edit from the feature tree or select a supported surface."
        else:
            comp_id, feat, spec = hit
            fid = f"{comp_id}:{feat.id}" if comp_id else feat.id
            entity_id = getattr(spec, "entity_id", None)
            group = get_group(fid, spec.label, comp_id, entity_id or "")
            if (ambiguity or {}).get(i):
                group.selection_error = "Multiple features contribute to this surface. Select an unambiguous surface or edit from the feature tree."
            profile_id = getattr(spec, "profile_id", None)
            if feat.kind == "extrude" and profile_id:
                key = (fid, spec.label, entity_id or "")
                profile_faces.setdefault(key, []).append(i)
                group.surface_reference = {"version": 1, "ledger_id": plan.project_id,
                    "source_ledger_id": (component_sources or {}).get(comp_id, plan.project_id),
                    "source_part_id": comp_to_part.get(comp_id) if comp_id else None,
                    "component_path": comp_id.split("/") if comp_id else [],
                    "feature_id": feat.id, "feature_kind": "extrude", "operation": "add",
                    "profile_id": profile_id, "entity_id": entity_id, "role": spec.label}
        tessellate_face(face, group, linear_deflection, angular_deflection)

    for key, indices in profile_faces.items():
        if len(indices) != 1 or any((ambiguity or {}).get(i) for i in indices):
            groups[key].surface_reference = None
            groups[key].selection_error = "This sketch surface is split or ambiguous. Select an unambiguous surface or edit from the feature tree."

    # Preserve ledger order so node order in the GLB is stable across rebuilds;
    # a viewer diffing scenes then sees a moved vertex, not a reshuffled scene.
    ordered: list[MeshGroup] = []
    if isinstance(plan, AssemblyPlan):
        # Full instance paths include nested external sources, not just local
        # parts of the top-level plan. Never drop an already attributed group.
        ordered.extend(groups[key] for key in sorted(groups) if key[0] != UNATTRIBUTED)
    else:
        for f in plan.features:
            f_groups = [groups[k] for k in groups if k[0] == f.id]
            f_groups.sort(key=lambda g: g.label)
            ordered.extend(f_groups)

    u_groups = [groups[k] for k in groups if k[0] == UNATTRIBUTED]
    ordered.extend(u_groups)

    try:
        data = write_glb([g for g in ordered if not g.is_empty])
    except Exception as exc:
        raise GeometryError(
            GeometryErrorCode.EXPORT_FAILURE,
            "GLB export failed",
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc

    entities: dict[str, list[str]] = {}
    for g in ordered:
        if not g.is_empty and g.name != UNATTRIBUTED:
            if g.name not in entities:
                entities[g.name] = []
            if g.label:
                entities[g.name].append(g.label)

    return data, entities
