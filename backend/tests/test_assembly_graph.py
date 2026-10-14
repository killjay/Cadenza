import asyncio
import json
import time

import pytest

from cadenza_backend import graph
from cadenza_backend.agents import librarian
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.contracts_bridge import ErrorCode, Ledger
from cadenza_backend.geometry_bridge import BuildOutcome
from cadenza_backend.session import Session, LedgerStore
from cadenza_backend.wire import AgentMessage


BOLT = {"sku": "ISO-4017-M10x20", "type": "Hexagon Head Bolt",
        "parameters": {"thread": "M10", "length": 20}}


def state_for(session, emit):
    return graph.AgentState(session=session, message_id="msg_assembly", prompt="Assemble two bolts",
        targets=[], images=[], active_ledger_ids=[], emit=emit, emit_binary=None, client=ModelClient(),
        started=time.perf_counter(), resolved_list=[], enriched_prompt="Assemble two bolts",
        is_assembly=False, is_edit=False, bom=[], catalog_matches=[], ops=[], summary="", assumptions=[],
        needs_clarification=False, clarification_text="", attempt=0, candidate=None, built_ledgers=None,
        outcome=None, done=False)


@pytest.fixture
def providers(monkeypatch):
    contexts = []
    replies = []
    async def complete(_self, **kwargs):
        if kwargs["stage"] == "architect":
            return {"is_assembly": True, "is_edit": False, "summary": "Two bolts",
                    "bom": [{"name": "bolt", "is_standard": True, "description": "M10 x 20"}]}
        if kwargs["stage"] == "librarian":
            return {"queries": [{"type": "Hexagon Head Bolt"}], "summary": "Find bolt"}
        assert kwargs["stage"] == "assembler"
        context = json.loads(kwargs["user"].split("Available Parts:\n", 1)[1])
        contexts.append((kwargs["user"], context))
        if replies:
            reply = replies.pop(0)
            return reply(context) if callable(reply) else reply
        part_id = context["sourced_parts"][0]["id"]
        return {"components": [{"id": f"comp_bolt_{i}", "part_id": part_id,
                    "placement": {"origin": [i * 40, 0, 0]}} for i in range(2)],
                "joints": [], "summary": "Two nominal bolts"}
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    monkeypatch.setattr(librarian, "query_catalog", lambda criteria: [BOLT])
    return contexts, replies


def test_fresh_catalog_assembly_real_build_and_retry(monkeypatch, providers):
    contexts, _ = providers
    session = Session()
    events = []
    async def emit(event):
        events.append(event)
    real_build = graph.build_design
    attempts = []
    async def build(ledgers, primary):
        assert session.revision == 0 and session.store.ledger.assembly is None
        attempts.append(ledgers[primary].model_dump(mode="json"))
        if len(attempts) == 1:
            return BuildOutcome(ok=False, detail="Move the second bolt farther away.", stub=False,
                                code=ErrorCode.GEOMETRY_INVALID_PARAMETER)
        return await real_build(ledgers, primary)
    monkeypatch.setattr(graph, "build_design", build)
    result = asyncio.run(graph.app.ainvoke(state_for(session, emit)))
    assert result["done"] and session.revision == 1, [event.model_dump() for event in events if getattr(event, "type", None) in {"geometry.failed", "error"}]
    assembly = session.store.ledger.assembly
    assert assembly is not None and len(assembly.parts) == 1 and len(assembly.components) == 2
    assert attempts[0] == attempts[1]  # No appended retry duplicates or regenerated source IDs.
    assert "Move the second bolt" in contexts[1][0]
    assert contexts[1][1]["rejected_operations"][0]["path"] == "/assembly"
    artifact = session.artifacts.get(1, "step")
    assert artifact is not None and b"ISO-10303-21" in artifact.data


@pytest.mark.parametrize("catalog", [[], [{**BOLT, "type": "Plain Washer"}],
    [{**BOLT, "type": "Hexagon Nut"}], [{**BOLT, "parameters": {"thread": "bad"}}]])
