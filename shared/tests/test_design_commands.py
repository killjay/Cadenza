import pytest
from pydantic import ValidationError
from cadenza_contracts.design_commands import AddFeature, DesignCommand, RemoveFeature, SetDimension, SetOrigin, SetSuppressed

from pydantic import ValidationError, TypeAdapter
from cadenza_contracts.design_commands import CommandOperation, DesignCommand, SetDimension, SetOrigin, SetSuppressed

def test_add_feature_validation():
    adapter = TypeAdapter(CommandOperation)
    # Valid
    adapter.validate_python({
        "op": "add_feature",
        "kind": "box",
        "name": "Block",
        "operation": "add",
        "placement": {"origin": [0, 0, 0]},
        "parameters": {"length": 10, "width": 20, "height": 30}
    })
    
    # Invalid op
    with pytest.raises(ValidationError):
        adapter.validate_python({"op": "invalid", "kind": "box", "name": "", "operation": "add", "placement": {"origin": [0,0,0]}, "parameters": {}})

    # Strict numeric coercion rejection (no boolean to float)
    with pytest.raises(ValidationError):
        adapter.validate_python({
            "op": "add_feature", "kind": "box", "name": "Block", "operation": "add",
            "placement": {"origin": [0,0,0]}, "parameters": {"length": True, "width": 20, "height": 30}
        })
        
    # Strict boolean coercion rejection (no string to bool, etc)
    with pytest.raises(ValidationError):
        adapter.validate_python({
            "op": "add_feature", "kind": "hole", "name": "Hole", "operation": "add",
            "placement": {"origin": [0,0,0]}, "parameters": {"diameter": 10, "through": 1}
        })

    # Mismatched kind and parameters (box with cylinder params)
    with pytest.raises(ValidationError):
        adapter.validate_python({
            "op": "add_feature", "kind": "box", "name": "Block", "operation": "add",
            "placement": {"origin": [0,0,0]}, "parameters": {"diameter": 10, "height": 10}
        })

    # Blind hole missing depth
    with pytest.raises(ValidationError):
        adapter.validate_python({
            "op": "add_feature", "kind": "hole", "name": "Hole", "operation": "add",
            "placement": {"origin": [0,0,0]}, "parameters": {"diameter": 10, "through": False}
        })

def test_set_dimension_validation():
    # Valid
    SetDimension.model_validate({"op": "set_dimension", "feature_id": "feat_123", "parameter": "length", "value": 100})
    
    # Invalid parameter
    with pytest.raises(ValidationError):
        SetDimension.model_validate({"op": "set_dimension", "feature_id": "feat_123", "parameter": "invalid", "value": 100})

def test_design_command_validation():
    # Valid
    DesignCommand.model_validate({
        "type": "design.command",
        "message_id": "msg_1",
        "base_revision": 1,
        "ledger_id": "prj_1",
        "scope": "source",
        "operations": [{"op": "remove_feature", "feature_id": "feat_123"}]
    })


@pytest.mark.parametrize("field", ["origin", "rotation_deg"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "3"])
def test_creation_rejects_nonfinite_or_coerced_placement(field, value):
    with pytest.raises(ValidationError):
        TypeAdapter(CommandOperation).validate_python(dict(op="add_feature", kind="box", name="Box",
            operation="add", placement={field: [value, 0, 0]}, parameters=dict(length=10, width=10, height=10)))


def test_new_creation_rejects_relative_placement_without_changing_legacy_schema():
    from cadenza_contracts.ledger import Placement
    placement = {"origin": [-3, 0, 2], "relative_to": "feat_parent"}
    assert Placement.model_validate(placement).relative_to == "feat_parent"
    with pytest.raises(ValidationError):
        TypeAdapter(CommandOperation).validate_python(dict(op="add_feature", kind="box", name="Box",
            operation="add", placement=placement, parameters=dict(length=10, width=10, height=10)))
