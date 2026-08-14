"""Geometry service STAND-IN. Owned by Dev B (`cadenza/geometry/`) for real.

Blueprint §4 is the real pipeline: generate a build123d script from the ledger,
execute it, export GLB for the viewport and STEP for the client-side kernel.
None of that is mine to write, and the contract already names the interface
(`GeometryService`, `BuildResult`, `BBox`).

This module exists so the transport and agent layers have something to call
that either succeeds or fails, and so `revision` gets bumped by a "build" —
preserving the ledger invariant that a revision which exists has been built.
It computes an analytic bounding box and reports `stub=True` on every result so
no caller can mistake it for real geometry.

REPLACE with `GeometryService.build(ledger)` when it lands.
"""

from __future__ import annotations

from dataclasses import dataclass

from cadenza_backend.contracts_bridge import Ledger
from cadenza_backend.spatial_stub import _aabb


@dataclass(frozen=True)
class StubBuildResult:
    ok: bool
    bbox: list[float] | None
    """[min_x, min_y, min_z, max_x, max_y, max_z] in mm, or None when empty."""
    detail: str
    stub: bool = True


def build(ledger: Ledger) -> StubBuildResult:
    """Pretend to build the part. Reports the additive extent of the ledger."""
    additive = [
        f for f in ledger.features if not f.suppressed and f.operation == "add"
    ]
    if not additive:
        return StubBuildResult(ok=False, bbox=None, detail="Nothing additive to build.")

    los: list[tuple[float, float, float]] = []
    his: list[tuple[float, float, float]] = []
    for feature in additive:
        bounds = _aabb(feature)
        if bounds:
            los.append(bounds[0])
            his.append(bounds[1])

    if not los:
        return StubBuildResult(ok=False, bbox=None, detail="No feature produced bounds.")

    bbox = [
        min(p[0] for p in los),
        min(p[1] for p in los),
        min(p[2] for p in los),
        max(p[0] for p in his),
        max(p[1] for p in his),
        max(p[2] for p in his),
    ]
    size = (bbox[3] - bbox[0], bbox[4] - bbox[1], bbox[5] - bbox[2])
    return StubBuildResult(
        ok=True,
        bbox=bbox,
        detail=(
            f"Stub build OK — {len(ledger.features)} feature(s), "
            f"extent {size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f} mm. "
            "No B-rep was produced; awaiting the geometry service."
        ),
    )
