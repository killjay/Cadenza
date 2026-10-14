import pytest
from typing import Any
from pydantic import ValidationError

from cadenza_contracts.messages import PartPrompt, parse_client_message
from cadenza_contracts.part_commands import PartCreate


def creation(**updates) -> dict[str, Any]:
    return dict(message_id="new", base_revision=0, name="Plate", operations=[dict(
        op="add_feature", kind="box", name="Stock", operation="add", placement={},
        parameters=dict(length=40, width=30, height=5))], **updates)


@pytest.mark.parametrize("field,value", [("name", " "), ("base_revision", True),
    ("base_revision", "0"), ("ledger_id", "prj_injected"), ("operations", []), ("name", "x" * 101)])
def test_strict_part_request(field, value):
    raw = creation(); raw[field] = value
    with pytest.raises(ValidationError): PartCreate.model_validate(raw)


@pytest.mark.parametrize("value", [True, "40", float("nan"), float("inf"), -1, 0])
def test_strict_dimensions(value):
    raw = creation(); raw["operations"][0]["parameters"]["length"] = value
    with pytest.raises(ValidationError): PartCreate.model_validate(raw)


def test_creation_bounds_and_discriminator():
    raw = creation(); raw["operations"] *= 33
    with pytest.raises(ValidationError): PartCreate.model_validate(raw)
    raw = creation(); raw["operations"][0]["operation"] = "subtract"
    with pytest.raises(ValidationError): PartCreate.model_validate(raw)
    raw = creation(); raw["name"] = " Plate "
    assert PartCreate.model_validate(raw).name == "Plate"
    assert isinstance(parse_client_message(dict(type="design.part.create", **raw)), PartCreate)
    with pytest.raises(ValidationError):
        PartPrompt(message_id="p", base_revision=0, name="A", prompt=" ")
