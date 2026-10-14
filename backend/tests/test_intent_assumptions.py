"""The schema advertised to AI must match bounded interpreter metadata."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cadenza_backend.agents import assembly_intents, design_intents
from cadenza_contracts.ledger import Assumption, Ledger

CAPTURED = Path(__file__).parent / "fixtures/assembly_assumption_response.json"


def response(module, assumption):
    operation = (dict(op="rename_instance", component_id="comp_a", name="A") if module is assembly_intents
        else dict(op="set_dimension", feature_id="feat_stock", parameter="height", value=10))
    return dict(kind="proposal", description="Edit", operations=[operation], assumptions=[assumption])


def assumption(value):
    return dict(field="placement.origin", value=value, basis="Owner coordinates", confidence=0.8)


@pytest.mark.parametrize("module", [assembly_intents, design_intents])
def test_advertised_assumption_schema(module):
    schema = module.CARD.output_schema
    ref = schema["$defs"]["Proposal"]["properties"]["assumptions"]["items"]["$ref"].split("/")[-1]
    metadata = schema["$defs"][ref]
    types = metadata["properties"]["value"].get("anyOf", [])
    assert {branch["type"] for branch in types} == {"string", "integer", "number", "boolean", "null"}
    assert next(branch for branch in types if branch["type"] == "string")["maxLength"] == 500
    assert all(metadata["properties"][key]["maxLength"] == 500 for key in ("field", "basis"))
    assert metadata["additionalProperties"] is False


@pytest.mark.parametrize("module", [assembly_intents, design_intents])
@pytest.mark.parametrize("value", ["35", "(35,0,0)", 35, 35.25, True, False, None, "x" * 500])
def test_scalar_values_preserve_type(module, value):
    parsed = module.INTENT.validate_python(response(module, assumption(value)))
    assert type(parsed.assumptions[0].value) is type(value)
    assert parsed.assumptions[0].value == value


@pytest.mark.parametrize("module", [assembly_intents, design_intents])
@pytest.mark.parametrize("changes", [
    dict(value=[35, 0, 0]), dict(value={"op": "remove_joint"}), dict(value=float("nan")),
    dict(value=float("inf")), dict(value=-float("inf")), dict(value=10 ** 501),
    dict(value="x" * 501), dict(field="x" * 501), dict(basis="x" * 501), dict(extra="unexpected"),
    dict(confidence=-0.01), dict(confidence=1.01), dict(confidence=float("nan")),
])
def test_invalid_metadata_is_rejected(module, changes):
    with pytest.raises(ValidationError):
        module.INTENT.validate_python(response(module, {**assumption(35), **changes}))


@pytest.mark.parametrize("module", [assembly_intents, design_intents])
def test_count_and_confidence_bounds(module):
    raw = response(module, assumption(35))
    for confidence in (0, 1):
        raw["assumptions"] = [{**assumption(35), "confidence": confidence}] * 8
        assert len(module.INTENT.validate_python(raw).assumptions) == 8
    raw["assumptions"] *= 2
    with pytest.raises(ValidationError): module.INTENT.validate_python(raw)


def test_captured_response_stays_invalid_and_offline_text_variant_is_valid():
    raw = json.loads(CAPTURED.read_text(encoding="utf-8"))
    with pytest.raises(ValidationError): assembly_intents.INTENT.validate_python(raw)
    raw["assumptions"][0]["value"] = "(35,0,0)"  # Offline variant; never replace live evidence.
    assert assembly_intents.INTENT.validate_python(raw).operations[0].op == "remove_joint"


def test_saved_ledger_structured_assumptions_remain_compatible(plate_ledger):
    raw = plate_ledger.model_dump(mode="json")
    raw["features"][0]["ai_context"]["assumptions"] = [assumption([35, 0, 0])]
    ledger = Ledger.model_validate(raw)
    reopened = Ledger.model_validate_json(ledger.model_dump_json())
    assert reopened.features[0].ai_context.assumptions[0].value == [35, 0, 0]
    assert Assumption.model_validate(assumption({"legacy": [1, 2]})).value == {"legacy": [1, 2]}
