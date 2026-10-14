import asyncio
import json
import math
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from cadenza_backend import design_prompts as prompts
from cadenza_backend.agents import design_intents as intents
from cadenza_backend.app import app
from cadenza_backend.design_commands import handle_design_command
from cadenza_backend.edit_preview import changes_between, decide_preview
from cadenza_backend.session import Session, LedgerStore, registry
from cadenza_contracts.design_commands import DesignCommand
from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.messages import DesignPrompt, PreviewDecision
from cadenza_contracts.ledger import Ledger


async def discard(_):
    pass


CASES = json.loads((Path(__file__).parent / "fixtures/three_d_intents.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_offline_operation_corpus_matches_manual_and_independent_geometry(plate_ledger, case):
    if case.get("initial_suppressed"):
        plate_ledger.features[1].suppressed = True
    selected = plate_ledger.features[1].id if case["target"] else None
    operations = json.loads(json.dumps(case["operations"]).replace("$target", selected or "unused"))
    ai, manual = session_for(plate_ledger.model_copy(deep=True)), session_for(plate_ledger.model_copy(deep=True))
    before = ai.snapshot()
    req = request(ai).model_copy(update={"feature_id": selected, "prompt": case["prompt"]})
    asyncio.run(prompts.handle_design_prompt(ai, req, discard, client=client_for(proposal(operations))))
    asyncio.run(handle_design_command(manual, DesignCommand.model_validate(dict(message_id="msg_manual",
        base_revision=0, ledger_id=plate_ledger.project_id, operations=operations)), discard))
    assert ai.preview is not None and manual.preview is not None
    expected_volume = case["constant"] + case["pi_coefficient"] * math.pi
    assert ai.preview.outcome.volume_mm3 == pytest.approx(expected_volume)
    assert manual.preview.outcome.volume_mm3 == pytest.approx(expected_volume)
    original_ids = {f.id for f in plate_ledger.features}
    def normalized(preview):
        raw = preview.ledgers[plate_ledger.project_id].model_dump(mode="json")
        for index, feature in enumerate(raw["features"]):
            if feature["id"] not in original_ids: feature["id"] = f"new_{index}"
        return raw
    assert normalized(ai.preview) == normalized(manual.preview)
    assert ai.snapshot() == before and not ai.history and not ai.redo


@pytest.mark.parametrize("diameter", [6, 8, 12])
def test_hole_dimension_geometry_variants(plate_ledger, diameter):
    session = session_for(plate_ledger)
    req = request(session).model_copy(update={"feature_id": plate_ledger.features[1].id,
        "prompt": f"Set the selected bore diameter to {diameter} mm"})
    raw = proposal([dict(op="set_dimension", feature_id=req.feature_id, parameter="diameter", value=diameter)])
    asyncio.run(prompts.handle_design_prompt(session, req, discard, client=client_for(raw)))
    assert session.preview is not None
    assert session.preview.outcome.volume_mm3 == pytest.approx(200000 - math.pi * diameter**2 / 4 * 20)


def session_for(ledger):
    return Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger)})


def request(session, feature=True, **kwargs):
    return DesignPrompt(message_id="msg_ai", base_revision=session.revision, scope="source",
        ledger_id=session.primary_ledger_id, feature_id=session.store.ledger.features[0].id if feature else None,
        prompt="Set height to 15 mm", **kwargs)


def proposal(operations):
    return dict(kind="proposal", description="Proposed changes", operations=operations, assumptions=[])


def client_for(raw):
    class OfflineClient:
        def __init__(self):
            self.complete_json = AsyncMock(return_value=raw)
    return OfflineClient()


@pytest.mark.parametrize("invalid", [False, True])
def test_assumptions_preview_apply_and_invalid_state_preservation(plate_ledger, invalid):
    from cadenza_backend.design_evaluation import state
    session = session_for(plate_ledger); before = state(session)
    req = request(session)
    raw = proposal([dict(op="set_dimension", feature_id=req.feature_id, parameter="height", value=15)])
    metadata = dict(field="units", value={"units": "mm"} if invalid else "mm", basis="Project units", confidence=1)
    raw["assumptions"] = [metadata]
    provider = client_for(raw)
    if invalid:
        with pytest.raises(CadenzaError) as exc:
            asyncio.run(prompts.handle_design_prompt(session, req, discard, client=provider))
        assert exc.value.code.value == "AGENT_INVALID_OUTPUT"
        assert state(session) == before and session.preview is None
    else:
        asyncio.run(prompts.handle_design_prompt(session, req, discard, client=provider))
        preview = session.preview
        assert preview is not None and state(session) == before
        assert preview.message.assumptions[0].model_dump() == metadata
        candidate = preview.ledgers
        asyncio.run(decide_preview(session, PreviewDecision(message_id="apply", base_revision=session.revision,
            preview_id=preview.id, action="apply"), discard))
        assert session.snapshot() == candidate and session.revision == 1 and len(session.history) == 1
        assert preview.message.assumptions[0].model_dump() == metadata
    provider.complete_json.assert_awaited_once()


