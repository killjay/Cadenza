from copy import deepcopy
from typing import Any
import pytest
from pydantic import ValidationError
from cadenza_contracts.ledger import Joint, PlanarTarget
from cadenza_contracts.assembly_commands import AssemblyCapabilitiesResponse, PLANAR_OPERATIONS


def target() -> dict[str, Any]:
    return dict(mode="planar_face", reference=dict(ledger_id="source", source_ledger_id="source",
        feature_id="feat_stock", feature_kind="box", operation="add", role="top_face"), uv=[0, 0])


@pytest.mark.parametrize("value", [True, "1", float("nan"), float("inf"), None])
def test_uv_requires_finite_numeric_values(value):
    data = target(); data["uv"][0] = value
    with pytest.raises(ValidationError): PlanarTarget.model_validate(data)


@pytest.mark.parametrize("change", ["mixed", "twist_missing", "twist_bool", "motion", "unknown", "path", "lateral"])
def test_invalid_joint_modes_rejected(change):
    data: dict[str, Any] = dict(id="joint_test", kind="rigid", component_a="comp_a", component_b="comp_b",
        target_a=target(), target_b=deepcopy(target()), twist_deg=0.0)
    if change == "mixed": data["target_b"] = dict(point=[0, 0, 0])
    if change == "twist_missing": del data["twist_deg"]
    if change == "twist_bool": data["twist_deg"] = True
    if change == "motion": data["kind"] = "revolute"
    if change == "unknown": data["target_a"]["mode"] = "future"
    if change == "path": data["target_a"]["reference"]["component_path"] = ["comp_nested"]
    if change == "lateral": data["target_a"]["reference"].update(feature_kind="cylinder", role="lateral_face")
    with pytest.raises(ValidationError): Joint.model_validate(data)


def test_version_three_discovery_and_legacy_joint_shape():
    cap = AssemblyCapabilitiesResponse(message_id="cap", session_id="session", revision=0, ledger_id="root",
        version=3, owner_path=[], operations=list(PLANAR_OPERATIONS), can_edit=True, can_initialize=False, reason=None)
    assert cap.model_dump()["operations"][-1] == "add_planar_attachment"
    joint = Joint.model_validate(dict(id="joint_old", kind="rigid", component_a="comp_a", component_b="comp_b",
        target_a=dict(point=[0, 0, 0]), target_b=dict(point=[0, 0, 1])))
    assert "twist_deg" not in joint.model_dump(mode="json")


@pytest.mark.parametrize("version", [True, "2", 2.0, 3, None])
def test_document_version_does_not_coerce_unknown_or_noninteger_values(version):
    from cadenza_contracts.project import load_document
    with pytest.raises(ValidationError):
        load_document(dict(document_version=version, name="Test", primary_ledger_id="part",
            ledgers={"part": dict(project_id="part")}))
