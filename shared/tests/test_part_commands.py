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


def test_rename_source_contract():
    from cadenza_contracts.part_commands import PartRename
    raw = dict(type="design.part.rename", message_id="rename", base_revision=2, ledger_id="prj_plate", name=" Plate ")
    parsed = parse_client_message(raw)
    assert isinstance(parsed, PartRename) and parsed.name == "Plate"
    for changes in ({"name": " "}, {"name": "x" * 101}, {"ledger_id": ""},
                    {"base_revision": True}, {"base_revision": "2"}, {"operations": []}):
        with pytest.raises(ValidationError):
            parse_client_message({**raw, **changes})


def test_source_context_contract_bounds():
    from cadenza_contracts.part_commands import PartViewRequest, PartViewReady
    raw = dict(message_id="view", base_revision=0, ledger_id="prj_plate")
    assert PartViewRequest.model_validate(raw).component_path is None
    assert PartViewRequest.model_validate({**raw, "component_path": ["comp_parent", "comp_leaf"]}).component_path
    for path in ([], ["comp_a"] * 9, ["bad"], [12], "comp_a"):
        with pytest.raises(ValidationError): PartViewRequest.model_validate({**raw, "component_path": path})
    response = dict(message_id="view", session_id="ses_test", revision=0, ledger_id="prj_plate", view_id="view")
    for matrix in ([1] * 15, [float("nan")] * 16, [float("inf")] * 16, [True] * 16):
        with pytest.raises(ValidationError): PartViewReady.model_validate({**response, "source_to_project": matrix})


def test_creation_and_discovery_versions_are_strict_and_legacy_shape_is_preserved():
    from cadenza_contracts.part_commands import PartCapabilitiesRequest, PartCapabilitiesResponse, PartCapabilitiesV2Response
    from cadenza_contracts.messages import parse_server_message
    for value in (True, 2.0, "2", 0, 3):
        with pytest.raises(ValidationError): PartCapabilitiesRequest.model_validate(dict(message_id="cap", version=value))
        with pytest.raises(ValidationError): PartCreate.model_validate(creation(creation_version=value))
    legacy = PartCapabilitiesResponse(message_id="cap", session_id="test").model_dump(mode="json")
    assert legacy["starter_kinds"] == ["box", "cylinder"] and legacy["version"] == 1
    current = PartCapabilitiesV2Response(message_id="cap", session_id="test").model_dump(mode="json")
    assert current["starter_kinds"] == ["box", "cylinder", "revolve", "gear"]
    parsed_current, parsed_legacy = parse_server_message(current), parse_server_message(legacy)
    assert isinstance(parsed_current, PartCapabilitiesV2Response) and parsed_current.version == 2
    assert isinstance(parsed_legacy, PartCapabilitiesResponse) and parsed_legacy.version == 1
