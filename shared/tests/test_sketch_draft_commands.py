import pytest
from pydantic import ValidationError

from cadenza_contracts.sketch_draft import DraftUpdate


@pytest.mark.parametrize("operation", [
    {"op": "add_entity", "alias": "Bad alias", "entity": {"kind": "circle", "diameter": 10}},
    {"op": "add_dimension", "entity_id": {"constraint_alias": "wrong"}, "parameter": "x", "value": 0},
    {"op": "bind_dimension", "constraint_id": {"entity_alias": "wrong"}, "expression": "width"},
    {"op": "bind_dimension", "constraint_id": "con_x", "expression": "x" * 257},
    {"op": "add_driver", "parameter": "a", "value": float("inf")},
    {"op": "add_driver", "parameter": "a", "value": 10, "unit": "cm"},
    {"op": "add_driver", "parameter": "a", "value": 10, "expression": "a"},
    {"op": "bind_dimension", "constraint_id": "con_x", "expression": "width", "feature_id": "feat_other"},
])
def test_relationship_commands_reject_malformed_or_out_of_scope_fields(operation):
    with pytest.raises(ValidationError):
        DraftUpdate.model_validate(dict(message_id="test", base_revision=0, draft_id="draft", draft_revision=0, operations=[operation]))
