import pytest
from pydantic import ValidationError
from cadenza_contracts.feature_capabilities import CapabilitiesRequest, CapabilitiesResponse, RevolveCapability, feature_capabilities
from cadenza_contracts.messages import parse_client_message, parse_server_message, dump_message


def test_capabilities_roundtrip_and_promoted_scope():
    request = parse_client_message(dict(type="design.capabilities", message_id="query"))
    assert isinstance(request, CapabilitiesRequest)
    response = CapabilitiesResponse(message_id=request.message_id, session_id="session", features=feature_capabilities())
    assert parse_server_message(dump_message(response)) == response
    kinds = {c.kind: c for c in response.features}
    assert set(kinds) == {"box", "cylinder", "hole", "extrude"}
    assert kinds["hole"].create_operations == ["subtract"]
    assert kinds["extrude"].origin_axes == []
    assert kinds["box"].dimensions == ["height", "length", "width"]
    # Returned metadata is fresh, not mutable global state.
    response.features[0].dimensions.clear()
    assert feature_capabilities()[0].dimensions


@pytest.mark.parametrize("extra", [{"features": []}, {"scope": "instance"}, {"operations": []}])
def test_client_cannot_supply_capabilities(extra):
    with pytest.raises(ValidationError):
        parse_client_message(dict(type="design.capabilities", message_id="query", **extra))


def test_response_rejects_unpromoted_kind_or_units():
    response = CapabilitiesResponse(message_id="q", session_id="s", features=feature_capabilities()).model_dump()
    response["length_unit"] = "inches"
    with pytest.raises(ValidationError):
        parse_server_message(response)
    response["length_unit"] = "mm"
    response["features"][0]["kind"] = "shell"
    with pytest.raises(ValidationError):
        parse_server_message(response)


def test_revolve_version_roundtrip_and_legacy_wire_shape():
    response = CapabilitiesResponse(message_id="q", session_id="s", version=2,
        features=feature_capabilities(), revolve=RevolveCapability())
    assert parse_server_message(dump_message(response)) == response
    legacy = CapabilitiesResponse(message_id="q", session_id="s", features=feature_capabilities())
    assert "revolve" not in legacy.model_dump(mode="json")
    for bad in [True, "2", 2.0, 3]:
        with pytest.raises(ValidationError):
            CapabilitiesRequest.model_validate(dict(message_id="q", version=bad))
    for version, revolve in [(1, RevolveCapability()), (2, None)]:
        with pytest.raises(ValidationError):
            CapabilitiesResponse(message_id="q", session_id="s", version=version,
                features=feature_capabilities(), revolve=revolve)
