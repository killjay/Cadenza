"""The public entry point: ledger in, geometry out.

    service = Build123dGeometryService()
    result  = service.build(ledger_json)             # -> BuildResult
    hit     = service.probe(ledger_json, SpatialQuery(point=(10.5, 0.0, 20.0)))

`probe` needs the same solid `build` produced. Rebuilding it per click would
put a 2-5 second kernel round trip in front of every selection, so builds are
memoised on the ledger's exact content -- a probe against a ledger just built
is a cache hit and costs only the extrema query.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any

from build123d import Face, Shape

from cadenza_geometry.builder import AssemblyLeaf, build_assembly, build_solid
from cadenza_geometry.frames import to_world, dir_to_world
from cadenza_geometry.contracts import (
    BBox,
    BuildResult,
    GeometryError,
    GeometryErrorCode,
    SpatialHit,
    SpatialQuery,
)
from cadenza_geometry.exporters import export_glb_bytes, export_step_bytes
from cadenza_geometry.plan import BuildPlan, AssemblyPlan, RFeature, resolve
from cadenza_geometry.provenance import Spec, attribute
from cadenza_geometry.spatial import probe as spatial_probe


@dataclass
class _Built:
    """One built model, kept so a probe does not have to rebuild it."""

    plan: BuildPlan | AssemblyPlan
    solid: Shape
    faces: list[Face]
    attribution: dict[int, tuple[str | None, RFeature, Spec]]
    # Assembly specs remain source-local; faces are world-space. Current
    # assembly consumers use spec.label and explicit instance transforms.
    ambiguity: dict[int, list[str]]
    build_ms: float
    instances: dict[str, AssemblyLeaf]
    contacts: list[dict]


def _fingerprint(ledger: Any) -> str:
    doc = ledger.model_dump(mode="json") if hasattr(ledger, "model_dump") else ledger
    try:
        blob = json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        blob = repr(doc)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Build123dGeometryService:
    """Implements the `GeometryService` protocol in contracts.py."""

    def __init__(self, cache_size: int = 4) -> None:
        self._cache: OrderedDict[str, _Built] = OrderedDict()
        self._cache_size = max(1, cache_size)

    # ---------------------------------------------------------------- build --

    def _compile(self, ledger: Any, all_ledgers: dict[str, Any] | None = None) -> _Built:
        # External part edits must invalidate an otherwise unchanged assembly.
        key = _fingerprint({"ledger": ledger.model_dump(mode="json") if hasattr(ledger, "model_dump") else ledger,
                            "dependencies": {lid: doc.model_dump(mode="json") if hasattr(doc, "model_dump") else doc
                                             for lid, doc in (all_ledgers or {}).items()}})
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        t0 = time.perf_counter()
        plan = resolve(ledger, all_ledgers=all_ledgers)                 # invariant gates, no kernel yet
        
        contacts: list[dict] = []
        instances: dict[str, AssemblyLeaf] = {}
        if isinstance(plan, AssemblyPlan):
            leaves: list[AssemblyLeaf] = []
            solid = build_assembly(plan, leaves=leaves, contacts=contacts)
            faces = []
            attribution: dict[int, tuple[str | None, RFeature, Spec]] = {}
            ambiguity: dict[int, list[str]] = {}
            for leaf in leaves:
                component_path = "/".join(leaf.path)
                instances[component_path] = leaf
                local_faces = list(leaf.solid.faces())
                local_attr, local_ambiguity = attribute(leaf.plan, local_faces)
                offset = len(faces)
                faces.extend(leaf.location * face for face in local_faces)
                attribution.update({offset + i: (component_path, feature, spec)
                                    for i, (feature, spec) in local_attr.items()})
                ambiguity.update({offset + i: rivals for i, rivals in local_ambiguity.items()})
        else:
            solid = build_solid(plan)              # exact B-rep
            faces = list(solid.faces())
            attr, ambiguity = attribute(plan, faces)
            attribution = {i: (None, feat, spec) for i, (feat, spec) in attr.items()}
        built = _Built(
            plan=plan,
            solid=solid,
            faces=faces,
            attribution=attribution,
            ambiguity=ambiguity,
            build_ms=(time.perf_counter() - t0) * 1000.0,
            instances=instances, contacts=contacts,
        )

        self._cache[key] = built
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return built

    def build(self, ledger: Any, all_ledgers: dict[str, Any] | None = None) -> BuildResult:
        """Ledger -> GLB + STEP + metadata. Raises GeometryError on bad input."""
        built = self._compile(ledger, all_ledgers=all_ledgers)
        t0 = time.perf_counter()
        step = export_step_bytes(built.solid)
        glb, entities = export_glb_bytes(built.faces, built.attribution, built.plan,
            component_parts={path: leaf.part_id for path, leaf in built.instances.items()},
            component_sources={path: leaf.plan.project_id for path, leaf in built.instances.items()},
            ambiguity=built.ambiguity)
        export_ms = (time.perf_counter() - t0) * 1000.0

        bb = built.solid.bounding_box()
        warnings = list(built.plan.warnings)

        unattributed = [i for i in range(len(built.faces)) if i not in built.attribution]
        if unattributed:
            # Not fatal -- the model renders and exports fine -- but a click on
            # such a face cannot be traced to a ledger node, so it is worth
            # surfacing rather than discovering through a silent NO_HIT.
            warnings.append(
                f"{len(unattributed)} of {len(built.faces)} faces could not be traced "
                "to a ledger feature; selection there may be unavailable or uncertain"
            )

        return BuildResult(
            glb=glb,
            step=step,
            bbox=BBox(min=(bb.min.X, bb.min.Y, bb.min.Z), max=(bb.max.X, bb.max.Y, bb.max.Z)),
            volume=sum(solid.volume for solid in built.solid.solids()),
            entities=entities,
            revision=built.plan.revision,
            build_ms=built.build_ms + export_ms,
            warnings=warnings,
        )

    # ---------------------------------------------------------------- probe --

    def probe(self, ledger: Any, query: SpatialQuery, all_ledgers: dict[str, Any] | None = None) -> SpatialHit | None:
        """A 3D click -> the ledger node that generated the nearest face."""
        built = self._compile(ledger, all_ledgers=all_ledgers)
        hit = spatial_probe(
            built.plan, built.faces, built.attribution, built.ambiguity, query, built.solid
        )
        if hit is not None and hit.component_id in built.instances:
            leaf = built.instances[hit.component_id]
            feature = next(f for f in leaf.plan.features if f.id == hit.feature_id)
            hit = replace(hit, source_ledger_id=leaf.plan.project_id, source_part_id=leaf.part_id,
                feature_name=feature.name, feature_kind=feature.kind,
                source_point=to_world(leaf.location.inverse(), hit.point_on_face),
                source_normal=dir_to_world(leaf.location.inverse(), hit.normal))
        return hit


    def run_probe_query(self, ledger: Any, query: dict, all_ledgers: dict[str, Any] | None = None) -> dict:
        """Executes a geometric probe query (bounding box, collision, etc.)"""
        from cadenza_geometry.probe import run_probe_query
        from cadenza_contracts.surface_reference import SurfaceReference
        from cadenza_geometry.surface_references import capture_surface_reference, resolve_surface_reference, validate_face_additions
        if query.get("query_type") == "resolve_surface_reference":
            raw = query.get("reference")
            if query.get("reference_id") is not None:
                doc = ledger.model_dump(mode="json") if hasattr(ledger, "model_dump") else ledger
                raw = doc.get("surface_references", {}).get(query["reference_id"])
            if raw is None:
                return {"status": "missing", "message": "Saved surface reference was not found."}
            reference = SurfaceReference.model_validate(raw)
            # An emptied ledger has no solid to compile, but saved annotations
            # must remain inspectable after deletion of its final feature.
            doc = ledger.model_dump(mode="json") if hasattr(ledger, "model_dump") else ledger
            empty = (not doc["assembly"].get("components") if doc.get("assembly") is not None
                     else not any(not f.get("suppressed", False) for f in doc.get("features", [])))
            if empty:
                return {"status": "missing", "reference": reference.model_dump(mode="json"),
                        "message": "Referenced geometry is empty or suppressed."}
            return resolve_surface_reference(self._compile(ledger, all_ledgers=all_ledgers), reference)
        if query.get("query_type") == "instance_context":
            from pydantic import TypeAdapter
            from cadenza_contracts.part_commands import OccurrencePath
            path = TypeAdapter(OccurrencePath).validate_python(query.get("component_path"))
            built = self._compile(ledger, all_ledgers=all_ledgers)
            leaf = built.instances.get("/".join(path))
            if leaf is None or leaf.part_id is not None or leaf.plan.project_id != query.get("source_ledger_id"):
                raise GeometryError(GeometryErrorCode.INVALID_PARAMETER, "Occurrence does not reference the requested source part.")
            origin = to_world(leaf.location, (0, 0, 0))
            axes = [dir_to_world(leaf.location, axis) for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]
            matrix = [value for row in range(3) for value in (*[axis[row] for axis in axes], origin[row])]
            return {"component_path": path, "source_ledger_id": leaf.plan.project_id,
                    "source_to_project": [*matrix, 0.0, 0.0, 0.0, 1.0]}
        built = self._compile(ledger, all_ledgers=all_ledgers)
        if query.get("query_type") == "capture_planar_attachment":
            from cadenza_contracts.assembly_commands import AddPlanarAttachment
            from cadenza_geometry.planar_attachments import capture_pair
            return capture_pair(built, AddPlanarAttachment.model_validate(query["operation"]))
        if query.get("query_type") == "attachment_contacts":
            return {"contacts": built.contacts, "interference_checked": True}
        if query.get("query_type") == "validate_face_additions":
            return validate_face_additions(built, SurfaceReference.model_validate(query["reference"]), query["operations"])
        if query.get("query_type") == "capture_surface_reference":
            return capture_surface_reference(built, query.get("point", []), query.get("normal"))
        return run_probe_query(built, query)

    def probe_point_legacy(
        self,
        ledger: Any,
        point: tuple[float, float, float],
        max_distance: float | None = None,
        normal: tuple[float, float, float] | None = None,
        all_ledgers: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Convenience wrapper returning the blueprint §5 dict shape directly.

        Pass `normal` (from `TargetRef.normal`) whenever it is known — it is the
        single biggest factor in whether a stored coordinate still resolves to
        the surface the user meant. See NOTES.md.
        """
        hit = self.probe(
            ledger,
            SpatialQuery(
                point=tuple(point),
                max_distance=max_distance,
                normal=tuple(normal) if normal is not None else None,
            ),
            all_ledgers=all_ledgers,
        )
        return hit.to_dict() if hit is not None else None


# --------------------------------------------------------------------------- #
# Module-level conveniences
# --------------------------------------------------------------------------- #

_default = Build123dGeometryService()


def ledger_to_glb_step(ledger: Any) -> tuple[bytes, bytes]:
    """The milestone-1 ask: ledger JSON -> (glb_bytes, step_bytes)."""
    result = _default.build(ledger)
    return result.glb, result.step


def build(ledger: Any) -> BuildResult:
    return _default.build(ledger)


def probe(
    ledger: Any,
    point: tuple[float, float, float],
    max_distance: float | None = None,
    normal: tuple[float, float, float] | None = None,
):
    return _default.probe(
        ledger,
        SpatialQuery(
            point=tuple(point),
            max_distance=max_distance,
            normal=tuple(normal) if normal is not None else None,
        ),
    )
