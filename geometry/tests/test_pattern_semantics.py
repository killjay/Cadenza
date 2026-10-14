"""Independent positions and service-level pattern compatibility checks."""
import math
from copy import deepcopy

import pytest
from build123d import GeomType
from cadenza_contracts.ledger import Ledger
from cadenza_geometry.builder import build_solid
from cadenza_geometry.plan import resolve, BuildPlan
from cadenza_geometry.service import Build123dGeometryService


def plate(kind="circular_pattern", count=4, radius=20):
    return {"features": [
        {"id": "feat_plate", "kind": "box", "parameters": {"length": 100, "width": 100, "height": 8}},
        {"id": "feat_seed", "kind": "hole", "parameters": {"diameter": 6, "through": True},
         "placement": {"origin": [20 if kind == "circular_pattern" else -30, 0, 8]}},
        {"id": "feat_pattern", "kind": kind, "operation": "subtract", "parameters": {
            "target_feature": "feat_seed", "count": count,
            **({"radius": radius} if kind == "circular_pattern" else {"spacing": 20})}}
    ]}


def bores(shape):
    return sorted((round(f.axis_of_rotation.position.X, 6), round(f.axis_of_rotation.position.Y, 6),
                   round(f.radius, 6)) for f in shape.faces() if f.geom_type == GeomType.CYLINDER)


def build_part(raw):
    plan = resolve(raw)
    assert isinstance(plan, BuildPlan)
    return build_solid(plan)


def volume(shape):
    return sum(s.volume for s in shape.solids())


@pytest.mark.parametrize("kind", ["linear_pattern", "circular_pattern"])
def test_full_service_pattern_attribution(kind):
    ledger = Ledger.model_validate(plate(kind))
    built = Build123dGeometryService()._compile(ledger)
    assert volume(built.solid) == pytest.approx(80000 - 4 * 9 * math.pi * 8)
    assert len(bores(built.solid)) == 4
    owners = [feature.id for _, feature, spec in built.attribution.values() if spec.label == "bore"]
    assert owners.count("feat_seed") == 1 and owners.count("feat_pattern") == 3


def test_legacy_radius_hint_does_not_reinterpret_saved_geometry():
    a, b = plate(radius=20), plate(radius=30)
    assert bores(build_part(a)) == bores(build_part(b))
    b["features"][1]["placement"]["origin"][0] = 30
    assert bores(build_part(a)) != bores(build_part(b))


@pytest.mark.parametrize("mode", ["partial", "axis", "rotated"])
def test_service_preserves_legacy_effective_transforms(mode):
    raw = plate()
    feature = raw["features"][-1]
    if mode == "partial": feature["parameters"]["sweep_angle"] = 180
    if mode == "axis": feature["parameters"]["axis"] = [1, 0, 0]
    if mode == "rotated": feature["placement"] = dict(origin=[0,0,0], rotation_deg=[0,0,30])
    expected = build_part(deepcopy(raw))
    built = Build123dGeometryService()._compile(Ledger.model_validate(raw))
    assert bores(built.solid) == bores(expected)
    assert volume(built.solid) == pytest.approx(volume(expected))


@pytest.mark.parametrize("kind", ["linear_pattern", "circular_pattern"])
@pytest.mark.parametrize("through", [True, False])
def test_effect_inspection_measures_complete_holes(kind, through):
    raw = plate(kind)
    raw["features"][1]["parameters"].update(through=through, depth=None if through else 3)
    result = Build123dGeometryService().run_probe_query(Ledger.model_validate(raw),
        {"query_type": "pattern_effects", "feature_ids": ["feat_pattern"]})
    assert "error" not in result, result
    effect = result["patterns"][0]
    assert effect["measured_complete_bores"] == 4
    assert effect["copy_effects_mm3"] == pytest.approx([math.pi * 9 * (8 if through else 3)] * 3)


@pytest.mark.parametrize("failure", ["outside", "overlap", "consumed", "blind_exits", "through_starts_inside", "through_capped_later", "blind_floor_pierced"])
def test_effect_inspection_rejects_missing_or_incomplete_holes(failure):
    raw = plate("linear_pattern")
    if failure == "outside": raw["features"][2]["parameters"]["spacing"] = 35
    if failure == "overlap": raw["features"][2]["parameters"]["spacing"] = 2
    if failure == "consumed": raw["features"].append({"id": "feat_fill", "kind": "box", "parameters": {"length": 100, "width": 100, "height": 8}})
    if failure == "blind_exits": raw["features"][1]["parameters"].update(through=False, depth=9)
    if failure == "through_starts_inside": raw["features"][1]["placement"]["origin"][2] = 4
    if failure == "through_capped_later": raw["features"].append(dict(id="feat_cap", kind="box",
        parameters=dict(length=100, width=100, height=1), placement=dict(origin=[0, 0, -1])))
    if failure == "blind_floor_pierced":
        raw["features"][1]["parameters"].update(through=False, depth=3)
        raw["features"].append(dict(id="feat_pierce", kind="hole", parameters=dict(diameter=2, through=True), placement=dict(origin=[-30, 0, 5])))
    result = Build123dGeometryService().run_probe_query(Ledger.model_validate(raw),
        {"query_type": "pattern_effects", "feature_ids": ["feat_pattern"]})
    assert "error" in result, result


