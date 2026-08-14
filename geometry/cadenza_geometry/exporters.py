"""In-memory export. Both functions return `bytes`; neither touches the disk.

STEP goes through build123d's own `export_step`, which does accept a BytesIO
(it branches to `STEPCAFControl_Writer.WriteStream`). GLB goes through our own
writer -- see the header of glb.py for why build123d's cannot be used.
"""

from __future__ import annotations

import io

from build123d import Face, Shape, export_step

from cadenza_geometry.contracts import GeometryError, GeometryErrorCode
from cadenza_geometry.glb import MeshGroup, tessellate_face, write_glb
from cadenza_geometry.plan import BuildPlan
from cadenza_geometry.provenance import Spec
from cadenza_geometry.plan import RFeature

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
    attribution: dict[int, tuple[RFeature, Spec]],
    plan: BuildPlan,
    linear_deflection: float = LINEAR_DEFLECTION,
    angular_deflection: float = ANGULAR_DEFLECTION,
) -> tuple[bytes, dict[str, list[str]]]:
    """Tessellate, grouped by ledger feature, and pack into a GLB.

    Returns (glb_bytes, entities) where `entities` maps feature_id -> the
    semantic labels that actually survived into the final solid.
    """
    # Preserve ledger order so node order in the GLB is stable across rebuilds;
    # a viewer diffing scenes then sees a moved vertex, not a reshuffled scene.
    groups: dict[str, MeshGroup] = {f.id: MeshGroup(name=f.id) for f in plan.features}
    groups[UNATTRIBUTED] = MeshGroup(name=UNATTRIBUTED)

    for i, face in enumerate(faces):
        hit = attribution.get(i)
        if hit is None:
            group = groups[UNATTRIBUTED]
        else:
            feat, spec = hit
            group = groups[feat.id]
            group.labels.add(spec.label)
        tessellate_face(face, group, linear_deflection, angular_deflection)

    ordered = [groups[f.id] for f in plan.features] + [groups[UNATTRIBUTED]]
    try:
        data = write_glb([g for g in ordered if not g.is_empty])
    except Exception as exc:
        raise GeometryError(
            GeometryErrorCode.EXPORT_FAILURE,
            "GLB export failed",
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc

    entities = {
        g.name: sorted(g.labels) for g in ordered if not g.is_empty and g.name != UNATTRIBUTED
    }
    return data, entities
