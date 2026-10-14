"""Independent Stage A measurements across the shared schema and real kernel."""
from copy import deepcopy
import json
import math
from pathlib import Path

from build123d import GeomType, import_step
import pytest

from cadenza_contracts.ledger import Ledger
from cadenza_geometry.builder import build_solid
from cadenza_geometry.exporters import export_step_bytes
from cadenza_geometry.plan import BuildPlan, resolve
from cadenza_geometry.service import Build123dGeometryService

CORPUS = json.loads((Path(__file__).parents[2] / "backend/tests/fixtures/three_d_workflows.json").read_text())


def volume(shape):
    return sum(s.volume for s in shape.solids())


def bounds(shape):
    box = shape.bounding_box()
    return (*box.min, *box.max)


def build(raw):
    # Shared validation must not be bypassed by a raw-dict-only kernel test.
    plan = resolve(Ledger.model_validate(raw))
    assert isinstance(plan, BuildPlan)
    return build_solid(plan)


def cylinders(shape):
    facts = []
    for face in shape.faces():
        if face.geom_type != GeomType.CYLINDER:
            continue
        radius, axis = face.radius, face.axis_of_rotation
        assert radius is not None and axis is not None
        facts.append((axis.position.X, axis.position.Y, radius))
    return facts


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda c: c["id"])
def test_fixture_geometry_identity_and_step(case, tmp_path):
    ledgers = {key: Ledger.model_validate(CORPUS["ledgers"][key]) for key in case["sources"]}
    primary = ledgers[case["primary"]]
    compiled = Build123dGeometryService()._compile(primary, ledgers)
    shape = compiled.solid
    expected = case["expected"]
    expected_volume = expected["constant"] + math.pi * expected["pi_coefficient"]
    assert shape.is_valid
    assert volume(shape) == pytest.approx(expected_volume, rel=1e-7)
    assert bounds(shape) == pytest.approx(expected["bbox"], abs=1e-6)
    assert len(shape.solids()) == expected["solid_count"]
    if "feature_count" in expected:
        assert len(primary.features) == expected["feature_count"]
    for x, y, radius in expected.get("bores", []):
        matches = [f for f in cylinders(shape) if math.isclose(f[2], radius, abs_tol=1e-6)
                   and math.isclose(f[0], x, abs_tol=1e-6) and math.isclose(f[1], y, abs_tol=1e-6)]
        assert len(matches) == 1
    if "blind_floor_z" in expected:
        assert any(f.geom_type == GeomType.PLANE and abs(f.center().Z - expected["blind_floor_z"]) < 1e-6
                   and abs(f.area - 25 * math.pi) < 1e-5 for f in shape.faces())
    if "instance_centers" in expected:
        assert set(compiled.instances) == set(expected["instance_centers"])
        for key, center in expected["instance_centers"].items():
            leaf = compiled.instances[key]
            box = bounds(leaf.location * leaf.solid)
            assert tuple((box[i] + box[i + 3]) / 2 for i in range(3)) == pytest.approx(center)
    filename = tmp_path / f"{case['id']}.step"
    filename.write_bytes(export_step_bytes(shape))
    reopened = import_step(filename)
    assert reopened.is_valid
    assert volume(reopened) == pytest.approx(expected_volume, rel=1e-7)
    assert bounds(reopened) == pytest.approx(expected["bbox"], abs=1e-6)
    assert len(reopened.solids()) == expected["solid_count"]


@pytest.mark.parametrize("diameter", [4, 8, 10])
def test_shaft_bore_variants_preserve_steps(diameter):
    raw = deepcopy(CORPUS["ledgers"]["proj_shaft"])
    raw["features"][-1]["parameters"]["diameter"] = diameter
    shape = build(raw)
    assert volume(shape) == pytest.approx(math.pi * (3720 - diameter**2 / 4 * 50), rel=1e-7)
    assert bounds(shape) == pytest.approx((-10, -10, 0, 10, 10, 50))
    assert sorted(round(f[2], 6) for f in cylinders(shape)) == [diameter / 2, 6, 10]


def patterned_plate(kind, count=4, radius=20):
    linear = kind == "linear_pattern"
    return {"features": [
        {"id": "feat_plate", "kind": "box", "parameters": {"length": 100, "width": 80, "height": 8}},
        {"id": "feat_bore", "kind": "hole", "parameters": {"diameter": 6, "through": True},
         "placement": {"origin": [-30 if linear else 20, 0, 8]}},
        {"id": "feat_pattern", "kind": kind, "operation": "subtract", "parameters":
         {"target_feature": "feat_bore", "count": count, **({"spacing": 20} if linear else {"radius": radius})}}
    ]}


