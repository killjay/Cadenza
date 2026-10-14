import asyncio
import json
from pathlib import Path
from typing import Literal

import pytest

from cadenza_backend import sketch_prompts, sketch_intents
from cadenza_backend.agents.model_client import ModelClient, AIError
from cadenza_backend.agents.sketch_editor import SketchIntent
from cadenza_backend.session import Session
from cadenza_backend.sketch_intents import compile_intent
from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.sketch_draft import DraftPrompt, DraftProposalDecision
from .test_sketch_drafts import start, update, action, decision, ignore, plate
from cadenza_backend.edit_preview import decide_preview


def create_actions():
    return [
        {"kind": "create", "key": "outer", "entity": {"kind": "rectangle", "width": 100, "height": 60}},
        {"kind": "create", "key": "left", "entity": {"kind": "circle", "operation": "subtract", "diameter": 6}},
        {"kind": "margin", "entity": "$left", "edge": "left", "distance": 15, "driver_name": "left_margin"},
        {"kind": "create", "key": "right", "entity": {"kind": "circle", "operation": "subtract", "diameter": 6}},
        {"kind": "margin", "entity": "$right", "edge": "right", "distance": 15, "driver_name": "right_margin"},
        {"kind": "extrusion", "amount": 8},
    ]


def intent(actions): return SketchIntent.model_validate({"summary": "Sketch changes", "actions": actions})


def prompt_request(session, **kwargs):
    assert session.draft is not None
    return DraftPrompt(message_id="prompt", base_revision=session.revision, draft_id=session.draft.state.id,
        draft_revision=session.draft.state.revision, prompt="Create a plate", **kwargs)


def choose(session, action: Literal["update", "cancel"] = "update", **kwargs):
    assert session.draft is not None and session.draft.proposal is not None
    return DraftProposalDecision(message_id="choice", base_revision=session.revision, draft_id=session.draft.state.id,
        draft_revision=session.draft.state.revision, proposal_id=session.draft.proposal.message.proposal_id,
        action=action, **kwargs)


def seed():
    session = Session()
    start(session)
    assert session.draft is not None
    session.draft.state, _ = compile_intent(session.draft, intent(create_actions()))
    return session


def test_create_resize_margins_and_driver_semantics():
    session = seed()
    assert session.draft is not None
    before = session.draft.state.model_copy(deep=True)
    assert before.revision == 1 and before.solution is not None
    outer, left, right = before.solution.entities
    assert [left.x, right.x] == [-35, 35]
    candidate, _ = compile_intent(session.draft, intent([
        {"kind": "dimension", "entity": outer.id, "parameter": "width", "value": 140},
        {"kind": "dimension", "entity": left.id, "parameter": "diameter", "value": 8}]))
    assert candidate.solution is not None
    assert [(e.x, e.diameter) for e in candidate.solution.entities[1:]] == [(-55, 8), (55, 6)]
    assert session.draft.state == before and session.revision == 0
    session.draft.state = candidate
    changed, _ = compile_intent(session.draft, intent([{"kind": "driver", "parameter": "left_margin", "value": 20}]))
    assert changed.solution is not None and changed.solution.entities[1].x == -50


def test_unbound_add_move_centre_remove():
    session = plate()
    start(session, "feat_profile")
    assert session.draft is not None
    candidate, _ = compile_intent(session.draft, intent([
        {"kind": "create", "key": "new", "entity": {"kind": "circle", "operation": "subtract", "diameter": 6, "x": 30, "y": 0}}]))
    entity = candidate.definition.entities[-1].id
    session.draft.state = candidate
    candidate, _ = compile_intent(session.draft, intent([{"kind": "dimension", "entity": entity, "parameter": "y", "value": 10}]))
    assert candidate.solution is not None and candidate.solution.entities[-1].y == 10
    session.draft.state = candidate
    removed, _ = compile_intent(session.draft, intent([{"kind": "remove", "entity": entity}]))
    assert len(removed.definition.entities) == 2
    # Centring replaces explicit unbound position constraints, retaining a shared-centre relation.
    session = Session(); start(session)
    assert session.draft is not None
    centred, _ = compile_intent(session.draft, intent([
        create_actions()[0], create_actions()[1], {"kind": "centre", "entity": "$left", "other": "$outer"}]))
    assert centred.solution is not None and centred.solution.entities[1].x == 0
    assert any(c.kind == "coincident" for c in centred.definition.constraints)


@pytest.mark.parametrize("actions", [
    [{"kind": "remove", "entity": "ent_missing"}],
    [{"kind": "dimension", "entity": "ent_plate", "parameter": "width", "value": -10}],
    [{"kind": "remove", "entity": "ent_plate"}],
    [{"kind": "remove", "entity": "ent_bore"}],
    [{"kind": "margin", "entity": "ent_bore", "edge": "left", "distance": 15, "driver_name": "margin"}],
    [{"kind": "extrusion", "amount": 10}],
    [{"kind": "dimension", "entity": "ent_bore", "parameter": "diameter", "value": 200}],
    [{"kind": "dimension", "entity": "ent_bore", "parameter": "diameter", "value": 6},
     {"kind": "dimension", "entity": "ent_bore", "parameter": "diameter", "value": 8}],
])
def test_unsafe_intents_preserve_draft(actions):
    session = plate(); start(session, "feat_profile")
    assert session.draft is not None
    before = session.draft.state.model_copy(deep=True)
    with pytest.raises(ValueError): compile_intent(session.draft, intent(actions))
    assert session.draft.state == before


