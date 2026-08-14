"""Stage 2: BuildPlan -> exact B-rep solid.

Uses build123d's algebra API (`+` / `-`) rather than the `BuildPart` context the
blueprint sketches. The blueprint's §4 snippet selects the face to cut with
`faces().sort_by(Axis.Z)[-1]` -- a topological selector, and exactly the thing
§7 says we do not do. Every feature here is placed by explicit coordinate, so
nothing depends on face ordering and the build is reproducible under edits.
"""

from __future__ import annotations

import math

from build123d import Align, Box, Cylinder, Edge, Face, Location, Pos, Shape, Wire, extrude

from cadenza_geometry.contracts import GeometryError, GeometryErrorCode
from cadenza_geometry.frames import feature_location
from cadenza_geometry.gear import Arc, Line, Spline, gear_profile
from cadenza_geometry.plan import BuildPlan, RFeature

# Overshoot for cutters, so a coplanar boolean never has to be decided exactly.
EPS = 0.01
_BASE_ALIGN = (Align.CENTER, Align.CENTER, Align.MIN)


def _gear(f: RFeature) -> Shape:
    """A spur gear: the involute outline from `gear.py`, extruded.

    Which is the whole trick — a gear is not a primitive the kernel knows, it is
    a closed 2D wire that gets swept. FreeCAD's gear workbenches do exactly this;
    all the gear-ness is in the profile, and the solid step is the same extrude
    a sketched pocket would get.

    Each flank becomes ONE spline edge rather than a run of short segments. That
    is not only for the face count: `provenance.py` attributes faces by matching
    analytic surfaces, and a flank split into twenty facets would be twenty
    faces the attributor has to recognise individually.
    """
    assert f.gear is not None  # plan.py resolves this for every gear
    edges: list[Edge] = []
    for seg in gear_profile(f.gear):
        if isinstance(seg, Spline):
            edges.append(Edge.make_spline([(x, y, 0.0) for x, y in seg.points]))
        elif isinstance(seg, Line):
            edges.append(Edge.make_line((*seg.start, 0.0), (*seg.end, 0.0)))
        else:
            # Three-point arc: OCC needs an interior point to fix the sense, and
            # a start/end pair alone leaves the major/minor choice ambiguous.
            mid = seg.midpoint
            edges.append(
                Edge.make_three_point_arc(
                    (seg.radius * math.cos(seg.start), seg.radius * math.sin(seg.start), 0.0),
                    (mid[0], mid[1], 0.0),
                    (seg.radius * math.cos(seg.end), seg.radius * math.sin(seg.end), 0.0),
                )
            )

    wire = Wire(edges)
    if not wire.is_closed:
        raise GeometryError(
            GeometryErrorCode.KERNEL_FAILURE,
            f"the gear outline for {f.id!r} did not close",
            f.id,
            detail="the profile segments did not join end-to-end",
        )
    return extrude(Face(wire), amount=f.p["height"])


def _primitive(f: RFeature) -> Shape:
    if f.kind == "box":
        return Box(f.p["length"], f.p["width"], f.p["height"], align=_BASE_ALIGN)
    if f.kind == "cylinder":
        return Cylinder(radius=f.p["diameter"] / 2, height=f.p["height"], align=_BASE_ALIGN)
    if f.kind == "gear":
        return _gear(f)
    raise GeometryError(
        GeometryErrorCode.UNSUPPORTED_FEATURE, f"no primitive for kind {f.kind!r}", f.id
    )


def _cutter(f: RFeature, current: Shape) -> Shape:
    """The tool for a hole: a cylinder running along -Z of the feature frame.

    A through hole is sized from the current solid's bounding-box diagonal, so
    it exits no matter how the part is oriented or how the ledger is edited
    later. Sizing it from a fixed number is how "through" holes quietly become
    blind ones after someone doubles a dimension.
    """
    r = f.p["diameter"] / 2
    if f.through:
        bb = current.bounding_box()
        span = bb.diagonal + 2 * EPS
    else:
        span = (f.depth or 0.0) + EPS
    # align=MAX puts the cutter's top at local z=0; lift by EPS so it starts
    # just proud of the face it is cutting from. The floor of a blind hole
    # therefore lands exactly at z = -depth, which is what `hole_bottom`
    # declares in provenance.py.
    tool = Cylinder(radius=r, height=span, align=(Align.CENTER, Align.CENTER, Align.MAX))
    return Pos(0, 0, EPS) * tool


def build_solid(plan: BuildPlan) -> Shape:
    """BuildPlan -> a single valid solid, or raise GeometryError."""
    solid: Shape | None = None

    for f in plan.features:
        loc: Location = feature_location(f)
        try:
            if f.kind == "hole":
                if solid is None:  # gated in plan.py; belt and braces
                    raise GeometryError(
                        GeometryErrorCode.INVALID_PARAMETER,
                        f"hole {f.id!r} has nothing to cut",
                        f.id,
                    )
                solid = solid - (loc * _cutter(f, solid))
            else:
                shape = loc * _primitive(f)
                if f.operation == "add":
                    solid = shape if solid is None else solid + shape
                else:
                    if solid is None:
                        raise GeometryError(
                            GeometryErrorCode.INVALID_PARAMETER,
                            f"{f.id!r} subtracts from nothing",
                            f.id,
                        )
                    solid = solid - shape
        except GeometryError:
            raise
        except Exception as exc:  # OCC failures are opaque; name the feature
            raise GeometryError(
                GeometryErrorCode.KERNEL_FAILURE,
                f"kernel failed while applying {f.kind} {f.id!r}",
                f.id,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

        if solid is None or solid.wrapped is None or not solid.solids():
            raise GeometryError(
                GeometryErrorCode.EMPTY_RESULT,
                f"the solid became empty after {f.kind} {f.id!r}",
                f.id,
                detail="a cut removed all remaining material",
            )

    assert solid is not None  # plan.py rejects an empty feature list

    if not solid.is_valid:
        raise GeometryError(
            GeometryErrorCode.KERNEL_FAILURE,
            "the resulting shape is not a valid solid",
            detail="OCC reported an invalid B-rep after all features were applied",
        )
    if solid.volume <= 0.0:
        raise GeometryError(GeometryErrorCode.EMPTY_RESULT, "the resulting solid has no volume")

    # More than one lump means the ledger describes disconnected bodies. Legal
    # geometry, but the frontend renders one part and the agent reasons about
    # one part, so flag it rather than silently shipping the surprise.
    lumps = len(solid.solids())
    if lumps > 1:
        plan.warnings.append(
            f"the part is in {lumps} disconnected pieces — features may be placed apart"
        )

    return solid
