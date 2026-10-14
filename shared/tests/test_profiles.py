import copy
import json
from pathlib import Path

import pytest

from cadenza_contracts.ledger import Ledger, ProfileFeature
from cadenza_contracts.profiles import ProfileParameters, validate_profile_region
from cadenza_contracts.patch import PatchOp, apply_patch
from cadenza_contracts.errors import CadenzaError


@pytest.fixture
def raw():
    return json.loads((Path(__file__).parents[1] / "contract_fixtures/profile_plate.json").read_text())


def solution(raw):
    result = ProfileParameters.model_validate(raw["features"][0]["parameters"]).solution
    assert result is not None
    return result


def test_exact_solution_rank_and_shared_centre(raw):
    raw["features"][0]["parameters"]["entities"][1].update(x=12, y=-7)
    solved = solution(raw)
    assert solved.status == "fully_constrained" and solved.degrees_of_freedom == 0
    assert solved.entities[1].x == solved.entities[0].x == 0
    assert solved.entities[1].y == solved.entities[0].y == 0
    validate_profile_region(solved)


@pytest.mark.parametrize("constraint,dof", [("con_width", 1), ("con_center", 2), ("con_x", 1)])
def test_underconstrained_reports_free_variables(raw, constraint, dof):
    parameters = raw["features"][0]["parameters"]
    parameters["constraints"] = [c for c in parameters["constraints"] if c["id"] != constraint]
    solved = solution(raw)
    assert solved.status == "underconstrained" and solved.degrees_of_freedom == dof
    assert len(solved.free_parameters) == dof
    with pytest.raises(ValueError, match="underconstrained"):
        validate_profile_region(solved)


def test_redundant_and_conflicting_constraints_identified(raw):
    constraints = raw["features"][0]["parameters"]["constraints"]
    constraints.append({"id": "con_again", "kind": "dimension", "entity_id": "ent_bore", "parameter": "x", "value": 0})
    assert solution(raw).redundant == ["con_again"]
    constraints[-1]["value"] = 5
    solved = solution(raw)
    assert solved.status == "conflicting"
    assert set(solved.conflicts) == {"con_x", "con_center", "con_again"}
    with pytest.raises(ValueError, match="conflicting"):
        validate_profile_region(solved)


def test_constraint_order_and_derived_cache_cannot_change_solution(raw):
    expected = solution(raw)
    parameters = raw["features"][0]["parameters"]
    parameters["constraints"].reverse()
    parameters["solution"] = {"status": "made_up", "entities": []}
    assert solution(raw) == expected


@pytest.mark.parametrize("mutation", ["missing_entity", "wrong_variable", "duplicate_id", "negative", "infinite", "oversized", "bad_opening"])
def test_invalid_constraints_and_shapes(raw, mutation):
    parameters = raw["features"][0]["parameters"]
    if mutation == "missing_entity": parameters["constraints"][0]["entity_id"] = "ent_missing"
    elif mutation == "wrong_variable": parameters["constraints"][0]["parameter"] = "diameter"
    elif mutation == "duplicate_id": parameters["constraints"].append(copy.deepcopy(parameters["constraints"][0]))
    elif mutation == "negative": parameters["constraints"][2]["value"] = -1
    elif mutation == "infinite": parameters["constraints"][2]["value"] = float("inf")
    elif mutation == "oversized": parameters["constraints"] *= 20
    else: parameters["entities"][1]["operation"] = "add"
    with pytest.raises(ValueError):
        validate_profile_region(solution(raw))


@pytest.mark.parametrize("diameter", [60, 70])
def test_tangent_or_escaping_opening_rejected(raw, diameter):
    raw["features"][0]["parameters"]["constraints"][4]["value"] = diameter
    with pytest.raises(ValueError, match="inside"):
        validate_profile_region(solution(raw))


def test_named_constraint_binding_and_override_protection(raw):
    ledger = Ledger.model_validate(raw)
    changed = apply_patch(ledger, [PatchOp(op="replace", path="/relationships/parameters/plate_width/value", value=140)])
    feature = changed.features[0]
    assert isinstance(feature, ProfileFeature) and feature.parameters.solution is not None
    assert feature.parameters.solution.entities[0].width == 140
    assert feature.parameters.entities[0].width == 100  # seed is not the driving dimension
    with pytest.raises(CadenzaError, match="driven by"):
        apply_patch(ledger, [PatchOp(op="replace", path="/features/feat_profile/parameters/constraints/con_width/value", value=140)])
    with pytest.raises(CadenzaError, match="Missing sketch dimension"):
        apply_patch(ledger, [PatchOp(op="remove", path="/features/feat_profile/parameters/constraints/con_width")])


def test_legacy_solid_sketch_and_new_flat_polygon():
    raw = {"features": [{"id": "feat_polygon", "kind": "sketch", "operation": "add",
        "parameters": {"vertices": [[0, 0], [10, 0], [10, 10]], "height": 5}}]}
    solid = Ledger.model_validate(raw)
    assert solid.model_dump()["features"][0]["parameters"]["height"] == 5
    raw["features"][0]["parameters"]["height"] = 0
    assert Ledger.model_validate(raw).model_dump()["features"][0]["parameters"]["height"] == 0


def test_coincident_cycles_are_redundant_and_overlapping_openings_fail(raw):
    parameters = raw["features"][0]["parameters"]
    parameters["entities"].append({"id": "ent_second", "kind": "circle", "operation": "subtract", "diameter": 10})
    parameters["constraints"].extend([
        {"id": "con_second_size", "kind": "dimension", "entity_id": "ent_second", "parameter": "diameter", "value": 10},
        {"id": "con_second_center", "kind": "coincident", "entity_a": "ent_bore", "entity_b": "ent_second"},
        {"id": "con_cycle", "kind": "coincident", "entity_a": "ent_second", "entity_b": "ent_plate"}])
    solved = solution(raw)
    assert solved.status == "fully_constrained" and "con_cycle" in solved.redundant
    with pytest.raises(ValueError, match="overlap"):
        validate_profile_region(solved)
