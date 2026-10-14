import copy
import json
from pathlib import Path

import pytest

from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.ledger import BoxFeature, Ledger
from cadenza_contracts.parameters import Relationships, evaluate, resolve_parameters
from cadenza_contracts.patch import PatchOp, apply_patch
from cadenza_contracts.project import load_document


@pytest.fixture
def raw():
    return json.loads((Path(__file__).parents[1] / "contract_fixtures/parameter_plate.json").read_text())


def width(ledger: Ledger) -> float:
    feature = ledger.features[0]
    assert isinstance(feature, BoxFeature)
    return feature.parameters.width


def test_resize_resolves_positions_and_persists_intent(raw):
    original = Ledger.model_validate(raw)
    resized = apply_patch(original, [PatchOp(op="replace", path="/relationships/parameters/plate_width/value", value=140)])
    assert width(resized) == 140
    assert [f.placement.origin[1] for f in resized.features[1:]] == [55, -55, 55, -55]
    assert width(original) == 100
    assert resized.relationships.bindings == original.relationships.bindings
    reopened = load_document(resized.model_dump(mode="json"))
    assert reopened.ledgers[resized.project_id] == resized
    resized.revision = 2
    assert width(resized) == 140


@pytest.mark.parametrize("expression", ["__import__('os')", "x.y", "x[0]", "2 ** 10", "1 // 2", "[1]", "True", "1e309", "1 / 0", "1e9 * 2", "(x for x in [])"])
def test_expressions_are_bounded_arithmetic_only(expression):
    with pytest.raises(ValueError):
        evaluate(expression, lambda name: (3, 1))


def test_unit_arithmetic_and_complexity():
    lookup = lambda name: (12, 1)
    assert evaluate("-width / 2 + width", lookup) == (6, 1)
    assert evaluate("width / width", lookup) == (1, 0)
    for expression in ("width + 3", "width * width", "1 / width", "+".join(["1"] * 30), "1" * 257):
        with pytest.raises(ValueError):
            evaluate(expression, lookup)


@pytest.mark.parametrize("parameters", [
    {"a": {"expression": "b"}, "b": {"expression": "a"}},
    {"a": {"expression": "missing"}}, {"a": {"expression": "3"}},
    {"a": {"value": 3, "expression": "a"}}, {"a": {}},
    {"bad-name": {"value": 3}}, {"a": {"value": float("inf")}},
    {f"p{i}": {"value": i} for i in range(65)},
])
def test_invalid_definitions(parameters):
    with pytest.raises(ValueError):
        resolve_parameters(Relationships.model_validate({"parameters": parameters}))


@pytest.mark.parametrize("edit", ["missing", "duplicate", "unit", "target", "negative", "spacing", "radius", "constraint_unit", "delete_driver"])
def test_invalid_bindings_and_impossible_spacing_are_atomic(raw, edit):
    original = Ledger.model_validate(raw)
    r = raw["relationships"]
    if edit == "missing": r["bindings"][0]["feature_id"] = "feat_missing"
    elif edit == "duplicate": r["bindings"].append(copy.deepcopy(r["bindings"][0]))
    elif edit == "unit": r["bindings"][0]["expression"] = "2"
    elif edit == "target": r["bindings"][0]["target"] = "diameter"
    elif edit == "negative": r["parameters"]["thickness"]["value"] = -10
    elif edit == "spacing": r["parameters"]["plate_width"]["value"] = 35
    elif edit == "radius": r["parameters"]["edge_margin"]["value"] = 2
    elif edit == "constraint_unit": r["constraints"][0]["right"] = "2"
    else: del r["parameters"]["plate_width"]
    before = original.model_dump(mode="json")
    with pytest.raises(CadenzaError):
        apply_patch(original, [PatchOp(op="replace", path="/relationships", value=r)])
    assert original.model_dump(mode="json") == before


def test_bound_overwrite_rejected_and_explicit_unbinding_allowed(raw):
    ledger = Ledger.model_validate(raw)
    edit = PatchOp(op="replace", path="/features/feat_plate/parameters/width", value=140)
    with pytest.raises(CadenzaError, match="driven by"):
        apply_patch(ledger, [edit])
    result = apply_patch(ledger, [PatchOp(op="remove", path="/relationships/bindings/1"), edit])
    assert width(result) == 140
    with pytest.raises(CadenzaError, match="missing"):
        apply_patch(ledger, [PatchOp(op="remove", path="/features/feat_hole_0")])


def test_legacy_document_and_stale_numeric_cache(raw):
    raw["features"][0]["parameters"]["width"] = 999
    assert width(Ledger.model_validate(raw)) == 100
    del raw["relationships"]
    assert width(Ledger.model_validate(raw)) == 999
    assert not Ledger.model_validate(raw).relationships.parameters


def test_raw_geometry_plan_resolves_relationships(raw):
    from cadenza_geometry.plan import BuildPlan, resolve
    raw["relationships"]["parameters"]["plate_width"]["value"] = 140
    plan = resolve(raw)
    assert isinstance(plan, BuildPlan)
    assert plan.features[0].p["width"] == 140


@pytest.mark.parametrize("reverse", [False, True])
def test_dependency_depth_is_bounded(reverse):
    parameters: dict[str, dict[str, str | int]] = {f"p{i}": {"expression": f"p{i+1}"} for i in range(17)}
    parameters["p17"] = {"value": 10}
    if reverse:
        parameters = dict(reversed(list(parameters.items())))
    with pytest.raises(ValueError, match="16 levels"):
        resolve_parameters(Relationships.model_validate({"parameters": parameters}))


@pytest.mark.parametrize("features", [None, {}, [3], [{"kind": "box"}]])
def test_malformed_relationship_feature_inputs_are_validation_errors(raw, features):
    raw["features"] = features
    with pytest.raises(ValueError):
        Ledger.model_validate(raw)
