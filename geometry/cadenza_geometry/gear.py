"""Involute spur gear profile — planar maths only, no kernel, no OCC import.

A gear is not a primitive. It is a closed 2D outline that gets extruded, and
every bit of the gear-ness lives in that outline. FreeCAD works the same way
(`src/Mod/PartDesign/fcgear/involute.py`, and the FCGear workbench's `pygears`);
the difference between the two is only how they draw the curve — Bézier
approximation there, sampled points fitted to a spline here.

Keeping this file free of build123d is the point. The profile is the part of a
gear that is easy to get subtly, unnoticeably wrong: a flank that is a smidgen
too fat still extrudes into a plausible-looking wheel that will not mesh. Pure
functions over floats can be tested against the closed-form invariants — tooth
thickness at the reference circle, the base-circle tangency, the pressure angle
recovered from the flank — at microsecond cost and with no kernel in the way.

THE GEOMETRY
------------
    reference (pitch) radius   Rref = module * teeth / 2
    base radius                Rb   = Rref * cos(alpha)
    tip radius                 Ra   = Rref + module * (1 + shift)
    root radius                Rf   = Rref - module * (1 + clearance - shift)

The involute is the curve traced by unwinding a taut string from the base
circle. At roll angle `phi` (radians of string unwound) it is at

    radius = Rb * sqrt(1 + phi^2)
    angle  = phi - atan(phi)            <- the involute function, inv(alpha)

Sampling in `phi` rather than in radius is deliberate: near the base circle the
involute is almost radial, so uniform steps in radius bunch the points where the
curve is straight and starve the region where it bends.

WHAT THIS DOES NOT MODEL
------------------------
Two DIFFERENT thresholds live near the tooth root, and they are easy to conflate
because both are quoted as "small gears are a problem". They are not the same
number and they are not the same phenomenon.

  1. ROOT RELIEF, below Rf >= Rb — i.e. z < 2.5 / (1 - cos alpha), which is
     about 41 teeth at 20 degrees, so it applies to MOST gears. The involute
     starts at the base circle and simply cannot reach a root circle inside it.
     A real gear fills that band with the trochoid swept by the hob tip; this
     profile drops a radial line instead. Cosmetically and structurally close,
     but it is not the same curve.

  2. UNDERCUT, below z = 2 (1 - x) / sin^2(alpha), about 17 teeth at 20 degrees.
     Here the hob does not merely fill the band below the base circle, it cuts
     INTO the working flank above it, thinning the tooth. This profile does not
     model that at all.

Both simplifications leave MORE material at the root than a cut gear would have,
so a modelled tooth is stronger than its real counterpart, never weaker. Case 2
is the one worth telling the user about — it changes how the gear meshes, not
just how its root is shaped — and `undercut_warning()` says so in their words
rather than leaving it to be discovered on a test print.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cadenza_geometry.contracts import GeometryError, GeometryErrorCode

# Standard proportions for a full-depth involute tooth. These are the ISO 53 /
# DIN 867 basic rack values, and they are what "a 2 module gear" means when
# nobody says otherwise.
DEFAULT_PRESSURE_ANGLE = 20.0   # degrees
DEFAULT_CLEARANCE = 0.25        # x module, the gap under the mating tip
ADDENDUM_COEFF = 1.0            # x module, reference circle -> tip

# Points sampled along one flank. 24 holds a Ø40 gear's flank to well under the
# 0.05 mm tessellation deflection the GLB exporter uses, so the spline fitted
# through them is not the limiting error in what the user sees.
FLANK_POINTS = 24

# Below this the flanks would cross before reaching the tip: the tooth comes to
# a point and the tip arc inverts. Real gears solve it with profile shift.
POINTED_TOOTH_TOL = 1e-6


def involute_angle(phi: float) -> float:
    """inv(alpha) = tan(alpha) - alpha, parameterised by roll angle `phi`.

    `phi` IS tan(alpha) — the unwound string length over the base radius — so
    the classical involute function is just `phi - atan(phi)`, with none of the
    cancellation that `sqrt(R^2 - Rb^2)/Rb - acos(Rb/R)` suffers as R -> Rb.
    """
    return phi - math.atan(phi)


def roll_angle_at(base_radius: float, radius: float) -> float:
    """The roll angle `phi` at which the involute reaches `radius`.

    Clamped at the base circle: the involute does not exist inside it, and
    callers legitimately ask about radii below it (a root circle under the base
    circle is the undercut case).
    """
    if radius <= base_radius:
        return 0.0
    ratio = radius / base_radius
    return math.sqrt(ratio * ratio - 1.0)


def minimum_teeth_without_undercut(pressure_angle_deg: float, shift: float = 0.0) -> float:
    """Below this tooth count a generating hob would undercut the root.

    z_min = 2 (1 - x) / sin^2(alpha) — 17.1 at the standard 20 degrees, which is
    why 17 is the number everyone quotes.
    """
    sin_a = math.sin(math.radians(pressure_angle_deg))
    if sin_a <= 1e-12:
        return math.inf
    return 2.0 * (1.0 - shift) / (sin_a * sin_a)


@dataclass(frozen=True)
class GearGeometry:
    """Every radius and angle the profile needs, derived once.

    Constructed through `gear_geometry()` so the derived values cannot drift
    from the inputs, and so the failure cases are raised where they can name the
    parameter at fault.
    """

    module: float
    teeth: int
    pressure_angle_deg: float
    shift: float
    clearance: float

    reference_radius: float
    base_radius: float
    tip_radius: float
    root_radius: float

    #: Half the angular tooth thickness at the reference circle, radians.
    half_tooth_angle: float
    #: Angle between the centrelines of adjacent teeth, radians.
    angular_pitch: float

    @property
    def outer_diameter(self) -> float:
        return 2.0 * self.tip_radius

    @property
    def undercuts(self) -> bool:
        """Would a generating hob cut into the working flank? (~17 teeth at 20°)"""
        return self.teeth < minimum_teeth_without_undercut(self.pressure_angle_deg, self.shift)

    @property
    def needs_root_relief(self) -> bool:
        """Is the root circle inside the base circle? (~41 teeth at 20°)

        A different question from `undercuts`, and true far more often — see the
        module docstring. When it is, the profile carries a radial segment
        between the root and the start of the involute.
        """
        return self.root_radius < self.base_radius - 1e-9


def gear_geometry(
    module: float,
    teeth: int,
    pressure_angle_deg: float = DEFAULT_PRESSURE_ANGLE,
    shift: float = 0.0,
    clearance: float = DEFAULT_CLEARANCE,
    feature_id: str | None = None,
) -> GearGeometry:
    """Parameters -> every derived radius, with the unbuildable cases named."""
    if teeth < 3:
        raise GeometryError(
            GeometryErrorCode.INVALID_PARAMETER,
            f"a gear needs at least 3 teeth, got {teeth}",
            feature_id,
        )
    if module <= 0.0:
        raise GeometryError(
            GeometryErrorCode.INVALID_PARAMETER,
            f"module must be > 0, got {module:g}",
            feature_id,
        )
    if not 5.0 <= pressure_angle_deg <= 45.0:
        raise GeometryError(
            GeometryErrorCode.INVALID_PARAMETER,
            f"pressure angle must be between 5 and 45 degrees, got {pressure_angle_deg:g}",
            feature_id,
            detail="20 degrees is the standard value for almost every application",
        )

    alpha = math.radians(pressure_angle_deg)
    reference_radius = module * teeth / 2.0
    base_radius = reference_radius * math.cos(alpha)
    tip_radius = reference_radius + module * (ADDENDUM_COEFF + shift)
    root_radius = reference_radius - module * (ADDENDUM_COEFF + clearance - shift)

    if root_radius <= 0.0:
        raise GeometryError(
            GeometryErrorCode.INVALID_PARAMETER,
            f"the root circle collapses to nothing (module {module:g} is too "
            f"large for {teeth} teeth)",
            feature_id,
            detail="increase the tooth count or reduce the module",
        )

    # Circular tooth thickness at the reference circle is s = m(pi/2 + 2x tan a);
    # as a half-angle that is (pi/2 + 2x tan a) / z.
    half_tooth_angle = (math.pi / 2.0 + 2.0 * shift * math.tan(alpha)) / teeth
    angular_pitch = 2.0 * math.pi / teeth

    geom = GearGeometry(
        module=module,
        teeth=teeth,
        pressure_angle_deg=pressure_angle_deg,
        shift=shift,
        clearance=clearance,
        reference_radius=reference_radius,
        base_radius=base_radius,
        tip_radius=tip_radius,
        root_radius=root_radius,
        half_tooth_angle=half_tooth_angle,
        angular_pitch=angular_pitch,
    )

    if flank_angle_at(geom, tip_radius) <= POINTED_TOOTH_TOL:
        raise GeometryError(
            GeometryErrorCode.INVALID_PARAMETER,
            f"the teeth come to a point before reaching the tip circle "
            f"({teeth} teeth at {pressure_angle_deg:g} degrees with shift {shift:g})",
            feature_id,
            detail="raise the tooth count, or reduce the profile shift",
        )

    # The other end of the same failure: teeth fat enough at the root that the
    # gap between them closes. The profile would still be a closed loop, but a
    # self-intersecting one, and OCC reports that as an opaque kernel failure
    # several stages downstream of the parameter that caused it.
    root_gap = angular_pitch - 2.0 * flank_angle_at(geom, max(root_radius, base_radius))
    if root_gap <= POINTED_TOOTH_TOL:
        raise GeometryError(
            GeometryErrorCode.INVALID_PARAMETER,
            f"the teeth touch at the root, leaving no gap between them "
            f"({teeth} teeth with shift {shift:g})",
            feature_id,
            detail="reduce the profile shift, or raise the tooth count",
        )
    return geom


def flank_angle_at(geom: GearGeometry, radius: float) -> float:
    """Angle from the tooth centreline to the flank, at `radius`. Radians.

    Positive on one flank, negated on the other. It shrinks as the radius grows
    — that shrinking IS the taper of the tooth — and reaching zero means the
    flanks have met, which `gear_geometry` rejects as a pointed tooth.
    """
    inv_ref = involute_angle(roll_angle_at(geom.base_radius, geom.reference_radius))
    inv_r = involute_angle(roll_angle_at(geom.base_radius, radius))
    return geom.half_tooth_angle + inv_ref - inv_r


def flank_points(
    geom: GearGeometry,
    *,
    mirrored: bool = False,
    count: int = FLANK_POINTS,
) -> list[tuple[float, float]]:
    """One flank of the tooth centred on angle 0, root end first.

    Sampled uniformly in roll angle between where the involute leaves the root
    (or the base circle, whichever is larger) and the tip.
    """
    start_radius = max(geom.root_radius, geom.base_radius)
    phi_start = roll_angle_at(geom.base_radius, start_radius)
    phi_end = roll_angle_at(geom.base_radius, geom.tip_radius)

    sign = 1.0 if mirrored else -1.0
    inv_ref = involute_angle(roll_angle_at(geom.base_radius, geom.reference_radius))
    base_angle = geom.half_tooth_angle + inv_ref

    pts: list[tuple[float, float]] = []
    for i in range(count):
        phi = phi_start + (phi_end - phi_start) * i / (count - 1)
        radius = geom.base_radius * math.sqrt(1.0 + phi * phi)
        theta = sign * (base_angle - involute_angle(phi))
        pts.append((radius * math.cos(theta), radius * math.sin(theta)))
    return pts


def polar(radius: float, angle: float) -> tuple[float, float]:
    return (radius * math.cos(angle), radius * math.sin(angle))


@dataclass(frozen=True)
class Arc:
    """An arc centred on the gear axis, swept from `start` to `end` (radians)."""

    radius: float
    start: float
    end: float

    @property
    def midpoint(self) -> tuple[float, float]:
        return polar(self.radius, (self.start + self.end) / 2.0)


@dataclass(frozen=True)
class Spline:
    """A flank, as points to fit a curve through."""

    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class Line:
    start: tuple[float, float]
    end: tuple[float, float]


Segment = Arc | Spline | Line


def tooth_segments(geom: GearGeometry, centre_angle: float) -> list[Segment]:
    """One tooth plus the root gap that follows it, counter-clockwise.

    Ordered so that consecutive teeth join end-to-start into a closed loop:
    up the trailing flank, across the tip, down the leading flank, then the root
    arc across to the next tooth.
    """
    start_radius = max(geom.root_radius, geom.base_radius)
    angle_at_start = flank_angle_at(geom, start_radius)
    angle_at_tip = flank_angle_at(geom, geom.tip_radius)

    def rotate(p: tuple[float, float]) -> tuple[float, float]:
        c, s = math.cos(centre_angle), math.sin(centre_angle)
        return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)

    segments: list[Segment] = []

    # Below the base circle the involute does not exist. A radial relief drops
    # to the root circle; see the module docstring on what that costs.
    needs_relief = geom.needs_root_relief
    if needs_relief:
        segments.append(
            Line(
                polar(geom.root_radius, centre_angle - angle_at_start),
                polar(start_radius, centre_angle - angle_at_start),
            )
        )

    segments.append(Spline(tuple(rotate(p) for p in flank_points(geom))))
    segments.append(
        Arc(geom.tip_radius, centre_angle - angle_at_tip, centre_angle + angle_at_tip)
    )
    segments.append(
        Spline(tuple(rotate(p) for p in reversed(flank_points(geom, mirrored=True))))
    )

    if needs_relief:
        segments.append(
            Line(
                polar(start_radius, centre_angle + angle_at_start),
                polar(geom.root_radius, centre_angle + angle_at_start),
            )
        )

    # Across the gap to the next tooth's leading edge.
    segments.append(
        Arc(
            geom.root_radius,
            centre_angle + angle_at_start,
            centre_angle + geom.angular_pitch - angle_at_start,
        )
    )
    return segments


def gear_profile(geom: GearGeometry) -> list[Segment]:
    """The complete closed outline, counter-clockwise from tooth 0.

    Tooth 0 is centred on +X so that a gear and a cylinder of the same nominal
    size sit in the same place, and so a rotation in `placement.rotation_deg`
    means what a user would expect it to.
    """
    segments: list[Segment] = []
    for i in range(geom.teeth):
        segments.extend(tooth_segments(geom, i * geom.angular_pitch))
    return segments


def undercut_warning(geom: GearGeometry, feature_id: str) -> str | None:
    """The sentence the user sees when the tooth count is below the hob limit."""
    if not geom.undercuts:
        return None
    limit = minimum_teeth_without_undercut(geom.pressure_angle_deg, geom.shift)
    return (
        f"gear {feature_id!r} has {geom.teeth} teeth, below the {limit:.0f} at which "
        f"a {geom.pressure_angle_deg:g}° cutter starts to undercut the root — the "
        "modelled teeth are slightly fuller at the root than a cut gear's would be"
    )
