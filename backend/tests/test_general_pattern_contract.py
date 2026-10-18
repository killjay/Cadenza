"""Offline reproduction of the manual bolt-circle incident and route parity."""
import asyncio
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from cadenza_backend import graph, orchestrator
from cadenza_backend.agents.draftsman import draftsman_system, run_draftsman
from cadenza_backend.agents.machinist import MACHINIST_SYSTEM
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.design_prompts import handle_design_prompt
from cadenza_backend.edit_preview import decide_preview
from cadenza_backend.session import LedgerStore, Session
from cadenza_contracts.errors import CadenzaError, ErrorCode
from cadenza_contracts.ledger import CircularPatternFeature, CircularPatternParameters, Ledger
from cadenza_contracts.messages import DesignPrompt, PreviewDecision
from cadenza_contracts.patch import PatchOp
from cadenza_backend.design import stage_operations


PROMPT = "Pattern the 6 mm hole at X=30, Y=0: six total around Z through 0,0 over 360 degrees. Preserve the 12 mm centre hole."


def plate():
    return Ledger.model_validate(dict(features=[
        dict(id="feat_stock", kind="box", parameters=dict(length=100, width=60, height=15)),
        dict(id="feat_center", kind="hole", parameters=dict(diameter=12, through=True), placement=dict(origin=[0, 0, 15])),
        dict(id="feat_seed", kind="hole", parameters=dict(diameter=6, through=True), placement=dict(origin=[30, 0, 15])),
    ]))


def pattern() -> dict[str, Any]:
    return dict(id="feat_pattern", name="Bolt circle", kind="circular_pattern", operation="subtract",
        parameters=dict(target_feature="feat_seed", count=6, radius=30, sweep_angle=360, axis=[0, 0, 1]),
        placement=dict(origin=[0, 0, 0], rotation_deg=[0, 0, 0]),
        ai_context=dict(original_intent=PROMPT, assumptions=[]))


def session_for(ledger):
    return Session(primary_ledger_id=ledger.project_id, stores={ledger.project_id: LedgerStore(ledger)})


def client_for(ledger, feature):
    async def complete(**kwargs):
        if kwargs["stage"] == "architect":
            return dict(is_edit=True, is_assembly=False, summary="Pattern the existing hole", bom=[])
        assert kwargs["stage"] == "machinist"
        return dict(summary="Preview six outer holes; preserve the centre hole", patch=[
            dict(op="add", path="/features/-", ledger_id=ledger.project_id, value=feature)])
    return SimpleNamespace(complete_json=AsyncMock(side_effect=complete))


@pytest.mark.parametrize("system", [MACHINIST_SYSTEM, draftsman_system()], ids=["machinist", "draftsman"])
def test_advertised_circular_parameters_include_contract_requirements(system):
    line = next(line for line in system.splitlines() if "circular_pattern ->" in line)
    required = {name for name, field in CircularPatternParameters.model_fields.items() if field.is_required()}
    assert required <= set(re.findall(r"\b[a-z_]+\b", line))


@pytest.mark.parametrize("system", [MACHINIST_SYSTEM, draftsman_system()], ids=["machinist", "draftsman"])
def test_pattern_instruction_example_passes_real_candidate_admission(system):
    example = re.search(r"CIRCULAR CREATION EXAMPLE\n```json\n(.*?)\n```", system, re.S)
    assert example is not None, "Give both agents a contract-valid circular creation example"
    feature = dict(id="feat_example", name="Bolt circle", kind="circular_pattern", **json.loads(example[1]))
    CircularPatternFeature.model_validate(feature)
    ledger = plate()
    _, candidates = stage_operations(session_for(ledger), [PatchOp(op="add", path="/features/-",
        ledger_id=ledger.project_id, value=feature)])
    assert candidates[ledger.project_id].features[:3] == ledger.features


def test_initial_generation_resolves_pattern_seed_name_without_inventing_ids():
    raw_features = plate().model_dump(mode="json")["features"] + [pattern()]
    for feature in raw_features:
        feature["name"] = feature.pop("id")
        feature.pop("ai_context", None)
    raw_features[-1]["parameters"]["target_feature"] = "Offset hole"
    raw_features[2]["name"] = "Offset hole"
    client = SimpleNamespace(complete_json=AsyncMock(return_value=dict(
        summary="Plate with six outer holes and a centre hole", features=raw_features)))
    features, _ = asyncio.run(run_draftsman(PROMPT, client=cast(ModelClient, client)))
    circle = features[-1]
    assert isinstance(circle, CircularPatternFeature)
    assert circle.parameters.target_feature == features[2].id
    assert circle.operation == "subtract" and circle.parameters.radius == 30
    empty = Ledger()
    _, candidates = stage_operations(session_for(empty), [PatchOp(op="add", path="/features/-",
        ledger_id=empty.project_id, value=feature.model_dump(mode="json")) for feature in features])
    assert candidates[empty.project_id].features == features
    assert "CIRCULAR CREATION EXAMPLE" in client.complete_json.call_args.kwargs["agent_card"].system_prompt