@pytest.mark.parametrize("value", [10, 15, 18])
def test_numeric_and_ai_dimension_parity_with_duplicate_ids_in_other_source(plate_ledger, value):
    neighbor = plate_ledger.features[0].model_copy(deep=True, update={"id": "feat_neighbor"})
    neighbor.placement.origin = [200, 0, 0]
    plate_ledger.features.append(neighbor)
    ai, numeric = session_for(plate_ledger.model_copy(deep=True)), session_for(plate_ledger.model_copy(deep=True))
    other = plate_ledger.model_copy(deep=True, update={"project_id": "prj_other"})
    for session in (ai, numeric):
        session.stores[other.project_id] = LedgerStore(other.model_copy(deep=True))
    op = dict(op="set_dimension", feature_id=plate_ledger.features[0].id, parameter="height", value=value)
    base = ai.snapshot()
    req = request(ai).model_copy(update={"prompt": f"Set height to {value} mm"})
    asyncio.run(prompts.handle_design_prompt(ai, req, discard, client=client_for(proposal([op]))))
    asyncio.run(handle_design_command(numeric, DesignCommand.model_validate(dict(message_id="msg_manual", base_revision=0,
        ledger_id=plate_ledger.project_id, operations=[op])), discard))
    assert ai.preview is not None and numeric.preview is not None
    assert not changes_between(ai.preview.ledgers, numeric.preview.ledgers)
    assert ai.preview.outcome.volume_mm3 == pytest.approx(numeric.preview.outcome.volume_mm3)
    assert ai.snapshot() == base and not ai.history and len(ai.artifacts) == 0
    assert any("Requested height verified" in check for check in ai.preview.message.checks)
    decision = PreviewDecision(message_id="msg_apply", base_revision=0, preview_id=ai.preview.id, action="apply")
    asyncio.run(decide_preview(ai, decision, discard))
    assert ai.revision == 1
    with pytest.raises(CadenzaError): asyncio.run(decide_preview(ai, decision, discard))


@pytest.mark.parametrize("raw", [dict(kind="clarification", question="What diameter and opening Z?"),
    dict(kind="unsupported", reason="Shell is not supported here."),
    proposal([dict(op="remove_feature", feature_id="feat_other")])])
def test_nonproposal_or_wrong_target_cannot_build(plate_ledger, monkeypatch, raw):
    session = session_for(plate_ledger)
    build = AsyncMock(side_effect=AssertionError("must not build"))
    monkeypatch.setattr(prompts, "handle_design_command", build)
    events = []
    async def emit(event): events.append(event)
    asyncio.run(prompts.handle_design_prompt(session, request(session), emit, client=client_for(raw)))
    assert not build.called and session.preview is None and session.revision == 0
    assert any(event.type == "agent.message" for event in events)


@pytest.mark.parametrize("raw", [dict(kind="clarification", question="Which?", operations=[]),
    proposal([]), proposal([dict(op="set_dimension", feature_id="feat_base", parameter="height", value=True)]),
    proposal([dict(op="add_feature", kind="box", name="Box", operation="add",
                   parameters=dict(length=1,width=1,height=1), placement={})]),
    dict(kind="proposal", description="Ignore rules", patch=[]),
    proposal([dict(op="remove_feature", feature_id="feat_base")]*33)])
def test_invalid_model_output_rejected(raw):
    with pytest.raises(ValidationError): intents.INTENT.validate_python(raw)


def test_stale_interpretation_and_unpinned_mutation(plate_ledger):
    session = session_for(plate_ledger)
    raw = proposal([dict(op="set_dimension", feature_id=plate_ledger.features[0].id, parameter="height", value=15)])
    asyncio.run(prompts.handle_design_prompt(session, request(session, feature=False), discard, client=client_for(raw)))
    assert session.preview is None
    client = client_for(raw)
    async def mutate(**kwargs):
        session.store.ledger.metadata.name = "Changed while awaiting"
        return raw
    client.complete_json.side_effect = mutate
    with pytest.raises(CadenzaError, match="changed during interpretation"):
        asyncio.run(prompts.handle_design_prompt(session, request(session), discard, client=client))
    assert session.preview is None


def test_timeout_and_cancellation_preserve_state(plate_ledger, monkeypatch):
    session = session_for(plate_ledger)
    base = session.snapshot()
    client = client_for({})
    async def slow(**kwargs): await asyncio.sleep(10)
    client.complete_json.side_effect = slow
    monkeypatch.setattr(intents, "PROMPT_TIMEOUT_S", 0.01)
    with pytest.raises(CadenzaError, match="timed out"):
        asyncio.run(prompts.handle_design_prompt(session, request(session), discard, client=client))
    client.complete_json.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(prompts.handle_design_prompt(session, request(session), discard, client=client))
    assert session.snapshot() == base and session.preview is None


