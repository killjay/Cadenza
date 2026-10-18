import pytest
from pydantic import ValidationError
from cadenza_contracts.messages import DesignPrompt, parse_client_message, parse_server_message


BASE = dict(type="design.prompt", message_id="msg_ai", base_revision=0, ledger_id="prj_part",
            scope="source", prompt="Set height to 30 mm")


@pytest.mark.parametrize("change", [dict(base_revision=True), dict(base_revision="0"), dict(base_revision=-1),
    dict(scope="instance"), dict(prompt=" "), dict(prompt="x" * 4001), dict(ledger_id=""),
    dict(feature_id="face_7"), dict(patch=[]), dict(images=[{}]), dict(active_ledger_ids=[])])
def test_design_prompt_rejects_invalid_envelope(change):
    with pytest.raises(ValidationError):
        parse_client_message({**BASE, **change})


def test_prompt_contract_roundtrip_and_discovery():
    assert isinstance(parse_client_message(BASE), DesignPrompt)
    request = DesignPrompt.model_validate(BASE)
    assert parse_client_message(request.model_dump_json()) == request
    assert parse_client_message(dict(type="design.prompt.capabilities", message_id="msg_cap"))
    assert parse_server_message(dict(type="design.prompt.capabilities", message_id="msg_cap",
        session_id="ses_test", version=1, scope="source", max_operations=32))


@pytest.mark.parametrize("bad", [None, "source", "feature", "nan", "zero", "extra", "string", "path", "mode"])
def test_source_face_context_is_strict_and_source_local(bad):
    face: dict = dict(mode="edit_feature", point=[0, 0, 15], normal=[0, 0, 1], component_path=["comp_base"],
        reference=dict(ledger_id="prj_part", source_ledger_id="prj_part", feature_id="feat_base",
            feature_kind="box", operation="add", role="top_face"))
    raw = {**BASE, "feature_id": "feat_base", "face": face}
    if bad == "source": raw["ledger_id"] = "prj_other"
    if bad == "feature": raw["feature_id"] = "feat_other"
    if bad == "nan": face["point"][0] = float("nan")
    if bad == "zero": face["normal"] = [0, 0, 0]
    if bad == "extra": face["face_hash"] = 42
    if bad == "string": face["point"][0] = "0"
    if bad == "path": face["component_path"] = ["comp_a"] * 9
    if bad == "mode": face["mode"] = "add_on_face"
    if bad:
        with pytest.raises(ValidationError): DesignPrompt.model_validate(raw)
    else:
        request = DesignPrompt.model_validate(raw)
        assert parse_client_message(request.model_dump_json()) == request


def test_interference_roundtrip_keeps_legacy_error_detail():
    from cadenza_contracts.messages import ErrorMessage
    error = ErrorMessage.model_validate(dict(code="GEOMETRY_KERNEL_FAILURE", message="Overlap", detail="native detail",
        interference=dict(definition_id="prj_inner", component_a=["comp_a"], component_b=["comp_b"], volume_mm3=12.0)))
    assert parse_server_message(error.model_dump_json()) == error
    assert error.detail == "native detail"
