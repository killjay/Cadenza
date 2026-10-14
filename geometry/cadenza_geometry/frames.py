"""Placement -> build123d Location, in one place.

`builder.py` and `provenance.py` MUST agree about where a feature sits, or the
reverse lookup attributes faces to the wrong feature. They agree by both
calling this.
"""

from __future__ import annotations

from math import radians
from build123d import Location, Vector
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf

from cadenza_geometry.plan import BuildPlan, RFeature


def feature_location(f: RFeature, plan: BuildPlan | None = None) -> Location:
    """The feature's local frame in world space. If plan is provided and the feature is relative_to another, their locations are multiplied."""
    loc = Location(tuple(f.origin), tuple(f.rotation_deg))
    if f.relative_to and plan:
        parent = plan.by_id(f.relative_to)
        if parent:
            parent_loc = feature_location(parent, plan)
            return parent_loc * loc
    return loc


def to_world(loc: Location, p: tuple[float, float, float]) -> tuple[float, float, float]:
    """Transform a LOCAL point into world space."""
    pnt = gp_Pnt(*p)
    pnt.Transform(loc.wrapped.Transformation())
    return (pnt.X(), pnt.Y(), pnt.Z())


def dir_to_world(loc: Location, d: tuple[float, float, float]) -> tuple[float, float, float]:
    """Transform a LOCAL direction into world space (rotation only)."""
    dr = gp_Dir(*d)
    dr.Transform(loc.wrapped.Transformation())
    return (dr.X(), dr.Y(), dr.Z())


def pattern_locations(f: RFeature, plan: BuildPlan) -> list[Location]:
    """Additional copies only; retain legacy resolved frames and sweep/count."""
    loc = feature_location(f, plan)
    count = int(f.p["count"])
    if f.kind == "linear_pattern":
        axis = Vector(dir_to_world(loc, f.p["axis"]))
        axis2 = Vector(dir_to_world(loc, f.p.get("axis_2", (0, 1, 0))))
        return [Location(axis * f.p["spacing"] * i + axis2 * f.p.get("spacing_2", 0) * j)
                for i in range(count) for j in range(int(f.p.get("count_2", 1))) if i or j]
    axis = gp_Ax1(gp_Pnt(*to_world(loc, (0, 0, 0))),
                  gp_Dir(*dir_to_world(loc, f.p.get("axis", (0, 0, 1)))))
    result = []
    for i in range(1, count):
        transform = gp_Trsf()
        transform.SetRotation(axis, radians(f.p["sweep_angle"] * i / count))
        result.append(Location(transform))
    return result
