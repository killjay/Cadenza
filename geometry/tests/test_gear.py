"""Involute spur gears: the profile maths, then the solid it extrudes into.

The two halves of this file are deliberately different in character.

`cadenza_geometry.gear` is pure planar maths, so it can be checked against the
closed-form identities a gear has to satisfy — tooth thickness at the reference
circle, the pressure angle recovered from the flank, the base-circle tangency.
Those are the checks that catch a profile which is subtly wrong, which is the
dangerous kind: a flank a few hundredths too fat still extrudes into a
convincing-looking wheel that will not mesh with anything.

The kernel half can only ask coarser questions — is it one closed solid, is the
volume bracketed by the root and tip cylinders, did every face find its ledger
feature. That is fine, because by then the shape has already been pinned above.
"""

from __future__ import annotations

import math
from collections import Counter

import pytest

from conftest import feature, gear_with_bore, ledger

from cadenza_geometry import build
from cadenza_geometry.contracts import GeometryError, GeometryErrorCode
from cadenza_geometry.gear import (
    Arc,
    Line,
    Spline,
    flank_points,
    gear_geometry,
    gear_profile,
    involute_angle,
    minimum_teeth_without_undercut,
    roll_angle_at,
)
from cadenza_geometry.plan import resolve

# --------------------------------------------------------------------------- #
# the profile — closed-form invariants, no kernel
# --------------------------------------------------------------------------- #


def test_the_standard_radii_come_out_of_module_and_teeth():
    g = gear_geometry(module=2.0, teeth=20)
    assert g.reference_radius == pytest.approx(20.0)          # m*z/2
    assert g.base_radius == pytest.approx(20.0 * math.cos(math.radians(20)))
    assert g.tip_radius == pytest.approx(22.0)                # + one module
    assert g.root_radius == pytest.approx(17.5)               # - 1.25 modules
    assert g.outer_diameter == pytest.approx(44.0)            # m*(z+2)


def test_tooth_thickness_at_the_reference_circle_is_half_the_pitch():
    """Unshifted, tooth and gap are equal there — that IS the reference circle."""
    g = gear_geometry(module=2.0, teeth=20)
    thickness = 2 * g.half_tooth_angle * g.reference_radius
    circular_pitch = math.pi * g.module
    assert thickness == pytest.approx(circular_pitch / 2)


def test_profile_shift_fattens_the_tooth_by_the_textbook_amount():
    """s = m(pi/2 + 2x tan a). Getting the factor of 2 wrong here is invisible
    in the render and fatal to the fit."""
    shift = 0.4
    g = gear_geometry(module=2.0, teeth=20, shift=shift)
    thickness = 2 * g.half_tooth_angle * g.reference_radius
    expected = g.module * (math.pi / 2 + 2 * shift * math.tan(math.radians(20)))
    assert thickness == pytest.approx(expected)


@pytest.mark.parametrize("pressure_angle", [14.5, 20.0, 25.0])
def test_the_flank_really_is_an_involute_of_the_base_circle(pressure_angle):
    """The angle between the flank tangent and the radius vector at any radius
    is that radius's pressure angle, by the definition of the involute. At the
    reference circle it is the gear's nominal pressure angle."""
    g = gear_geometry(module=1.0, teeth=30, pressure_angle_deg=pressure_angle)
    pts = flank_points(g, count=400)

    # The two points bracketing the reference radius.
    radii = [math.hypot(x, y) for x, y in pts]
    i = min(range(len(radii)), key=lambda k: abs(radii[k] - g.reference_radius))
    a, b = pts[i - 1], pts[i + 1]

    tangent = (b[0] - a[0], b[1] - a[1])
    radial = pts[i]
    cos_angle = abs(
        tangent[0] * radial[0] + tangent[1] * radial[1]
    ) / (math.hypot(*tangent) * math.hypot(*radial))
    assert math.degrees(math.acos(cos_angle)) == pytest.approx(pressure_angle, abs=0.05)


def test_the_involute_function_matches_its_closed_form():
    """inv(a) = tan(a) - a, reached from the roll angle instead."""
    for radius in (10.5, 12.0, 15.0, 19.9):
        base = 10.0
        phi = roll_angle_at(base, radius)
        alpha = math.acos(base / radius)
        assert involute_angle(phi) == pytest.approx(math.tan(alpha) - alpha)


def test_the_flank_spans_exactly_root_or_base_to_tip():
    g = gear_geometry(module=2.0, teeth=20)
    pts = flank_points(g)
    assert math.hypot(*pts[0]) == pytest.approx(max(g.root_radius, g.base_radius))
    assert math.hypot(*pts[-1]) == pytest.approx(g.tip_radius)


def _endpoints(seg):
    if isinstance(seg, Spline):
        return seg.points[0], seg.points[-1]
    if isinstance(seg, Line):
        return seg.start, seg.end
    start = (seg.radius * math.cos(seg.start), seg.radius * math.sin(seg.start))
    end = (seg.radius * math.cos(seg.end), seg.radius * math.sin(seg.end))
    return start, end