@pytest.mark.parametrize("count,radius,center", [(3, 10, [0, 0]), (4, 20, [5, -5]), (8, 30, [-5, 5])])
def test_circular_oracles_exports_and_saved_seed_identity(count, radius, center, tmp_path):
    from build123d import import_step
    raw = plate(count=count, radius=radius)
    raw["features"][1]["placement"]["origin"] = [center[0] + radius, center[1], 8]
    raw["features"][-1]["placement"] = dict(origin=[*center, 0])
    ledger = Ledger.model_validate(raw)
    service = Build123dGeometryService()
    result = service.build(ledger)
    assert result.glb and result.step
    path = tmp_path / "pattern.step"
    path.write_bytes(result.step)
    solid = import_step(path)
    assert solid.volume == pytest.approx(80000 - count * 9 * math.pi * 8)
    actual = bores(solid)
    assert len(actual) == count
    for i in range(count):
        point = [center[0] + radius * math.cos(2 * math.pi * i / count), center[1] + radius * math.sin(2 * math.pi * i / count)]
        assert any(math.dist(point, p[:2]) < 1e-5 for p in actual)
    reference = service.run_probe_query(ledger, {"query_type": "capture_surface_reference",
        "point": [center[0] + radius + 3, center[1], 4], "normal": [-1, 0, 0]})
    assert reference["status"] == "resolved", reference
    assert reference["reference"]["feature_id"] == "feat_seed"
    raw["features"][-1]["parameters"]["count"] = 4 if count != 4 else 8
    changed = Ledger.model_validate({**raw, "project_id": ledger.project_id})
    resolved = service.run_probe_query(changed, {"query_type": "resolve_surface_reference", "reference": reference["reference"]})
    assert resolved["status"] == "resolved", resolved
    assert resolved["reference"]["feature_id"] == "feat_seed"
    copy = service.run_probe_query(ledger, {"query_type": "capture_surface_reference",
        "point": [center[0] + radius * math.cos(2 * math.pi / count) + 3,
                  center[1] + radius * math.sin(2 * math.pi / count), 4], "normal": [-1, 0, 0]})
    assert copy["status"] in ("unsupported", "unresolved", "missing", "ambiguous")


@pytest.mark.parametrize("size", [0.003, 0.01, 2])
def test_effect_tolerance_boundary(size):
    raw = {"features": [
        dict(id="feat_seed", kind="box", parameters=dict(length=size, width=size, height=size)),
        dict(id="feat_pattern", kind="linear_pattern", parameters=dict(target_feature="feat_seed", count=3, spacing=20))]}
    result = Build123dGeometryService().run_probe_query(Ledger.model_validate(raw),
        dict(query_type="pattern_effects", feature_ids=["feat_pattern"]))
    assert ("error" in result) == (size ** 3 <= 1e-7)


def test_maximum_count_is_verified_without_rounding():
    raw = plate(count=32)
    raw["features"][1]["parameters"]["diameter"] = 1
    result = Build123dGeometryService().run_probe_query(Ledger.model_validate(raw),
        dict(query_type="pattern_effects", feature_ids=["feat_pattern"]))
    assert "error" not in result, result
    assert result["patterns"][0]["measured_complete_bores"] == 32
    assert len(result["patterns"][0]["copy_effects_mm3"]) == 31


@pytest.mark.parametrize("axis", [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0]])
def test_hole_rows_in_all_promoted_directions(axis):
    raw = plate("linear_pattern", count=3)
    raw["features"][1]["placement"]["origin"] = [-20 * axis[0], -20 * axis[1], 8]
    raw["features"][-1]["parameters"]["axis"] = axis
    ledger = Ledger.model_validate(raw)
    service = Build123dGeometryService()
    measured = bores(service._compile(ledger).solid)
    expected = sorted(((i - 1) * 20 * axis[0], (i - 1) * 20 * axis[1], 3) for i in range(3))
    assert measured == expected
    assert "error" not in service.run_probe_query(ledger, dict(query_type="pattern_effects", feature_ids=["feat_pattern"]))


def test_legacy_embedded_pattern_service_preserves_geometry():
    raw = plate(radius=777)
    raw["features"][-1]["parameters"].update(sweep_angle=180, axis=[1, 0, 0])
    assembly = Ledger.model_validate({"assembly": {"parts": [dict(id="part_legacy", features=raw["features"])],
        "components": [dict(id="comp_left", part_id="part_legacy"),
                       dict(id="comp_right", part_id="part_legacy", placement=dict(origin=[140, 0, 0]))]}})
    service = Build123dGeometryService()
    result = service.build(assembly)
    assert result.glb and result.step
    assert volume(service._compile(assembly).solid) == pytest.approx(2 * volume(build_part(raw)))
