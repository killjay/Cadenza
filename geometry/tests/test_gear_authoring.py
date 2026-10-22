import math
from time import perf_counter

import pytest
from build123d import import_step

from cadenza_contracts.ledger import Ledger
from cadenza_geometry.exporters import export_step_bytes


def source(module=1, teeth=24, height=5, extra=(), **settings):
    return Ledger.model_validate(dict(features=[dict(id="feat_gear", kind="gear", operation="add",
        parameters=dict(module=module, teeth=teeth, height=height, **settings),
        placement=dict(origin=[3, -2, 7], rotation_deg=[0, 0, 17])), *extra]))


def hole(z=12, x=3):
    return dict(id="feat_bore", kind="hole", operation="subtract", parameters=dict(diameter=4, through=True),
        placement=dict(origin=[x, -2, z]))


@pytest.mark.parametrize("module,teeth,height", [(1, 24, 5), (1.5, 32, 8), (2, 20, 10), (1, 25, 5), (1, 128, 5)])
def test_native_teeth_dimensions_transforms_and_step(svc, tmp_path, module, teeth, height):
    doc = source(module, teeth, height)
    start = perf_counter()
    result = svc.run_probe_query(doc, dict(query_type="gear_effects", feature_ids=["feat_gear"]))
    assert "error" not in result, result
    assert result["gears"][0]["tooth_count"] == teeth
    assert perf_counter()-start < 60
    solid = svc._compile(doc).solid
    path = tmp_path/'gear.step'
    path.write_bytes(export_step_bytes(solid))
    restored = import_step(path)
    assert restored.is_valid and len(restored.solids()) == 1
    assert restored.volume == pytest.approx(sum(s.volume for s in solid.solids()), rel=1e-6)
    assert [restored.bounding_box().min.Z, restored.bounding_box().max.Z] == pytest.approx([7, 7+height])
    assert math.pi*(module*(teeth/2-1.25))**2*height < restored.volume < math.pi*(module*(teeth/2+1))**2*height


def test_unchanged_bore_detects_cap_when_parent_grows(svc):
    good = svc.run_probe_query(source(extra=[hole()]), dict(query_type="gear_effects", feature_ids=["feat_gear"]))
    assert "error" not in good, good
    bad = svc.run_probe_query(source(height=8, extra=[hole()]), dict(query_type="gear_effects", feature_ids=["feat_gear"]))
    assert "opening" in bad["error"]
    fixed = svc.run_probe_query(source(height=8, extra=[hole(z=15)]), dict(query_type="gear_effects", feature_ids=["feat_gear"]))
    assert "error" not in fixed, fixed


def test_hub_bore_and_interior_pattern(svc):
    hub = dict(id="feat_hub", kind="cylinder", parameters=dict(diameter=10, height=3), placement=dict(origin=[3, -2, 12]))
    seed = dict(id="feat_seed", kind="hole", operation="subtract", parameters=dict(diameter=2, through=True), placement=dict(origin=[10, -2, 15]))
    pattern = dict(id="feat_pattern", kind="circular_pattern", operation="subtract", placement=dict(origin=[3, -2, 0]),
        parameters=dict(target_feature="feat_seed", count=4, radius=7, sweep_angle=360, axis=[0, 0, 1]))
    result = svc.run_probe_query(source(extra=[hub, hole(z=15), seed, pattern]), dict(query_type="gear_effects", feature_ids=["feat_gear"]))
    assert "error" not in result, result
    assert result["gears"][0]["complete_bores"] == ["feat_bore", "feat_seed"]


def test_tooth_cutting_bore_is_rejected(svc):
    result = svc.run_probe_query(source(extra=[hole(x=13)]), dict(query_type="gear_effects", feature_ids=["feat_gear"]))
    assert "error" in result


@pytest.mark.parametrize("depth,valid", [(2, True), (5, False), (7, False)])
def test_blind_bore_requires_actual_floor(svc, depth, valid):
    bore = hole()
    bore['parameters'] = dict(diameter=4, depth=depth, through=False)
    result = svc.run_probe_query(source(extra=[bore]), dict(query_type="gear_effects", feature_ids=["feat_gear"]))
    assert ("error" not in result) == valid, result


@pytest.mark.parametrize("settings", [dict(module=1000, teeth=128, height=1e6),
    dict(module=1, teeth=12, height=5, shift=0.5), dict(module=1, teeth=32, height=5, pressure_angle_deg=25)])
def test_upper_bounds_and_profile_variants(svc, settings):
    result = svc.run_probe_query(source(**settings), dict(query_type="gear_effects", feature_ids=["feat_gear"]))
    assert "error" not in result, result
