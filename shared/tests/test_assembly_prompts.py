import pytest
from pydantic import ValidationError

from cadenza_contracts.messages import AssemblyPrompt, parse_client_message, parse_server_message
from cadenza_contracts.assembly_commands import AssemblyPromptCapabilitiesResponse


@pytest.mark.parametrize("target", [dict(kind="definition"), dict(kind="instance", component_id="comp_a"),
    dict(kind="attachment", component_a="comp_a", component_b="comp_b", joint_id="joint_a")])
def test_prompt_wire_roundtrip_requires_owner_context(target):
    data = dict(type="design.assembly.prompt", message_id="e3_wire", base_revision=0, ledger_id="root",
        owner_path=["comp_outer"], scope="assembly_definition", target=target, prompt="Describe the change")
    message = parse_client_message(data)
    assert isinstance(message, AssemblyPrompt)
    assert parse_client_message(message.model_dump(mode="json")) == message
    del data["owner_path"]
    with pytest.raises(ValidationError): parse_client_message(data)


@pytest.mark.parametrize("change", [dict(version=True), dict(version="1"), dict(version=2), dict(available="true"),
    dict(target_modes=["definition", "instance"]), dict(revision=True), dict(owner_path=["comp_a"] * 9)])
def test_capability_wire_rejects_unknown_or_coerced_shape(change):
    raw = dict(type="design.assembly.prompt.capabilities", message_id="cap", session_id="session", revision=0,
        ledger_id="root", owner_path=[], available=True, vision_available=False)
    with pytest.raises(ValidationError): parse_server_message({**raw, **change})


def test_discovery_roundtrip_and_prompt_image_limits():
    request = dict(type="design.assembly.prompt.capabilities", message_id="cap", base_revision=0, ledger_id="root", owner_path=[])
    assert parse_client_message(request).model_dump(mode="json") == request
    response = AssemblyPromptCapabilitiesResponse(message_id="cap", session_id="session", revision=0,
        ledger_id="root", owner_path=[], available=False, vision_available=False, reason="No configured provider")
    assert parse_server_message(response.model_dump(mode="json")) == response
    for images in [[dict(media_type="image/png", data="invalid")], [{}] * 5]:
        with pytest.raises(ValidationError):
            AssemblyPrompt.model_validate(dict(message_id="prompt", base_revision=0, ledger_id="root", owner_path=[],
                scope="assembly_definition", target=dict(kind="definition"), prompt="Use this image", images=images))
