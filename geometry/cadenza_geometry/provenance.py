"""Face -> ledger feature attribution, WITHOUT topological identity.

The obvious way to answer "which ledger node made this face?" is to build
incrementally and diff the face sets. That way is the topological naming
problem wearing a hat: booleans split, merge and reorder faces, so the diff is
only stable until the model changes -- which is precisely when you need it.

This module does it analytically instead. Every feature kind declares the
SURFACES it is capable of generating -- a box declares six planes, a hole
declares one cylinder (plus a floor when blind) -- expressed in the feature's
local frame and pushed into world space through the same `Location` the builder
uses. Attribution is then a geometric question asked of the FINAL solid only:
which declared surface does this face lie on?

Nothing is carried across a rebuild, so nothing can go stale. Two boxes at the
same place are still ambiguous, but that ambiguity is real and is reported
(`confidence`) rather than hidden.

The discriminator that makes this work is the OUTWARD NORMAL. A boss and a bore
of the same radius on the same axis declare identical cylinders; they differ
only in which side the material is on. OCC orients a solid's face normals
outward, so:
    boss lateral face at radius r -> normal points AWAY from the axis
    bore                          -> normal points TOWARD the axis
and the same signed test separates a box's top face from the coincident bottom
face of a box stacked on it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Literal

from build123d import Face, GeomType

from cadenza_geometry.frames import dir_to_world, feature_location, to_world
from cadenza_geometry.plan import BuildPlan, RFeature

# Tolerances. Generous relative to OCC's 1e-7 modelling tolerance, tight
# relative to any dimension a user would type.
DIST_TOL = 1e-4      # mm, "does this face lie on that surface"
ANG_TOL = 1e-3       # 1 - |dot|, "are these directions parallel"
RADIUS_TOL = 1e-4    # mm


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a) -> float:
    return math.sqrt(_dot(a, a))


def _unit(a):
    n = _norm(a)
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-12 else (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class PlaneSpec:
    """A plane a feature can generate. `normal` points OUT of the solid."""

    label: str
    point: tuple[float, float, float]
    normal: tuple[float, float, float]
    kind: Literal["plane"] = "plane"


@dataclass(frozen=True)
class CylinderSpec:
    """A cylindrical surface a feature can generate.

    `material_inside` is True for a boss (material within the radius, normals
    point outward) and False for a bore (material outside, normals point in).
    """

    label: str
    axis_point: tuple[float, float, float]
    axis_dir: tuple[float, float, float]
    radius: float
    material_inside: bool
    kind: Literal["cylinder"] = "cylinder"


@dataclass(frozen=True)
class FlankSpec:
    """The tooth flanks of a gear — the one surface here that is not analytic.

    A flank is an extruded involute: a B-spline surface, so neither of the two
    specs above can name it, and there is no closed-form "does this face lie on
    that surface" test to write. It is also the face a user is most likely to
    click on a gear, so leaving all of them unattributed was not an option.

    So this one is a REGION rather than a surface: the annulus between the root
    and tip circles, over the gear's own height, excluding faces whose normal
    runs along the axis (those are the top and bottom). Weaker than an exact
    match, and deliberately scored by distance from the reference circle so that
    the tip and root cylinders — which also fall inside the band — are still won
    by their exact `CylinderSpec`. Two gears sharing a region come back
    ambiguous, which is the honest answer.
    """

    label: str
    axis_point: tuple[float, float, float]
    axis_dir: tuple[float, float, float]
    inner_radius: float
    outer_radius: float
    reference_radius: float
    height: float
    kind: Literal["flank"] = "flank"


Spec = PlaneSpec | CylinderSpec | FlankSpec


def surfaces_for(f: RFeature) -> list[Spec]:
    """The surfaces this feature is capable of contributing to the final solid.

    Declared in the feature's local frame, returned in world space. A surface
    listed here may or may not survive into the final solid -- a boss can be
    entirely swallowed by a later cut. Declaring is cheap; only the ones that
    match a real face are ever reported.
    """
    loc = feature_location(f)
    P = lambda p: to_world(loc, p)  # noqa: E731
    D = lambda d: dir_to_world(loc, d)  # noqa: E731
    out: list[Spec] = []

    if f.kind == "box":
        length, width, height = f.p["length"], f.p["width"], f.p["height"]
        hl, hw = length / 2, width / 2
        # Labels are the semantic vocabulary the agent layer sees. They are
        # frame-relative ("+X side"), not view-relative ("right"), because the
        # camera is not part of the ledger.
        out += [
            PlaneSpec("top_face", P((0, 0, height)), D((0, 0, 1))),
            PlaneSpec("bottom_face", P((0, 0, 0)), D((0, 0, -1))),
            PlaneSpec("side_x_pos", P((hl, 0, height / 2)), D((1, 0, 0))),
            PlaneSpec("side_x_neg", P((-hl, 0, height / 2)), D((-1, 0, 0))),
            PlaneSpec("side_y_pos", P((0, hw, height / 2)), D((0, 1, 0))),
            PlaneSpec("side_y_neg", P((0, -hw, height / 2)), D((0, -1, 0))),
        ]

    elif f.kind == "cylinder":
        r, height = f.p["diameter"] / 2, f.p["height"]
        out += [
            PlaneSpec("top_face", P((0, 0, height)), D((0, 0, 1))),
            PlaneSpec("bottom_face", P((0, 0, 0)), D((0, 0, -1))),
            CylinderSpec("lateral_face", P((0, 0, 0)), D((0, 0, 1)), r, material_inside=True),
        ]

    elif f.kind == "gear":
        assert f.gear is not None  # plan.py resolves this for every gear
        g = f.gear
        height = f.p["height"]
        # A subtracted gear is a gear-shaped pocket: same surfaces, material on
        # the other side of every one of them.
        solid_side = f.operation == "add"
        out += [
            PlaneSpec("top_face", P((0, 0, height)), D((0, 0, 1 if solid_side else -1))),
            PlaneSpec("bottom_face", P((0, 0, 0)), D((0, 0, -1 if solid_side else 1))),
            CylinderSpec(
                "tooth_tip", P((0, 0, 0)), D((0, 0, 1)), g.tip_radius, material_inside=solid_side
            ),
            CylinderSpec(
                "tooth_root", P((0, 0, 0)), D((0, 0, 1)), g.root_radius, material_inside=solid_side
            ),
            FlankSpec(
                "tooth_flank",
                P((0, 0, 0)),
                D((0, 0, 1)),
                inner_radius=g.root_radius,
                outer_radius=g.tip_radius,
                reference_radius=g.reference_radius,
                height=height,
            ),
        ]

    elif f.kind == "hole":
        r = f.p["diameter"] / 2
        out.append(CylinderSpec("bore", P((0, 0, 0)), D((0, 0, 1)), r, material_inside=False))
        if not f.through and f.depth is not None:
            # The floor of a blind hole. Material is ABOVE it, so the outward
            # normal points down (-Z of the feature frame).
            out.append(PlaneSpec("hole_bottom", P((0, 0, -f.depth)), D((0, 0, -1))))

    return out


@dataclass
class FaceProbe:
    """The geometric facts about one face of the final solid."""

    index: int
    geom: str                                   # "plane" | "cylinder" | other
    sample: tuple[float, float, float]          # a point ON the face
    normal: tuple[float, float, float]          # outward at `sample`
    axis_point: tuple[float, float, float] | None = None
    axis_dir: tuple[float, float, float] | None = None
    radius: float | None = None


def probe_face(face: Face, index: int) -> FaceProbe:
    """Extract the analytic facts we match against. Never raises."""
    gt = face.geom_type
    # `center()` is the area centroid: on the SURFACE for the trimmed patches we
    # produce, and always on the underlying plane even for an annulus, which is
    # what the plane test needs.
    c = face.center()
    sample = (c.X, c.Y, c.Z)
    try:
        n = face.normal_at(c)
        normal = (n.X, n.Y, n.Z)
    except Exception:
        normal = (0.0, 0.0, 0.0)

    if gt == GeomType.CYLINDER:
        ax = face.axis_of_rotation
        ap = (ax.position.X, ax.position.Y, ax.position.Z)
        ad = (ax.direction.X, ax.direction.Y, ax.direction.Z)
        # A cylindrical patch's centroid lies ON the surface, so a normal there
        # is meaningful and its sign tells us which side the material is on.
        return FaceProbe(index, "cylinder", sample, normal, ap, ad, float(face.radius))

    if gt == GeomType.PLANE:
        return FaceProbe(index, "plane", sample, normal)

    return FaceProbe(index, str(gt), sample, normal)


def _plane_score(fp: FaceProbe, s: PlaneSpec) -> float | None:
    """Lower is better. None = not a match."""
    if fp.geom != "plane":
        return None
    # Signed, not absolute: a coincident face belonging to a DIFFERENT feature
    # (the underside of a stacked box) has the opposite outward normal, and
    # rejecting it here is what keeps stacked solids attributable.
    align = _dot(fp.normal, s.normal)
    if align < 1.0 - ANG_TOL:
        return None
    off = abs(_dot(_sub(fp.sample, s.point), s.normal))
    if off > DIST_TOL:
        return None
    return off


def _cylinder_score(fp: FaceProbe, s: CylinderSpec) -> float | None:
    if fp.geom != "cylinder" or fp.radius is None or fp.axis_dir is None:
        return None
    if abs(fp.radius - s.radius) > RADIUS_TOL:
        return None
    if 1.0 - abs(_dot(_unit(fp.axis_dir), _unit(s.axis_dir))) > ANG_TOL:
        return None
    # Distance between the two axis lines (parallel, so: reject the component
    # along the axis and measure what is left).
    delta = _sub(fp.axis_point or (0, 0, 0), s.axis_point)
    along = _dot(delta, _unit(s.axis_dir))
    perp = _sub(delta, tuple(along * c for c in _unit(s.axis_dir)))
    axis_off = _norm(perp)
    if axis_off > DIST_TOL:
        return None
    # The material-side test. Radial direction from the axis to the sample
    # point; a boss's normal agrees with it, a bore's opposes it.
    radial = _sub(fp.sample, s.axis_point)
    radial = _sub(radial, tuple(_dot(radial, _unit(s.axis_dir)) * c for c in _unit(s.axis_dir)))
    if _norm(radial) > 1e-9:
        agree = _dot(_unit(radial), _unit(fp.normal))
        if s.material_inside and agree < 0.5:
            return None
        if not s.material_inside and agree > -0.5:
            return None
    return axis_off + abs(fp.radius - s.radius)


def _flank_score(fp: FaceProbe, s: FlankSpec) -> float | None:
    axis = _unit(s.axis_dir)
    # Top and bottom faces run across the axis; the teeth run along it.
    if 1.0 - abs(_dot(_unit(fp.normal), axis)) < ANG_TOL:
        return None

    delta = _sub(fp.sample, s.axis_point)
    axial = _dot(delta, axis)
    if axial < -DIST_TOL or axial > s.height + DIST_TOL:
        return None

    radial = _norm(_sub(delta, tuple(axial * c for c in axis)))
    if radial < s.inner_radius - DIST_TOL or radial > s.outer_radius + DIST_TOL:
        return None

    # Distance from the reference circle, which the flanks straddle. This is
    # what keeps the tip and root cylinders — inside the band, but a whole
    # addendum away from the reference circle — with their exact CylinderSpec.
    return abs(radial - s.reference_radius)


def attribute(
    plan: BuildPlan, faces: Iterable[Face]
) -> tuple[dict[int, tuple[RFeature, Spec]], dict[int, list[str]]]:
    """Map face index -> (feature, surface) for every attributable face.

    Returns (attribution, ambiguity) where ambiguity[i] lists the OTHER feature
    ids that matched face i equally well. A non-empty entry is a genuine
    modelling ambiguity (two features declaring the same surface), and the
    spatial layer downgrades `confidence` when it reports such a face.
    """
    attribution: dict[int, tuple[RFeature, Spec]] = {}
    ambiguity: dict[int, list[str]] = {}

    specs: list[tuple[RFeature, Spec]] = []
    for f in plan.features:
        for s in surfaces_for(f):
            specs.append((f, s))

    for i, face in enumerate(faces):
        fp = probe_face(face, i)
        best: tuple[float, RFeature, Spec] | None = None
        matches: list[tuple[float, RFeature, Spec]] = []
        for feat, spec in specs:
            if isinstance(spec, PlaneSpec):
                score = _plane_score(fp, spec)
            elif isinstance(spec, CylinderSpec):
                score = _cylinder_score(fp, spec)
            else:
                score = _flank_score(fp, spec)
            if score is None:
                continue
            matches.append((score, feat, spec))
            # Ties break toward the LATER feature: when two features genuinely
            # declare the same surface, the one applied last is the one whose
            # edit the user will expect to move it.
            if best is None or score < best[0] - 1e-12 or (
                abs(score - best[0]) <= 1e-12 and feat.index > best[1].index
            ):
                best = (score, feat, spec)

        if best is not None:
            attribution[i] = (best[1], best[2])
            others = sorted({m[1].id for m in matches} - {best[1].id})
            if others:
                ambiguity[i] = others

    return attribution, ambiguity