@pytest.mark.parametrize("failure", ["both", "radius", "warnings", "center_z", "operation", "radius_mismatch"])
def test_general_pattern_invalid_output_never_builds_or_mutates(monkeypatch, failure):
    ledger = plate()
    session = session_for(ledger)
    feature = pattern()
    if failure in ("both", "radius"):
        feature["parameters"].pop("radius")
    if failure in ("both", "warnings"):
        feature["ai_context"]["warnings"] = [dict(field="edge_wall", value=1.02, basis="Review edge clearance")]
    if failure == "center_z": feature["placement"]["origin"][2] = 15
    if failure == "operation": feature["operation"] = "add"
    if failure == "radius_mismatch": feature["parameters"]["radius"] = 25
    build = AsyncMock(side_effect=AssertionError("Invalid candidate must not reach geometry"))
    monkeypatch.setattr(graph, "build_design", build)
    before = session.snapshot()
    events = []
    async def emit(event): events.append(event)
    client = client_for(ledger, feature)
    asyncio.run(orchestrator.handle_prompt(session, "msg_invalid", PROMPT, [], emit,
        client=cast(ModelClient, client), preview=True))
    errors = [e for e in events if e.type == "error"]
    assert len(errors) == 1 and errors[0].code in (ErrorCode.LEDGER_INVALID, ErrorCode.PATCH_INVALID)
    build.assert_not_awaited()
    assert client.complete_json.await_count == 2  # Architect + Machinist; no retry.
    assert session.snapshot() == before and session.revision == 0
    assert not session.history and not session.redo and session.preview is None
    assert not session.artifacts._items and session.last_geometry is None


@pytest.mark.parametrize("route", ["general", "targeted"])
def test_bolt_circle_real_step_preview_cancel_apply(route, tmp_path):
    ledger = plate()
    session = session_for(ledger)
    before = session.snapshot()
    events = []
    async def emit(event): events.append(event)
    general = client_for(ledger, pattern())
    targeted = SimpleNamespace(complete_json=AsyncMock(return_value=dict(kind="proposal",
        description="Six holes including the seed", assumptions=[], operations=[dict(
            op="add_circular_pattern", name="Bolt circle", target_feature="feat_seed", count=6, center_xy=[0, 0])])))

    async def propose():
        if route == "general":
            await orchestrator.handle_prompt(session, "msg_circle", PROMPT, [], emit,
                client=cast(ModelClient, general), preview=True)
        else:
            await handle_design_prompt(session, DesignPrompt(message_id="msg_circle", base_revision=0,
                ledger_id=ledger.project_id, feature_id="feat_seed", scope="source", prompt=PROMPT), emit, client=targeted)

    asyncio.run(propose())
    preview = session.preview
    assert preview is not None, [e.model_dump() for e in events if e.type == "error"]
    assert session.snapshot() == before and not session.history
    candidate = preview.ledgers[ledger.project_id]
    assert candidate.features[:3] == ledger.features
    assert len(candidate.features) == 4
    assert any("6 complete bores" in check for check in preview.message.checks)
    expected_volume = 100 * 60 * 15 - math.pi * (6**2 + 6 * 3**2) * 15
    assert preview.outcome.volume_mm3 == pytest.approx(expected_volume)
    assert preview.outcome.step is not None and not preview.outcome.stub
    step = tmp_path / "pattern.step"
    step.write_bytes(preview.outcome.step)
    inspector = Path(__file__).resolve().parents[1] / "scripts" / "inspect_evaluation_step.py"
    measured = json.loads(subprocess.run([sys.executable, str(inspector), str(step)],
        capture_output=True, text=True, check=True, timeout=60).stdout)
    assert measured["solids"] == 1
    assert measured["volume"] == pytest.approx(expected_volume)
    assert measured["minimum"] == pytest.approx([-50, -30, 0])
    assert measured["maximum"] == pytest.approx([50, 30, 15])
    bores = measured["bores"]
    assert len(bores) == 7
    expected = [(0, 0, 6)] + [(30 * math.cos(i * math.pi / 3), 30 * math.sin(i * math.pi / 3), 3) for i in range(6)]
    for x, y, radius in expected:
        matches = [b for b in bores if (b["x"], b["y"], b["radius"]) == pytest.approx((x, y, radius), abs=1e-6)]
        assert len(matches) == 1
        assert (matches[0]["z_min"], matches[0]["z_max"]) == pytest.approx((0, 15))
    asyncio.run(decide_preview(session, PreviewDecision(message_id="msg_cancel", base_revision=0,
        preview_id=preview.id, action="cancel"), emit))
    assert session.snapshot() == before and not session.history and session.preview is None
    asyncio.run(propose())
    assert session.preview is not None
    decision = PreviewDecision(message_id="msg_apply", base_revision=0, preview_id=session.preview.id, action="apply")
    asyncio.run(decide_preview(session, decision, emit))
    accepted = session.snapshot()
    with pytest.raises(CadenzaError): asyncio.run(decide_preview(session, decision, emit))
    assert session.snapshot() == accepted and session.revision == 1 and len(session.history) == 1
    assert session.store.ledger.features[:3] == ledger.features
