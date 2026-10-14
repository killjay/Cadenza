from typing import Any
import pytest
from pydantic import ValidationError
from cadenza_contracts.assembly_commands import AssemblyCommand
from cadenza_contracts.messages import parse_client_message, parse_server_message


def request(**updates) -> dict[str, Any]:
    return dict(type="design.assembly.command", message_id="asm_test", base_revision=0,
        ledger_id="root", scope="assembly_definition", operations=[dict(op="add_instance",
        source_ledger_id="source", name="Plate", placement=dict(origin=[0, 0, 0], rotation_deg=[0, 0, 0]))], **updates)


@pytest.mark.parametrize("value", [True, "1", float("nan"), float("inf"), None])
def test_coordinates_are_strict_and_finite(value):
    data = request(); data["operations"][0]["placement"]["origin"][0] = value
    with pytest.raises(ValidationError): AssemblyCommand.model_validate(data)


@pytest.mark.parametrize("change", [
    {"base_revision": True}, {"base_revision": -1}, {"base_revision": "0"}, {"scope": "source"},
    {"operations": []}, {"unexpected": 1},
    {"operations": [dict(op="remove_instance", component_id="comp_parent/comp_child")]},
    {"operations": [dict(op="remove_instance", component_id="comp_a")] * 17},
])
def test_invalid_envelopes(change):
    with pytest.raises(ValidationError): parse_client_message({**request(), **change})


@pytest.mark.parametrize("change", [{"id": "comp_minted"}, {"name": " "}, {"name": "x" * 101},
    {"placement": dict(origin=[0, 0], rotation_deg=[0, 0, 0])},
    {"placement": dict(origin=[0, 0, 0], rotation_deg=[0, 0, 0], relative_to="comp_other")}])
def test_invalid_operation_fields(change):
    data = request(); data["operations"][0].update(change)
    with pytest.raises(ValidationError): parse_client_message(data)


def test_transport_roundtrip_and_name_normalization():
    data = request(); data["operations"][0]["name"] = " Plate "
    message = parse_client_message(data)
    assert isinstance(message, AssemblyCommand)
    from cadenza_contracts.assembly_commands import AddInstance
    assert isinstance(message.operations[0], AddInstance)
    assert message.operations[0].name == "Plate"
    assert parse_client_message(message.model_dump(mode="json")) == message
    from cadenza_contracts.assembly_commands import AssemblyCapabilitiesResponse
    cap = AssemblyCapabilitiesResponse(message_id="cap", session_id="session", revision=0, ledger_id="root",
        can_initialize=True, can_edit=False, reason=None)
    assert parse_server_message(cap.model_dump(mode="json")) == cap


def rigid_operation():
    return dict(op="add_rigid_joint", component_a="comp_a", component_b="comp_b",
        target_a=dict(point=[0, 0, 4], normal=[0, 0, 1]),
        target_b=dict(point=[30, 0, 0], normal=[0, 0, -2]))


def test_e2_joint_and_owner_contract_roundtrip():
    data = {**request(), "owner_path": ["comp_outer"], "operations": [rigid_operation()]}
    assert parse_client_message(data).model_dump(mode="json")["owner_path"] == ["comp_outer"]
    assert parse_client_message({**request(), "operations": [dict(op="remove_joint", joint_id="joint_link")]})


@pytest.mark.parametrize("target", [dict(point=[0,0,0],normal=[0,0,0]),
    dict(point=[0,0,0],normal=[True,0,1]), dict(point=[0,0,0],normal=None),
    dict(point=[0,0,0],normal=[0,0,1],reference_feature="feat_box")])
def test_e2_rejects_invalid_or_one_sided_anchors(target):
    operation=rigid_operation(); operation["target_a"]=target
    with pytest.raises(ValidationError): parse_client_message({**request(),"operations":[operation]})


def test_e2_subassembly_contract_boundaries():
    child = dict(source_ledger_id="source",name="Child",placement=dict(origin=[0,0,0],rotation_deg=[0,0,0]))
    operation = dict(op="create_subassembly",name="Module",instance_name="Module A",
        placement=child["placement"],children=[child]*16)
    assert parse_client_message({**request(),"operations":[operation]})
    for children in ([],[child]*17):
        with pytest.raises(ValidationError): parse_client_message({**request(),"operations":[{**operation,"children":children}]})


@pytest.mark.parametrize("path", [["comp_x"]*9,["comp_a/comp_b"],["feat_box"],[True]])
def test_e2_invalid_owner_paths(path):
    with pytest.raises(ValidationError):parse_client_message(request(owner_path=path))


def test_e2_discovery_versions_have_exact_shapes():
    from cadenza_contracts.assembly_commands import AssemblyCapabilitiesRequest, AssemblyCapabilitiesResponse, E2_OPERATIONS
    assert AssemblyCapabilitiesRequest(message_id="c").model_dump()==dict(type="design.assembly.capabilities",message_id="c")
    for updates in [dict(version=True),dict(version="1"),dict(version=2),dict(version=1,ledger_id="root"),dict(version=2,ledger_id="root",owner_path=["comp_x"]*9)]:
        with pytest.raises(ValidationError):AssemblyCapabilitiesRequest.model_validate(dict(message_id="c",**updates))
    cap=dict(message_id="c",session_id="s",revision=0,ledger_id="root",can_initialize=True,can_edit=False,reason=None)
    with pytest.raises(ValidationError):AssemblyCapabilitiesResponse.model_validate(dict(**cap,version=1,operations=list(E2_OPERATIONS)))
    with pytest.raises(ValidationError):AssemblyCapabilitiesResponse.model_validate(dict(**cap,version=2,operations=list(E2_OPERATIONS)))
    assert AssemblyCapabilitiesResponse.model_validate(dict(**cap,version=2,owner_path=[],operations=list(E2_OPERATIONS))).model_dump()["owner_path"]==[]
