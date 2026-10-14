"""Pattern mutation admission must not alter compatibility reads."""
from copy import deepcopy

import pytest

from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.ledger import Ledger, CircularPatternFeature
from cadenza_contracts.patch import PatchOp, apply_patch
from cadenza_contracts.design_commands import DesignCommand
from pydantic import ValidationError


def source():
    return Ledger.model_validate({"features": [
        {"id": "feat_seed", "kind": "cylinder", "parameters": {"diameter": 4, "height": 5},
         "placement": {"origin": [20, 0, 0]}},
        {"id": "feat_pattern", "kind": "circular_pattern", "parameters": {
            "target_feature": "feat_seed", "count": 4, "radius": 20}}]})


def circle_feature(ledger):
    feature = ledger.features[1]
    assert isinstance(feature, CircularPatternFeature)
    return feature


@pytest.mark.parametrize("path", ["/features/1/parameters/radius", "/features/feat_pattern/parameters/radius",
                                 "/features/1/parameters", "/features/1", "/features"])
def test_radius_write_rejects_at_every_parent(path):
    ledger = source()
    raw = ledger.model_dump(mode="json")
    raw["features"][1]["parameters"]["radius"] = 30
    value = {"/features": raw["features"], "/features/1": raw["features"][1],
             "/features/1/parameters": raw["features"][1]["parameters"]}.get(path, 30)
    with pytest.raises(CadenzaError, match="compatibility data"):
        apply_patch(ledger, [PatchOp(op="replace", path=path, value=value)])
    assert circle_feature(ledger).parameters.radius == 20


def test_remove_readd_identity_and_embedded_parts_cannot_bypass():
    ledger = source()
    raw = ledger.features[1].model_dump(mode="json")
    raw["parameters"]["radius"] = 30
    with pytest.raises(CadenzaError, match="compatibility data"):
        apply_patch(ledger, [PatchOp(op="remove", path="/features/1"), PatchOp(op="add", path="/features/-", value=raw)])
    embedded = Ledger.model_validate({"assembly": {"parts": [{"id": "part_plate", "name": "Plate",
        "features": ledger.model_dump(mode="json")["features"]}]}})
    with pytest.raises(CadenzaError, match="compatibility data"):
        apply_patch(embedded, [PatchOp(op="replace", path="/assembly/parts/part_plate/features/feat_pattern/parameters/radius", value=30)])


@pytest.mark.parametrize("count", [True, "4", 4.0, 4.5, 1, 33])
def test_count_is_strict_at_raw_patch_boundary(count):
    with pytest.raises(CadenzaError):
        apply_patch(source(), [PatchOp(op="replace", path="/features/1/parameters/count", value=count)])


def test_legacy_read_and_seed_edit_keep_stale_radius():
    raw = source().model_dump(mode="json")
    raw["features"][1]["parameters"].update(radius=777, sweep_angle=180, axis=[1, 0, 0])
    ledger = Ledger.model_validate(raw)
    edited = apply_patch(ledger, [PatchOp(op="replace", path="/features/0/placement/origin/0", value=30)])
    assert edited.features[1] == ledger.features[1]


def test_unchanged_radius_parent_replacement_and_new_admission():
    ledger = source()
    params = ledger.features[1].parameters.model_dump(mode="json")
    params["count"] = 8
    assert circle_feature(apply_patch(ledger, [PatchOp(op="replace", path="/features/1/parameters", value=params)])).parameters.count == 8
    pattern = ledger.features[1].model_dump(mode="json")
    pattern["id"] = "feat_new"
    for bad in [{"radius": 30}, {"axis": [1, 0, 0]}, {"sweep_angle": 180}, {"count": 33}]:
        altered = deepcopy(pattern)
        altered["parameters"].update(bad)
        with pytest.raises(CadenzaError):
            apply_patch(ledger, [PatchOp(op="add", path="/features/-", value=altered)])


@pytest.mark.parametrize("override", [{"count": v} for v in [True, "4", 4.0, 4.5, 1, 33]] +
    [{"spacing": v} for v in [0, -1, float("nan"), float("inf"), True, "2", 1e10]] +
    [{"direction": "diagonal"}, {"radius": 20}, {"id": "feat_client"}, {"operation": "add"}])
def test_strict_typed_pattern_fields(override):
    op = dict(op="add_linear_pattern", name="Pattern", target_feature="feat_seed", count=4, spacing=10, direction="+x")
    op.update(override)
    with pytest.raises(ValidationError):
        DesignCommand.model_validate(dict(message_id="msg", base_revision=0, ledger_id="prj", operations=[op]))


def test_restoring_overbudget_and_retargeting_cannot_bypass():
    raw = source().model_dump(mode="json")
    for i in range(4):
        f = deepcopy(raw["features"][1]); f["id"] = f"feat_pattern_{i}"; f["parameters"]["count"] = 32
        raw["features"].append(f)
    raw["features"][1]["parameters"]["count"] = 32
    raw["features"][1]["suppressed"] = True
    ledger = Ledger.model_validate(raw)
    with pytest.raises(CadenzaError, match="128"):
        apply_patch(ledger, [PatchOp(op="replace", path="/features/1/suppressed", value=False)])
    with pytest.raises(CadenzaError):
        apply_patch(source(), [PatchOp(op="replace", path="/features/1/parameters/target_feature", value="feat_pattern")])