@pytest.mark.parametrize("teeth", [8, 12, 17, 20, 60])
def test_the_profile_closes_into_a_single_loop(teeth):
    """Every segment ends where the next begins, all the way round. A gap here
    is what makes `Face(wire)` fail several stages downstream with nothing
    pointing back at the tooth that caused it."""
    g = gear_geometry(module=2.0, teeth=teeth)
    segments = gear_profile(g)
    for i, seg in enumerate(segments):
        _, end = _endpoints(seg)
        start, _ = _endpoints(segments[(i + 1) % len(segments)])
        assert math.dist(end, start) < 1e-9, f"segment {i} does not meet segment {i + 1}"


def test_every_profile_point_lies_between_the_root_and_tip_circles():
    g = gear_geometry(module=2.0, teeth=20)
    for seg in gear_profile(g):
        points = seg.points if isinstance(seg, Spline) else _endpoints(seg)
        for p in points:
            assert g.root_radius - 1e-9 <= math.hypot(*p) <= g.tip_radius + 1e-9


def test_root_relief_appears_exactly_when_the_root_circle_is_inside_the_base():
    """Four segments a tooth when the involute reaches the root unaided, six
    when it needs a radial drop to get there.

    The crossover is z = 2.5/(1-cos a) — about 41 teeth at 20 degrees, NOT the
    17 of the undercut limit. Conflating the two is the easy mistake here: they
    are different thresholds describing different things, and 20 teeth is on the
    relief side of one and the safe side of the other.
    """
    small = gear_geometry(module=2.0, teeth=20)
    assert small.needs_root_relief is True
    assert len(gear_profile(small)) == 20 * 6

    large = gear_geometry(module=2.0, teeth=42)
    assert large.needs_root_relief is False
    assert len(gear_profile(large)) == 42 * 4


def test_the_undercut_limit_is_the_textbook_17_and_is_not_the_relief_limit():
    assert minimum_teeth_without_undercut(20.0) == pytest.approx(17.097, abs=0.01)
    assert gear_geometry(module=1.0, teeth=17).undercuts is True
    assert gear_geometry(module=1.0, teeth=18).undercuts is False
    # Profile shift is how a real gear gets below the limit without undercut.
    assert gear_geometry(module=1.0, teeth=12, shift=0.5).undercuts is False
    # 24 teeth: past the undercut limit, still short of the relief limit.
    twenty_four = gear_geometry(module=1.0, teeth=24)
    assert twenty_four.undercuts is False
    assert twenty_four.needs_root_relief is True


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"module": 2.0, "teeth": 2}, "at least 3 teeth"),
        ({"module": 2.0, "teeth": 20, "pressure_angle_deg": 60.0}, "pressure angle"),
        ({"module": 2.0, "teeth": 6, "shift": 1.0}, "come to a point"),
        # Only a negative shift can pull the root circle through the axis: at
        # shift 0 the 3-teeth floor already keeps Rf positive.
        ({"module": 2.0, "teeth": 3, "shift": -0.5}, "root circle collapses"),
    ],
)
def test_unbuildable_gears_are_rejected_by_name(kwargs, expected):
    with pytest.raises(GeometryError) as exc:
        gear_geometry(feature_id="feat_g", **kwargs)
    assert expected in exc.value.message
    assert exc.value.code is GeometryErrorCode.INVALID_PARAMETER
    assert exc.value.feature_id == "feat_g"


# --------------------------------------------------------------------------- #
# the plan stage
# --------------------------------------------------------------------------- #


def test_a_fractional_tooth_count_is_refused():
    """`teeth` is a count. 20.5 is a typo or a unit confusion, and rounding it
    silently would ship a gear with the wrong ratio."""
    with pytest.raises(GeometryError) as exc:
        resolve(ledger(feature("feat_g", "gear", {"module": 2, "teeth": 20.5, "height": 5})))
    assert "whole number" in exc.value.message


def test_a_gear_below_the_undercut_limit_builds_but_warns():
    plan = resolve(ledger(feature("feat_g", "gear", {"module": 2, "teeth": 11, "height": 5})))
    assert plan.features[0].gear is not None
    assert any("undercut" in w for w in plan.warnings)


def test_a_normal_gear_warns_about_nothing():
    plan = resolve(ledger(feature("feat_g", "gear", {"module": 2, "teeth": 24, "height": 5})))
    assert plan.warnings == []


def test_the_planned_extent_uses_the_tip_circle_not_the_pitch_circle():
    """An envelope built from the pitch circle would report a hole through a
    tooth as missing the part."""
    plan = resolve(ledger(feature("feat_g", "gear", {"module": 2, "teeth": 20, "height": 5})))
    lo, hi = plan.features[0].local_extent()
    assert hi[0] == pytest.approx(22.0)
    assert lo == pytest.approx((-22.0, -22.0, 0.0))


# --------------------------------------------------------------------------- #
# the solid
# --------------------------------------------------------------------------- #


