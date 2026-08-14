"""Placement -> build123d Location, in one place.

`builder.py` and `provenance.py` MUST agree about where a feature sits, or the
reverse lookup attributes faces to the wrong feature. They agree by both
calling this.
"""

from __future__ import annotations

from build123d import Location
from OCP.gp import gp_Dir, gp_Pnt

from cadenza_geometry.plan import RFeature


def feature_location(f: RFeature) -> Location:
    """The feature's local frame in world space."""
    return Location(tuple(f.origin), tuple(f.rotation_deg))


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
