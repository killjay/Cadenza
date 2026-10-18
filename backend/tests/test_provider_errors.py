"""Provider failures preserve state and their classification across prompt routes."""
import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from cadenza_backend import orchestrator, design_prompts, part_commands, assembly_prompts, sketch_prompts
from cadenza_backend.agents.model_client import AIError, AIProviderError, ModelClient
from cadenza_backend.session import Session
from cadenza_contracts.errors import CadenzaError, ErrorCode
from cadenza_contracts.messages import PartPrompt
from .test_3d_intents import session_for, request as feature_request
from .test_assembly_prompts import established, request as assembly_request
from .test_sketch_prompts import seed, prompt_request


@pytest.mark.parametrize("route,failure", [(route, failure)
    for route in ("general", "feature", "part", "assembly", "sketch")
    for failure in ("unavailable", "timeout", "invalid", "deadline")
    if (route, failure) != ("general", "deadline")])
def test_prompt_failure_classification_and_unchanged_state(monkeypatch, plate_ledger, route, failure):
    code = {"unavailable": ErrorCode.AGENT_UNAVAILABLE, "timeout": ErrorCode.AGENT_TIMEOUT,
        "invalid": ErrorCode.AGENT_INVALID_OUTPUT, "deadline": ErrorCode.AGENT_TIMEOUT}[failure]
    error = (AIError("invalid fixture output") if failure == "invalid" else
             asyncio.TimeoutError() if failure == "deadline" else AIProviderError("Safe provider failure", code))
    provider = SimpleNamespace(complete_json=AsyncMock(side_effect=error))
    events = []
    async def emit(message): events.append(message)
    session = Session()
    component_id = ""
    if route == "feature": session = session_for(plate_ledger)
    if route == "assembly":
        session, (component, _) = established()
        component_id = component.id
    if route == "sketch":
        session = seed()
        monkeypatch.setattr(ModelClient, "complete_json", provider.complete_json)
    before = session.snapshot()
    revision, history = session.revision, list(session.history)
    draft_before = session.draft.state.model_copy(deep=True) if session.draft else None

    async def run():
        if route == "general":
            await orchestrator.handle_prompt(session, "failure", "Create a plate", [], emit, client=cast(ModelClient, provider))
        elif route == "feature":
            await design_prompts.handle_design_prompt(session, feature_request(session), emit, client=provider)
        elif route == "part":
            await part_commands.prompt_part(session, PartPrompt(message_id="failure", base_revision=session.revision,
                name="New part", prompt="Create a plate"), emit, client=provider)
        elif route == "assembly":
            await assembly_prompts.handle_assembly_prompt(session, assembly_request(session, component_id), emit, client=provider)
        else:
            await sketch_prompts.handle_sketch_prompt(session, prompt_request(session), emit)

    if route == "general":
        asyncio.run(run())
        errors = [e for e in events if e.type == "error"]
        assert len(errors) == 1 and errors[0].code == code
    else:
        with pytest.raises(CadenzaError) as caught: asyncio.run(run())
        assert caught.value.code == code
    provider.complete_json.assert_awaited_once()
    assert session.snapshot() == before and session.revision == revision and session.history == history
    assert session.preview is None
    if session.draft:
        assert session.draft.state == draft_before and session.draft.proposal is None