def test_missing_or_unsupported_catalog_preserves_design(monkeypatch, providers, catalog):
    contexts, _ = providers
    monkeypatch.setattr(librarian, "query_catalog", lambda criteria: catalog)
    session = Session()
    before = session.snapshot()
    events = []
    async def emit(event):
        events.append(event)
    asyncio.run(graph.app.ainvoke(state_for(session, emit)))
    assert session.snapshot() == before and not session.history and session.last_geometry is None
    assert not contexts
    assert any(isinstance(event, AgentMessage) and event.needs_clarification for event in events)


def test_assembler_clarification_discards_all_proposed_operations(providers):
    _, replies = providers
    replies.append({"components": [{"id": "comp_invalid", "part_id": "part_missing"}], "joints": [],
                    "summary": "Need custom part", "needs_clarification": "Please create the missing bracket first."})
    session = Session()
    before = session.snapshot()
    events = []
    async def emit(event):
        events.append(event)
    result = asyncio.run(graph.app.ainvoke(state_for(session, emit)))
    assert result["ops"] == [] and session.snapshot() == before and session.last_geometry is None
    assert any(isinstance(event, AgentMessage) and event.needs_clarification for event in events)


def test_omitted_catalog_part_requests_clarification(providers):
    _, replies = providers
    replies.append({"components": [], "joints": [], "summary": "Done"})
    session = Session()
    async def emit(_):
        pass
    result = asyncio.run(graph.app.ainvoke(state_for(session, emit)))
    assert result["needs_clarification"] and "omitted sourced parts" in result["clarification_text"]
    assert result["ops"] == [] and session.revision == 0 and not session.history


def test_exhausted_assembly_retries_preserve_accepted_state(monkeypatch, providers):
    contexts, _ = providers
    session = Session()
    before = session.snapshot()
    attempts = []
    async def emit(_):
        pass
    async def fail(ledgers, primary):
        attempts.append(ledgers[primary].model_dump(mode="json"))
        return BuildOutcome(ok=False, detail="Invalid placement", stub=False,
                            code=ErrorCode.GEOMETRY_INVALID_PARAMETER)
    monkeypatch.setattr(graph, "build_design", fail)
    result = asyncio.run(graph.app.ainvoke(state_for(session, emit)))
    assert result["done"] and len(attempts) == len(contexts) == 3
    assert attempts[0] == attempts[1] == attempts[2]
    assert session.snapshot() == before and not session.history and session.last_geometry is None


def test_existing_assembly_sources_preserved(providers, plate_ledger):
    contexts, replies = providers
    primary = Ledger.model_validate({"assembly": {"parts": [{"id": "part_existing", "features": [
        {"id": "feat_existing", "kind": "box", "parameters": {"length": 10, "width": 10, "height": 10}}]}],
        "components": [{"id": "comp_existing", "part_id": "part_existing", "placement": {"origin": [200, 0, 0]}}]}})
    session = Session(primary_ledger_id=primary.project_id, stores={
        primary.project_id: LedgerStore(primary), plate_ledger.project_id: LedgerStore(plate_ledger)})
    replies.append(lambda context: {"components": [
        {"id": "comp_plate", "sub_assembly_id": plate_ledger.project_id},
        {"id": "comp_new_bolt", "part_id": context["sourced_parts"][0]["id"],
         "placement": {"origin": [90, 0, 0]}}], "joints": [], "summary": "Add plate and bolt"})
    async def emit(_):
        pass
    asyncio.run(graph.app.ainvoke(state_for(session, emit)))
    assembly = session.store.ledger.assembly
    assert assembly is not None
    assert [c.id for c in assembly.components] == ["comp_existing", "comp_plate", "comp_new_bolt"]
    assert assembly.parts[0].id == "part_existing"
    assert contexts[0][1]["primary_ledger"]["assembly"]["components"][0]["id"] == "comp_existing"
    assert plate_ledger.project_id in contexts[0][1]["secondary_ledgers"]
    assert session.revision == 1
