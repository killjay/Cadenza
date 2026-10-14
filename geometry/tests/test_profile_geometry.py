"""Exact geometry and legacy compatibility for the constrained profile workflow."""
import copy
import json
import math
from pathlib import Path

import pytest
from build123d import export_step, import_step

from cadenza_geometry.builder import build_solid
from cadenza_geometry.contracts import GeometryError
from cadenza_geometry.plan import resolve, BuildPlan


@pytest.fixture
def raw():
    return json.loads((Path(__file__).parents[2] / "shared/contract_fixtures/profile_plate.json").read_text())


def solid(raw):
    plan = resolve(raw)
    assert isinstance(plan, BuildPlan)
    return build_solid(plan)


def volume(shape):
    return sum(body.volume for body in shape.solids())


def test_profile_extrusion_volume_bore_and_step_roundtrip(raw, tmp_path):
    shape = solid(raw)
    assert volume(shape) == pytest.approx((100 * 60 - math.pi * 10**2) * 8)
    assert len(shape.solids()) == 1 and len(shape.faces()) == 7
    assert shape.is_valid
    output = tmp_path / "profile.step"
    export_step(shape, output)
    reopened = import_step(output)
    assert reopened.is_valid and volume(reopened) == pytest.approx(volume(shape))
    assert len(reopened.faces()) == 7


def test_profile_plane_placement_and_normal_are_inherited(raw):
    raw["features"][0]["placement"] = {"origin": [10, 20, 30], "rotation_deg": [90, 0, 0]}
    shape = solid(raw)
    box = shape.bounding_box()
    assert (box.size.X, box.size.Y, box.size.Z) == pytest.approx((100, 8, 60))
    assert (box.min.X, box.min.Y, box.min.Z) == pytest.approx((-40, 12, 0))
    assert volume(shape) == pytest.approx((100 * 60 - math.pi * 10**2) * 8)


def test_circle_profile_subtractive_extrusion(raw):
    profile = raw["features"][0]
    profile["parameters"] = {"entities": [{"id": "ent_disk", "kind": "circle", "diameter": 20}],
        "constraints": [{"id": f"con_{parameter}", "kind": "dimension", "entity_id": "ent_disk", "parameter": parameter, "value": value}
                        for parameter, value in [("x", 0), ("y", 0), ("diameter", 20)]]}
    raw.pop("relationships")
    raw["features"][1]["operation"] = "subtract"
    raw["features"].insert(0, {"id": "feat_stock", "kind": "box", "operation": "add", "parameters": {"length": 100, "width": 60, "height": 8}})
    assert volume(solid(raw)) == pytest.approx((100 * 60 - math.pi * 10**2) * 8)


@pytest.mark.parametrize("problem", ["under", "conflict", "opening", "twist", "placement", "missing", "suppressed"])
def test_bad_profiles_or_consumers_fail_before_acceptance(raw, problem):
    raw.pop("relationships")
    constraints = raw["features"][0]["parameters"]["constraints"]
    if problem == "under": constraints.pop()
    elif problem == "conflict": constraints.append({**constraints[2], "id": "con_conflict", "value": 150})
    elif problem == "opening": constraints[4]["value"] = 80
    elif problem == "twist": raw["features"][1]["parameters"]["twist"] = 30
    elif problem == "placement": raw["features"][1]["placement"] = {"origin": [10, 0, 0]}
    elif problem == "missing": raw["features"].pop(0)
    else: raw["features"][0]["suppressed"] = True
    with pytest.raises(GeometryError):
        solid(raw)


def test_legacy_solid_and_flat_polygon_extrusion_agree():
    legacy = {"features": [{"id": "feat_sketch", "kind": "sketch", "operation": "add",
        "parameters": {"vertices": [[-10, -5], [10, -5], [10, 5], [-10, 5]], "height": 4}}]}
    flat = copy.deepcopy(legacy)
    flat["features"][0]["parameters"]["height"] = 0
    flat["features"].append({"id": "feat_extrude", "kind": "extrude", "operation": "add",
        "parameters": {"target_feature": "feat_sketch", "amount": 4}})
    assert volume(solid(legacy)) == pytest.approx(800)
    assert volume(solid(flat)) == pytest.approx(800)


@pytest.mark.parametrize("height", [-1, float("nan"), float("inf")])
def test_raw_polygon_height_rejected(height):
    with pytest.raises(GeometryError):
        solid({"features": [{"id": "feat_sketch", "kind": "sketch", "operation": "add",
            "parameters": {"vertices": [[0, 0], [10, 0], [0, 10]], "height": height}}]})
