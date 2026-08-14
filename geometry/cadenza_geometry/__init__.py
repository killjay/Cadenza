"""cadenza_geometry — JSON ledger -> exact B-rep solid -> GLB + STEP, in memory.

Owned by Dev B. The backend should import ONLY from this module:

    from cadenza_geometry import Build123dGeometryService, SpatialQuery

    svc = Build123dGeometryService()
    result = svc.build(ledger)                 # BuildResult: .glb, .step, .bbox
    hit = svc.probe_point(ledger, [10.5, 0, 20])
    # -> {"node_id": "feat_base_plate_9f2a", "feature": "top_face", "distance": 0.0, ...}

Interface types are re-exported from `contracts.py`, which is the single file to
change when `cadenza_contracts.geometry` lands.
"""

from cadenza_geometry.contracts import (
    HAVE_CONTRACTS,
    BBox,
    BuildResult,
    EntityRef,
    GeometryError,
    GeometryErrorCode,
    GeometryService,
    SpatialHit,
    SpatialQuery,
)
from cadenza_geometry.plan import BuildPlan, RFeature, resolve
from cadenza_geometry.service import (
    Build123dGeometryService,
    build,
    ledger_to_glb_step,
    probe,
)

__all__ = [
    "Build123dGeometryService",
    "GeometryService",
    "BuildResult",
    "SpatialQuery",
    "SpatialHit",
    "EntityRef",
    "BBox",
    "GeometryError",
    "GeometryErrorCode",
    "BuildPlan",
    "RFeature",
    "resolve",
    "build",
    "probe",
    "ledger_to_glb_step",
    "HAVE_CONTRACTS",
]
