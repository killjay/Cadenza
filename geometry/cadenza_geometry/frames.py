"""Placement -> build123d Location, in one place.

`builder.py` and `provenance.py` MUST agree about where a feature sits, or the
reverse lookup attributes faces to the wrong feature. They agree by both
calling this.
"""

from __future__ import annotations

from build123d import Location
from OCP.gp import gp_Dir, gp_Pnt

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
