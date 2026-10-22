import math

import pytest
from build123d import import_step
from pydantic import ValidationError

from cadenza_contracts.ledger import Ledger
from cadenza_contracts.revolve import RevolveSettings, revolve_reason
from cadenza_geometry.builder import build_solid
from cadenza_geometry.plan import BuildPlan, resolve
from cadenza_geometry.exporters import export_step_bytes


def source(vertices, angle=360, origin=None, rotation=None):
    return Ledger.model_validate(dict(features=[dict(id="feat_turn", kind="revolve",
        parameters=dict(vertices=vertices, angle=angle, axis=[0, 1, 0]),
        placement=dict(origin=origin or [0, 0, 0], rotation_deg=rotation or [0, 0, 0]))]))


@pytest.mark.parametrize("r,R,y,h,angle", [(5, 10, 0, 20, 360), (10, 20, 5, 10, 180), (2, 7, -5, 12, 90)])
def test_explicit_profile_position_angle_and_step(r, R, y, h, angle, tmp_path):
    vertices = [[r, y], [R, y], [R, y+h], [r, y+h]]
    RevolveSettings(vertices=vertices, angle=angle)
    ledger = source(vertices, angle, [30, -12, 8], [90, 0, 0])
    assert revolve_reason(ledger.features[0].model_dump(mode="json")) is None
    plan = resolve(ledger)
    assert isinstance(plan, BuildPlan)
    shape = build_solid(plan)
    expected = math.pi * (R*R-r*r) * h * angle / 360
    assert shape.is_valid and len(shape.solids()) == 1
    assert sum(s.volume for s in shape.solids()) == pytest.approx(expected)
    box = shape.bounding_box()
    assert [box.min.Z, box.max.Z] == pytest.approx([8+y, 8+y+h])
    if angle == 360:
        assert [box.min.X, box.max.X, box.min.Y, box.max.Y] == pytest.approx([30-R, 30+R, -12-R, -12+R])
    path = tmp_path / "turn.step"; path.write_bytes(export_step_bytes(shape))
    restored = import_step(path)
    assert restored.is_valid and restored.volume == pytest.approx(expected)
    assert list(restored.bounding_box().min) == pytest.approx(list(box.min))
    assert list(restored.bounding_box().max) == pytest.approx(list(box.max))


def test_asymmetric_stepped_profile_is_not_recentered():
    vertices = [[0, 5], [10, 5], [10, 10], [5, 10], [5, 25], [0, 25]]
    RevolveSettings.model_validate(dict(vertices=vertices, angle=360))
    plan = resolve(source(vertices))
    assert isinstance(plan, BuildPlan)
    shape = build_solid(plan)
    assert sum(s.volume for s in shape.solids()) == pytest.approx(math.pi * (100*5 + 25*15))
    assert [shape.bounding_box().min.Y, shape.bounding_box().max.Y] == pytest.approx([5, 25])


@pytest.mark.parametrize("rotation", [[20, 30, 40], [180, 0, 90]])
def test_arbitrary_creation_rotation_has_independently_calculated_cylinder_bounds(rotation):
    origin, y, height, radius = [11, -7, 23], 5, 12, 10
    plan = resolve(source([[2, y], [radius, y], [radius, y+height], [2, y+height]],
        origin=origin, rotation=rotation))
    assert isinstance(plan, BuildPlan)
    solid = build_solid(plan)
    x, ry, z = map(math.radians, rotation)
    # Existing feature placements use intrinsic XYZ: Rx * Ry * Rz.
    axis = [-math.sin(z)*math.cos(ry), math.cos(x)*math.cos(z)-math.sin(x)*math.sin(ry)*math.sin(z),
            math.sin(x)*math.cos(z)+math.cos(x)*math.sin(ry)*math.sin(z)]
    center = [o + a*(y+height/2) for o, a in zip(origin, axis)]
    extent = [abs(a)*height/2 + radius*math.sqrt(max(0, 1-a*a)) for a in axis]
    box = solid.bounding_box()
    assert list(box.min) == pytest.approx([c-e for c, e in zip(center, extent)], abs=1e-5)
    assert list(box.max) == pytest.approx([c+e for c, e in zip(center, extent)], abs=1e-5)


@pytest.mark.parametrize("vertices", [
    [[-1, 0], [10, 0], [10, 10], [-1, 10]],
    [[0, 0], [10, 10], [0, 10], [10, 0]],
    [[0, 0], [5, 0], [10, 0]],
    [[0, 0], [10, 0], [10, 10], [0, 0]],
    [[True, 0], [10, 0], [10, 10]],
    [[0, 0, 1], [10, 0], [10, 10]],
    [[0, 0], [float("nan"), 0], [10, 10]],
])
def test_bad_profiles_rejected_before_native_build(vertices):
    with pytest.raises(ValidationError): RevolveSettings(vertices=vertices, angle=360)


@pytest.mark.parametrize("angle", [0, -10, 361, float("inf"), True])
def test_bad_angles(angle):
    with pytest.raises(ValidationError):
        RevolveSettings(vertices=[[0, 0], [10, 0], [10, 10]], angle=angle)


def test_legacy_axis_keeps_document_but_is_not_promoted():
    ledger = source([[5, 0], [10, 0], [10, 20], [5, 20]])
    feature = ledger.features[0]
    assert feature.kind == "revolve"
    feature.parameters.axis = [1, 0, 0]
    reason = revolve_reason(feature.model_dump(mode="json"))
    assert reason is not None and "legacy" in reason
    assert Ledger.model_validate_json(ledger.model_dump_json()) == ledger
