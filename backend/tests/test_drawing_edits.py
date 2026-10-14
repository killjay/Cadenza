import asyncio
import json

import pytest

from cadenza_backend import graph
from cadenza_backend.agents import librarian
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.contracts_bridge import ErrorCode
from cadenza_backend.geometry_bridge import BuildOutcome
from cadenza_backend.orchestrator import handle_prompt
from cadenza_backend.session import LedgerStore, Session
from cadenza_backend.wire import Target


# Decoded transport attachments; provider interpretation is faked, not asserted.
IMAGES = [("image/png", b"drawing-front"), ("image/jpeg", b"drawing-section")]


def edit_payload(ledger, height=40, question=None):
    return {"summary": "Updated thickness.", "needs_clarification": question,
            "patch": [{"op": "replace", "ledger_id": ledger.project_id,
                       "path": f"/features/{ledger.features[0].id}/parameters/height", "value": height}]}


def run_turn(session, *, targets=None, images=None, active=None):
    events, blobs = [], []
    async def emit(event):
        events.append(event)
    async def emit_binary(data):
        blobs.append(data)
    asyncio.run(handle_prompt(session, "msg_drawing", "Update this using the supplied dimensions.",
        targets or [], emit, images=images or [], active_ledger_ids=active, emit_binary=emit_binary))
    return events, blobs


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("images", [[], IMAGES])
def test_drawing_or_text_edit_preserves_context_and_builds(monkeypatch, plate_ledger, selected, images):
    session = Session(primary_ledger_id=plate_ledger.project_id,
                      stores={plate_ledger.project_id: LedgerStore(ledger=plate_ledger)})
    calls = []
    async def complete(_self, **kwargs):
        calls.append(kwargs)
        if kwargs["stage"] == "architect":
            context = json.loads(kwargs["user"])
            assert context["current_ledgers"][plate_ledger.project_id] == plate_ledger.model_dump(mode="json")
            assert context["has_target"] is False
            return {"is_assembly": False, "is_edit": True, "summary": "Edit existing plate"}
        assert kwargs["stage"] == "machinist"
        assert plate_ledger.features[0].id in kwargs["user"]
        assert kwargs["context"]["ledgers"][plate_ledger.project_id] == plate_ledger
        assert ("DRAWING-ASSISTED EDIT" in kwargs["agent_card"].system_prompt) is bool(images)
        if selected:
            assert "did not click" not in kwargs["user"]
        return edit_payload(plate_ledger)
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    before = session.revision
    targets = [Target(point=[20, 20, 20], normal=[0, 0, 1])] if selected else []
    events, blobs = run_turn(session, targets=targets, images=images)
    assert [call["stage"] for call in calls] == (["machinist"] if selected else ["architect", "machinist"])
    assert all(call["images"] == images for call in calls)
    assert session.revision == before + 1 and len(session.store.ledger.features) == 2
    assert session.store.ledger.model_dump()["features"][0]["parameters"]["height"] == 40
    assert any(event.type == "geometry.ready" for event in events) and blobs
    assert session.store.ledger.features[1] == plate_ledger.features[1]


def test_architect_receives_selected_secondary_design(monkeypatch, plate_ledger):
    session = Session()
    session.stores[plate_ledger.project_id] = LedgerStore(ledger=plate_ledger)
    before = session.snapshot()
    async def complete(_self, **kwargs):
        if kwargs["stage"] == "architect":
            context = json.loads(kwargs["user"])
            assert context["primary_ledger_id"] == session.primary_ledger_id
            assert context["active_ledger_ids"] == [plate_ledger.project_id]
            assert set(context["current_ledgers"]) == {plate_ledger.project_id}
            assert set(context["available_ledgers"]) == set(session.stores)
            return {"is_assembly": False, "is_edit": True, "summary": "Edit selected source"}
        return edit_payload(plate_ledger, question="Is the marked thickness 30 or 40 mm?")
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    events, blobs = run_turn(session, images=IMAGES, active=[plate_ledger.project_id])
    assert session.snapshot() == before and not session.history and not blobs
    message = next(event for event in events if event.type == "agent.message")
    assert message.needs_clarification and message.text == "Is the marked thickness 30 or 40 mm?"
    assert not any(event.type in {"ledger.updated", "geometry.ready"} for event in events)


def test_image_edit_retry_retains_original_attachments_and_accepted_base(monkeypatch, plate_ledger):
    session = Session(primary_ledger_id=plate_ledger.project_id,
                      stores={plate_ledger.project_id: LedgerStore(ledger=plate_ledger)})
    calls = []
    async def complete(_self, **kwargs):
        assert kwargs["stage"] == "machinist" and kwargs["images"] == IMAGES
        assert kwargs["context"]["ledgers"][plate_ledger.project_id] == plate_ledger
        calls.append(kwargs)
        return edit_payload(plate_ledger, height=30 if len(calls) == 1 else 40)
    real_build = graph.build_design
    attempts = []
    async def build(ledgers, primary):
        attempts.append(ledgers[primary])
        if len(attempts) == 1:
            return BuildOutcome(ok=False, stub=False, detail="Use 40 mm thickness.", code=ErrorCode.GEOMETRY_INVALID_PARAMETER)
        return await real_build(ledgers, primary)
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    monkeypatch.setattr(graph, "build_design", build)
    run_turn(session, targets=[Target(point=[20, 20, 20])], images=IMAGES)
    assert len(calls) == 2 and "Use 40 mm thickness." in calls[1]["user"]
    assert session.store.ledger.model_dump()["features"][0]["parameters"]["height"] == 40
    assert session.revision == plate_ledger.revision + 1


def test_drawing_assembly_images_and_bom_reach_catalog_and_assembler(monkeypatch):
    session = Session()
    calls = []
    async def complete(_self, **kwargs):
        calls.append(kwargs)
        assert kwargs["images"] == IMAGES
        if kwargs["stage"] == "architect":
            return {"is_assembly": True, "is_edit": False, "summary": "Two bolts",
                    "bom": [{"name": "bolt", "is_standard": True, "description": "Two M10 x 20 bolts"}]}
        if kwargs["stage"] == "librarian":
            assert "Two M10 x 20 bolts" in kwargs["user"]
            return {"queries": [{"type": "Hexagon Head Bolt"}], "summary": "Find bolt"}
        assert kwargs["stage"] == "assembler"
        context = json.loads(kwargs["user"].split("Available Parts:\n", 1)[1])
        pid = context["sourced_parts"][0]["id"]
        return {"components": [{"id": "comp_a", "part_id": pid},
                {"id": "comp_b", "part_id": pid, "placement": {"origin": [40, 0, 0]}}],
                "joints": [], "summary": "Two bolts"}
    monkeypatch.setattr(ModelClient, "complete_json", complete)
    monkeypatch.setattr(librarian, "query_catalog", lambda _: [{"sku": "ISO-4017-M10x20",
        "type": "Hexagon Head Bolt", "parameters": {"thread": "M10", "length": 20}}])
    events, blobs = run_turn(session, images=IMAGES)
    assert [call["stage"] for call in calls] == ["architect", "librarian", "assembler"]
    assembly = session.store.ledger.assembly
    assert assembly is not None and len(assembly.components) == 2 and session.revision == 1
    assert any(event.type == "geometry.ready" for event in events) and blobs
