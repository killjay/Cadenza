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
from dataclasses import dataclass
from typing import Any

from build123d import Face, Shape

from cadenza_geometry.builder import build_solid
from cadenza_geometry.contracts import (
    BBox,
    BuildResult,
    GeometryError,
    GeometryErrorCode,
    SpatialHit,
    SpatialQuery,
)
from cadenza_geometry.exporters import export_glb_bytes, export_step_bytes
from cadenza_geometry.plan import BuildPlan, RFeature, resolve
from cadenza_geometry.provenance import Spec, attribute
from cadenza_geometry.spatial import probe as spatial_probe


@dataclass
class _Built:
    """One built model, kept so a probe does not have to rebuild it."""

    plan: BuildPlan
    solid: Shape
    faces: list[Face]
    attribution: dict[int, tuple[RFeature, Spec]]
    ambiguity: dict[int, list[str]]
    build_ms: float


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

    def _compile(self, ledger: Any) -> _Built:
        key = _fingerprint(ledger)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        t0 = time.perf_counter()
        plan = resolve(ledger)                 # invariant gates, no kernel yet
        solid = build_solid(plan)              # exact B-rep
        faces = list(solid.faces())
        attribution, ambiguity = attribute(plan, faces)
        built = _Built(
            plan=plan,
            solid=solid,
            faces=faces,
            attribution=attribution,
            ambiguity=ambiguity,
            build_ms=(time.perf_counter() - t0) * 1000.0,
        )

        self._cache[key] = built
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return built

    def build(self, ledger: Any) -> BuildResult:
        """Ledger -> GLB + STEP + metadata. Raises GeometryError on bad input."""
        built = self._compile(ledger)
        t0 = time.perf_counter()
        step = export_step_bytes(built.solid)
        glb, entities = export_glb_bytes(built.faces, built.attribution, built.plan)
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
                "to a ledger feature; clicks there will resolve to the nearest named face"
            )

        return BuildResult(
            glb=glb,
            step=step,
            bbox=BBox(min=(bb.min.X, bb.min.Y, bb.min.Z), max=(bb.max.X, bb.max.Y, bb.max.Z)),
            volume=built.solid.volume,
            entities=entities,
            revision=built.plan.revision,
            build_ms=built.build_ms + export_ms,
            warnings=warnings,
        )

    # ---------------------------------------------------------------- probe --

    def probe(self, ledger: Any, query: SpatialQuery) -> SpatialHit | None:
        """A 3D click -> the ledger node that generated the nearest face."""
        built = self._compile(ledger)
        return spatial_probe(
            built.plan, built.faces, built.attribution, built.ambiguity, query, built.solid
        )


    def run_probe_query(self, ledger: Any, query: dict) -> dict:
        """Executes a geometric probe query (bounding box, collision, etc.)"""
        from cadenza_geometry.probe import run_probe_query
        built = self._compile(ledger)
        return run_probe_query(built, query)

    def probe_point_legacy(
        self,
        ledger: Any,
        point: tuple[float, float, float],
        max_distance: float | None = None,
        normal: tuple[float, float, float] | None = None,
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