def test_a_gear_builds_into_one_valid_solid():
    result = build(gear_with_bore())
    assert result.volume > 0
    # Bracketed by the root and tip cylinders, minus the bore. Nothing tighter
    # is worth asserting: the exact volume is the profile's job, tested above.
    height, bore_r = 10.0, 4.0
    bore = math.pi * bore_r**2 * height
    assert math.pi * 17.5**2 * height - bore < result.volume < math.pi * 22.0**2 * height - bore
    assert result.bbox.max[0] == pytest.approx(22.0, abs=1e-4)
    assert result.bbox.max[2] == pytest.approx(10.0, abs=1e-4)


def test_gear_faces_are_all_traceable_to_the_ledger():
    """A click on a tooth has to land on the gear. Face attribution is analytic
    for planes and cylinders, but a flank is a spline surface matched by region
    — this is the test that says the region actually covers them."""
    result = build(gear_with_bore())
    labels = result.entities["feat_spur_gear_7c3d"]
    assert "tooth_flank" in labels
    assert "tooth_tip" in labels
    assert "tooth_root" in labels
    assert {"top_face", "bottom_face"} <= set(labels)
    assert result.entities["feat_axle_bore_4e1f"] == ["bore"]
    assert not any("could not be traced" in w for w in result.warnings), result.warnings


def test_a_click_on_a_tooth_flank_resolves_to_the_gear(svc):
    """Taken from the built solid rather than guessed, so the point is exactly
    on the surface — which is what the frontend's raycast delivers."""
    doc = gear_with_bore()
    built = svc._compile(doc)
    flank = next(
        f
        for i, f in enumerate(built.faces)
        if built.attribution.get(i) and built.attribution[i][1].label == "tooth_flank"
    )
    centre = flank.center()
    hit = svc.probe_point_legacy(doc, (centre.X, centre.Y, centre.Z))
    assert hit is not None
    assert hit["node_id"] == "feat_spur_gear_7c3d"
    assert hit["feature"] == "tooth_flank"
    assert hit["confidence"] == pytest.approx(1.0)


@pytest.mark.parametrize("teeth", [12, 20, 30])
def test_the_solid_has_exactly_as_many_teeth_as_asked_for(teeth, svc):
    """The most direct statement that this is a gear and not a wheel: count the
    tip faces on the finished B-rep. One per tooth, and one root face per gap.

    Every other assertion here is about the profile or the volume, and a profile
    that dropped or doubled a tooth would still satisfy most of them.
    """
    built = svc._compile(gear_with_bore(teeth=teeth, bore=4.0))
    labels = Counter(spec.label for _, spec in built.attribution.values())
    assert labels["tooth_tip"] == teeth
    assert labels["tooth_root"] == teeth
    # Two involute flanks a tooth, plus the two radial relief faces when the
    # root circle sits inside the base circle (true at every tooth count here).
    assert labels["tooth_flank"] == teeth * 4
    assert labels["top_face"] == 1 and labels["bottom_face"] == 1


def test_a_gear_without_root_relief_has_only_its_involute_flanks(svc):
    """Past the relief limit the radial faces are gone, so the flank count
    halves. Pins that `needs_root_relief` reaches the solid, not just the maths."""
    built = svc._compile(gear_with_bore(module=1.0, teeth=42, height=5.0, bore=10.0))
    labels = Counter(spec.label for _, spec in built.attribution.values())
    assert labels["tooth_tip"] == 42
    assert labels["tooth_flank"] == 42 * 2


def test_the_tip_cylinder_wins_over_the_flank_region_that_contains_it():
    """The tip surface sits inside the flank region's radius band, so both specs
    match it. The exact one has to win, or every tooth tip reads as a flank."""
    result = build(gear_with_bore())
    assert "tooth_tip" in result.entities["feat_spur_gear_7c3d"]


def test_more_teeth_at_the_same_module_makes_a_bigger_gear():
    """The edit the Machinist is told to prefer. It must change the size."""
    small = build(gear_with_bore(teeth=20))
    large = build(gear_with_bore(teeth=30))
    assert large.bbox.max[0] == pytest.approx(32.0, abs=1e-4)   # m*(z+2)/2
    assert large.volume > small.volume


def test_an_undercut_gear_still_builds_and_carries_its_warning():
    result = build(gear_with_bore(teeth=11, bore=4.0))
    assert result.volume > 0
    assert any("undercut" in w for w in result.warnings)


def test_a_gear_and_a_plate_compose():
    """Nothing about the gear branch is allowed to break the boolean chain."""
    doc = ledger(
        feature("feat_plate", "box", {"length": 60, "width": 60, "height": 5}, operation="add"),
        feature(
            "feat_gear",
            "gear",
            {"module": 1.5, "teeth": 20, "height": 8},
            origin=(0, 0, 5),
            operation="add",
        ),
        feature(
            "feat_bore",
            "hole",
            {"diameter": 6, "through": True},
            origin=(0, 0, 13),
            operation="subtract",
        ),
    )
    result = build(doc)
    assert result.bbox.max[2] == pytest.approx(13.0, abs=1e-4)
    assert "bore" in result.entities["feat_bore"]
    assert "tooth_flank" in result.entities["feat_gear"]