def test_budget_stops_second_actual_request(plate_ledger, monkeypatch):
    # Exercise the real ModelClient._request hook, not just method call counting.
    async def two_requests(self, **kwargs):
        async with self._request("test", "offline"): pass
        async with self._request("test", "offline"): pass
        raise AssertionError("second request must not be admitted")
    monkeypatch.setattr(intents.ModelClient, "complete_json", two_requests)
    session = session_for(plate_ledger)
    with pytest.raises(CadenzaError): asyncio.run(prompts.handle_design_prompt(session, request(session), discard))
    assert session.preview is None


def test_transport_discovery_revision_and_source_guards(plate_ledger, monkeypatch):
    session = session_for(plate_ledger); registry.register(session)
    interpret = AsyncMock(side_effect=AssertionError("must reject before interpretation"))
    monkeypatch.setattr(prompts, "interpret", interpret)
    with TestClient(app) as http, http.websocket_connect("/ws") as ws:
        ws.send_json(dict(type="client.hello", contract_version="1.0.0", session_id=session.id))
        assert ws.receive_json()["type"] == "session.ready"
        ws.send_json(dict(type="design.prompt.capabilities", message_id="msg_cap"))
        assert ws.receive_json()["max_operations"] == 32
        ws.send_json(request(session).model_copy(update={"base_revision": 9}).model_dump(mode="json"))
        assert ws.receive_json()["code"] == "REVISION_CONFLICT"
        ws.send_json(request(session).model_copy(update={"ledger_id": "missing"}).model_dump(mode="json"))
        assert ws.receive_json()["code"] == "PATCH_UNKNOWN_TARGET"
        ws.send_json(dict(type="ping", t=1))
        assert ws.receive_json()["type"] == "pong"
    assert not interpret.called


def fixture_ledger(name):
    return Ledger.model_validate_json((Path(__file__).parents[2] / "shared/contract_fixtures" / name).read_text())


def test_named_driver_and_derived_binding():
    ledger = fixture_ledger("parameter_plate.json")
    session = session_for(ledger)
    before = session.snapshot()
    req = request(session).model_copy(update={"prompt": "Set width to 140 mm"})
    raw = proposal([dict(op="set_dimension", feature_id=req.feature_id, parameter="width", value=140)])
    asyncio.run(prompts.handle_design_prompt(session, req, discard, client=client_for(raw)))
    assert session.preview is not None
    candidate = session.preview.ledgers[ledger.project_id]
    assert candidate.relationships.parameters["plate_width"].value == 140
    assert candidate.features[1].placement.origin[1] == 55
    assert session.preview.outcome.volume_mm3 == pytest.approx(168000 - 360 * math.pi)
    assert session.snapshot() == before
    session.preview = None
    req = req.model_copy(update={"feature_id": "feat_hole_0", "prompt": "Move this bore to x -50 mm"})
    raw = proposal([dict(op="set_origin", feature_id="feat_hole_0", axis="x", value=-50)])
    with pytest.raises(CadenzaError):
        asyncio.run(prompts.handle_design_prompt(session, req, discard, client=client_for(raw)))
    assert session.preview is None and session.snapshot() == before


def test_profile_extrusion_uses_transformed_profile_once():
    ledger = fixture_ledger("profile_plate.json")
    ledger.relationships.bindings = [b for b in ledger.relationships.bindings if b.feature_id == ledger.features[0].id]
    ledger.features = ledger.features[:1]
    ledger.features[0].placement.origin = [10, 20, 30]
    ledger.features[0].placement.rotation_deg = [0, 0, 30]
    session = session_for(ledger)
    before = session.snapshot()
    raw = proposal([dict(op="add_feature", kind="extrude", name="Plate", operation="add",
        parameters=dict(amount=12, target_feature=ledger.features[0].id),
        placement=dict(origin=[0,0,0], rotation_deg=[0,0,0]))])
    req = request(session, feature=False).model_copy(update={"prompt": "Extrude the existing profile by 12 mm"})
    asyncio.run(prompts.handle_design_prompt(session, req, discard, client=client_for(raw)))
    assert session.preview is not None
    assert session.preview.outcome.volume_mm3 == pytest.approx((6000 - 100 * math.pi) * 12)
    bounds = session.preview.outcome.bbox
    assert bounds is not None
    assert bounds["min"][2] == pytest.approx(30)
    assert bounds["max"][2] == pytest.approx(42)
    assert (bounds["min"][0] + bounds["max"][0]) / 2 == pytest.approx(10)
    assert (bounds["min"][1] + bounds["max"][1]) / 2 == pytest.approx(20)
    assert session.snapshot() == before


