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
