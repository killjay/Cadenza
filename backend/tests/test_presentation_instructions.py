"""Offline checks of the actual advertised explanation instructions, not live prose quality."""
import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from cadenza_backend.agents import assembler, assembly_intents, design_intents, draftsman, machinist
from cadenza_backend.agents.model_client import ModelClient


def test_explanations_describe_proposals_without_changing_machine_fields():
    client = SimpleNamespace(complete_json=AsyncMock(return_value={"summary": "Preview: place the parts", "components": [], "joints": []}))
    result = asyncio.run(assembler.run_assembler("Place the parts", "No sources", cast(ModelClient, client)))
    systems = [machinist.MACHINIST_SYSTEM, draftsman.draftsman_system(False), draftsman.draftsman_system(True),
               design_intents.CARD.system_prompt, assembly_intents.CARD.system_prompt,
               client.complete_json.call_args.kwargs["agent_card"].system_prompt]
    for prompt in systems:
        assert "Keep exact machine fields in structured operations unchanged" in prompt
        assert "acceptance happens only after the user clicks Apply" in prompt
        assert "Do not expose ledger IDs" in prompt
        assert "uncertainty, omissions and shared-part impact" in prompt
    assert result.summary == "Preview: place the parts"
    assert result.components == [] and result.joints == []