@pytest.mark.parametrize("kind", ["linear_pattern", "circular_pattern"])
@pytest.mark.parametrize("count", [2, 4])
def test_pattern_counts_and_positions(kind, count):
    shape = build(patterned_plate(kind, count))
    expected = [(-30 + 20 * i, 0) if kind == "linear_pattern" else
                (20 * math.cos(2 * math.pi * i / count), 20 * math.sin(2 * math.pi * i / count)) for i in range(count)]
    actual = [(x, y) for x, y, _ in cylinders(shape)]
    assert len(actual) == count
    for center in expected:
        assert any(math.dist(center, p) < 1e-6 for p in actual)
    assert volume(shape) == pytest.approx(64000 - count * math.pi * 9 * 8)


@pytest.mark.parametrize("angle", [180, 360])
def test_revolve_annular_profile_volume(angle):
    shape = build({"features": [{"id": "feat_revolve", "kind": "revolve", "parameters": {
        "vertices": [[5, 0], [10, 0], [10, 20], [5, 20]], "axis": [0, 1, 0], "angle": angle}}]})
    assert shape.is_valid and len(shape.solids()) == 1
    assert volume(shape) == pytest.approx(math.pi * (100 - 25) * 20 * angle / 360)


def test_circular_radius_compatibility_and_explicit_seed_driver():
    # C1-approved A1 refinement: preserve legacy loads, reject ineffective
    # mutations, and demonstrate the real seed-position driver independently.
    from cadenza_contracts.patch import PatchOp, apply_patch
    from cadenza_contracts.errors import CadenzaError
    before = build(patterned_plate("circular_pattern", radius=20))
    after = build(patterned_plate("circular_pattern", radius=30))
    centers = lambda shape: sorted((round(x, 6), round(y, 6)) for x, y, _ in cylinders(shape))
    assert centers(before) == centers(after)
    ledger = Ledger.model_validate(patterned_plate("circular_pattern", radius=20))
    with pytest.raises(CadenzaError, match="compatibility data"):
        apply_patch(ledger, [PatchOp(op="replace", path="/features/feat_pattern/parameters/radius", value=30)])
    moved = apply_patch(ledger, [PatchOp(op="replace", path="/features/feat_bore/placement/origin/0", value=30)])
    assert centers(before) != centers(build(moved.model_dump(mode="json")))


@pytest.mark.parametrize("kind,dimension", [("edge_fillet", "radius"), ("edge_chamfer", "width")])
def test_selected_edge_operation_crosses_shared_contract(kind, dimension):
    raw = {"features": [
        {"id": "feat_stock", "kind": "box", "parameters": {"length": 20, "width": 20, "height": 20}},
        {"id": "feat_finish", "kind": kind, "operation": "modify", "parameters": {dimension: 1, "edge_selectors": [[10, 10, 10]]}}
    ]}
    shape = build(raw)
    assert shape.is_valid and 0 < volume(shape) < 8000


def test_open_shell_selector_crosses_shared_contract():
    shape = build({"features": [
        {"id": "feat_stock", "kind": "box", "parameters": {"length": 20, "width": 20, "height": 20}},
        {"id": "feat_shell", "kind": "shell", "operation": "modify", "parameters": {"thickness": -2, "open_faces": [[0, 0, 20]]}}
    ]})
    assert shape.is_valid
    assert volume(shape) == pytest.approx(8000 - 16 * 16 * 18)


@pytest.mark.parametrize("kind,dimension", [("edge_fillet", "radius"), ("edge_chamfer", "width")])
def test_legacy_cutter_remains_distinct_from_selected_edge(kind, dimension):
    shape = build({"features": [
        {"id": "feat_stock", "kind": "box", "parameters": {"length": 20, "width": 20, "height": 20}},
        {"id": "feat_legacy", "kind": kind, "operation": "subtract", "parameters": {dimension: 1, "length": 20},
         "placement": {"origin": [-10, -10, 10]}}
    ]})
    assert shape.is_valid and len(shape.solids()) == 1
    assert 0 < volume(shape) < 8000


@pytest.mark.parametrize("kind", ["edge_fillet", "edge_chamfer", "shell"])
def test_kernel_only_modifiers_have_measurable_geometry(kind):
    # This intentionally bypasses shared validation to localize the mismatch.
    # Passing here does NOT mean these requests are usable through the application.
    parameters = {"radius" if kind == "edge_fillet" else "width": 1, "edge_selectors": [[10, 10, 10]]}
    expected = 8000 - (1 - math.pi / 4) * 20 if kind == "edge_fillet" else 7990
    if kind == "shell":
        parameters = {"thickness": -2, "open_faces": [[0, 0, 20]]}
        expected = 8000 - 16 * 16 * 18
    plan = resolve({"features": [
        {"id": "feat_stock", "kind": "box", "parameters": {"length": 20, "width": 20, "height": 20}},
        {"id": "feat_finish", "kind": kind, "operation": "modify", "parameters": parameters}
    ]})
    assert isinstance(plan, BuildPlan)
    shape = build_solid(plan)
    assert shape.is_valid and len(shape.solids()) == 1
    assert volume(shape) == pytest.approx(expected)