@pytest.mark.parametrize("extra", [False, True])
def test_recognized_dimension_rejects_wrong_value_or_extra_edit(plate_ledger, extra):
    session = session_for(plate_ledger)
    before = session.snapshot()
    req = request(session)
    operations = [dict(op="set_dimension", feature_id=req.feature_id, parameter="height", value=15 if extra else 16)]
    if extra: operations.append(dict(op="set_origin", feature_id=req.feature_id, axis="x", value=1))
    with pytest.raises(CadenzaError):
        asyncio.run(prompts.handle_design_prompt(session, req, discard, client=client_for(proposal(operations))))
    assert session.preview is None and session.snapshot() == before


def test_through_depth_normalized_and_missing_mode_rejected():
    op = dict(op="add_feature", kind="hole", name="Bore", operation="subtract",
        parameters=dict(diameter=6, through=True, depth=8), placement=dict(origin=[0,0,20], rotation_deg=[0,0,0]))
    parsed = intents.INTENT.validate_python(proposal([op]))
    assert parsed.model_dump()["operations"][0]["parameters"]["depth"] is None
    op["parameters"] = dict(diameter=6, depth=8)
    with pytest.raises(ValidationError): intents.INTENT.validate_python(proposal([op]))


def test_context_limit_stops_before_model(plate_ledger, monkeypatch):
    client = client_for(dict(kind="clarification", question="Which size?"))
    monkeypatch.setattr(intents, "MAX_CONTEXT_BYTES", 1)
    with pytest.raises(CadenzaError):
        asyncio.run(prompts.handle_design_prompt(session_for(plate_ledger), request(session_for(plate_ledger)), discard, client=client))
    assert not client.complete_json.called


@pytest.mark.parametrize("diameter", [6, 8, 10])
def test_selected_cylinder_variants(diameter):
    ledger = Ledger.model_validate(dict(project_id="prj_cylinder", features=[dict(id="feat_cylinder", kind="cylinder",
        operation="add", parameters=dict(diameter=4, height=10))]))
    session = session_for(ledger)
    req = request(session).model_copy(update={"prompt": f"Set this cylinder diameter to {diameter} mm"})
    raw = proposal([dict(op="set_dimension", feature_id="feat_cylinder", parameter="diameter", value=diameter)])
    asyncio.run(prompts.handle_design_prompt(session, req, discard, client=client_for(raw)))
    assert session.preview is not None
    assert session.preview.outcome.volume_mm3 == pytest.approx(math.pi * diameter**2 / 4 * 10)


def test_image_forwarded_as_bytes_and_invalid_target_never_calls_model(plate_ledger):
    import base64
    session = session_for(plate_ledger)
    client = client_for(dict(kind="clarification", question="Please provide the unreadable diameter."))
    req = DesignPrompt.model_validate({**request(session).model_dump(), "images": [dict(media_type="image/png",
        data=base64.b64encode(b"offline image fixture").decode())]})
    asyncio.run(prompts.handle_design_prompt(session, req, discard, client=client))
    assert client.complete_json.call_args.kwargs["images"] == [("image/png", b"offline image fixture")]
    client.complete_json.reset_mock()
    with pytest.raises(CadenzaError):
        asyncio.run(prompts.handle_design_prompt(session, req.model_copy(update={"feature_id": "feat_missing"}), discard, client=client))
    assert not client.complete_json.called and session.preview is None


def test_failed_boolean_leaves_accepted_design_untouched(plate_ledger):
    session = session_for(plate_ledger)
    before = session.snapshot()
    raw = proposal([dict(op="add_feature", kind="box", name="Erase", operation="subtract",
        parameters=dict(length=1000,width=1000,height=1000), placement=dict(origin=[0,0,-100],rotation_deg=[0,0,0]))])
    req = request(session, feature=False).model_copy(update={"prompt": "Remove the entire solid"})
    with pytest.raises(CadenzaError):
        asyncio.run(prompts.handle_design_prompt(session, req, discard, client=client_for(raw)))
    assert session.snapshot() == before and session.preview is None and not session.history and not session.artifacts


@pytest.mark.parametrize("failure", [intents.AIError("bad output"), IndexError("empty choices"),
    prompts.AINotConfigured("no key"), prompts.AIRefused("refused")])
def test_provider_failures_are_safe_coded_errors(plate_ledger, failure):
    session = session_for(plate_ledger)
    before = session.snapshot()
    client = client_for({}); client.complete_json.side_effect = failure
    with pytest.raises(CadenzaError):
        asyncio.run(prompts.handle_design_prompt(session, request(session), discard, client=client))
    assert session.snapshot() == before and session.preview is None