def test_independent_checks_detect_unrelated_mutation(monkeypatch):
    session = seed()
    assert session.draft is not None
    original = sketch_intents.update_candidate
    def corrupt(*args):
        state = original(*args)
        state.relationships.parameters["right_margin"].value = 18
        return state
    monkeypatch.setattr(sketch_intents, "update_candidate", corrupt)
    with pytest.raises(ValueError, match="Unrelated named"):
        compile_intent(session.draft, intent([{"kind": "driver", "parameter": "left_margin", "value": 20}]))


def test_proposal_cancel_update_finish_apply_and_no_ai_on_accept(monkeypatch):
    session = Session(); start(session)
    calls = []
    async def complete(self, **kwargs):
        calls.append(kwargs)
        return intent(create_actions()).model_dump()
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    assert session.draft is not None
    before = session.draft.state.model_copy(deep=True)
    asyncio.run(sketch_prompts.handle_sketch_prompt(session, prompt_request(session), ignore))
    assert session.draft.state == before and session.draft.proposal is not None
    assert session.draft.proposal.message.changes and not session.store.ledger.features
    asyncio.run(sketch_prompts.handle_sketch_prompt(session, choose(session, "cancel"), ignore))
    assert session.draft.state == before and session.draft.proposal is None
    asyncio.run(sketch_prompts.handle_sketch_prompt(session, prompt_request(session), ignore))
    choice = choose(session)
    asyncio.run(sketch_prompts.handle_sketch_prompt(session, choice, ignore))
    assert session.draft.state.revision == 1 and session.revision == 0
    with pytest.raises(CadenzaError): asyncio.run(sketch_prompts.handle_sketch_prompt(session, choice, ignore))
    assert len(calls) == 2 and not calls[0]["agent_card"].tools
    action(session, "finish")
    asyncio.run(decide_preview(session, decision(session), ignore))
    assert session.revision == 1 and session.draft is None
    assert session.last_geometry and session.last_geometry.revision == 1


@pytest.mark.parametrize("invalidate", ["manual", "expiry", "selection", "discard"])
def test_stale_proposal_cannot_update(monkeypatch, invalidate):
    session = seed()
    assert session.draft is not None
    async def complete(*args, **kwargs): return intent([{"kind": "driver", "parameter": "left_margin", "value": 20}]).model_dump()
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    asyncio.run(sketch_prompts.handle_sketch_prompt(session, prompt_request(session), ignore))
    choice = choose(session)
    if invalidate == "manual": update(session, {"op": "set_driver", "parameter": "left_margin", "value": 18})
    elif invalidate == "expiry":
        assert session.draft.proposal is not None
        session.draft.proposal.created -= 901
    elif invalidate == "selection": choice.selected_entity_id = session.draft.state.definition.entities[0].id
    else: action(session, "discard")
    before = session.draft.state.model_copy(deep=True) if session.draft else None
    with pytest.raises(CadenzaError): asyncio.run(sketch_prompts.handle_sketch_prompt(session, choice, ignore))
    assert (session.draft.state if session.draft else None) == before


def test_clarification_reply_is_revision_bound(monkeypatch):
    session = seed()
    assert session.draft is not None
    calls = []
    async def complete(self, **kwargs):
        calls.append(json.loads(kwargs["user"]))
        return {"summary": "Need a size", "question": "What diameter in mm?", "actions": []}
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    asyncio.run(sketch_prompts.handle_sketch_prompt(session, prompt_request(session), ignore))
    proposal = session.draft.proposal
    assert proposal is not None and proposal.candidate is None
    with pytest.raises(CadenzaError): asyncio.run(sketch_prompts.handle_sketch_prompt(session, choose(session), ignore))
    asyncio.run(sketch_prompts.handle_sketch_prompt(session, prompt_request(session, reply_to=proposal.message.proposal_id), ignore))
    assert "What diameter in mm?" in calls[-1]["request"]
    update(session, {"op": "set_driver", "parameter": "left_margin", "value": 18})
    with pytest.raises(CadenzaError):
        asyncio.run(sketch_prompts.handle_sketch_prompt(session, prompt_request(session, reply_to=proposal.message.proposal_id), ignore))


@pytest.mark.parametrize("failure", ["schema", "provider", "timeout", "late"])
def test_failed_interpretations_do_not_publish(monkeypatch, failure):
    session = seed()
    assert session.draft is not None
    before = session.draft.state.model_copy(deep=True)
    async def complete(*args, **kwargs):
        if failure == "schema": return {"patch": []}
        if failure == "provider": raise AIError("Offline")
        if failure == "timeout": await asyncio.sleep(1)
        if failure == "late":
            assert session.draft is not None
            session.draft.state.revision += 1
        return intent([{"kind": "driver", "parameter": "left_margin", "value": 20}]).model_dump()
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    if failure == "timeout": monkeypatch.setattr(sketch_prompts, "PROMPT_TIMEOUT_S", 0.01)
    with pytest.raises(CadenzaError): asyncio.run(sketch_prompts.handle_sketch_prompt(session, prompt_request(session), ignore))
    assert session.draft.proposal is None
    if failure == "late": before.revision += 1
    assert session.draft.state == before


CORPUS = json.loads((Path(__file__).parent / "fixtures/sketch_prompt_corpus.json").read_text())


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda case: case["id"])
def test_reference_corpus_compilation(case):
    # Golden responses exercise the real proposal path, not live language quality.
    from cadenza_backend.sketch_evaluation import evaluate_case, FixtureClient
    responses = [case["response"], *([case["followup_response"]] if "followup_response" in case else [])]
    result = asyncio.run(evaluate_case(CORPUS["contexts"][case["context"]], case, FixtureClient(responses), kernel=False))
    assert result["passed"], result
