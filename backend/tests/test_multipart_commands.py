import asyncio
from unittest.mock import AsyncMock

import pytest

from cadenza_backend import geometry_bridge
from cadenza_backend.contracts_bridge import CadenzaError, Ledger
from cadenza_backend.edit_preview import decide_preview
from cadenza_backend.ledger_store import LedgerStore
from cadenza_backend.part_commands import create_part, prompt_part
from cadenza_backend.session import Session
from cadenza_contracts.messages import PartPrompt, PreviewDecision
from cadenza_contracts.part_commands import PartCreate


def command(session, name="Plate", length=40):
    return PartCreate.model_validate(dict(message_id="new", base_revision=session.revision, name=name,
        operations=[dict(op="add_feature", kind="box", name="Stock", operation="add", placement={},
            parameters=dict(length=length, width=30, height=5))]))


async def discard(_): pass


def test_real_private_creation_apply_undo_redo_and_display():
    session = Session(); original = session.snapshot(); primary = session.primary_ledger_id
    asyncio.run(create_part(session, command(session), discard))
    candidate = session.preview
    assert candidate and candidate.display_outcome and candidate.display_outcome.volume_mm3 == pytest.approx(6000)
    assert candidate.outcome.empty and not candidate.outcome.glb
    assert candidate.message.source_glb_url and not candidate.message.glb_url
    assert session.snapshot() == original and not session.history
    decision = PreviewDecision(message_id="apply", base_revision=0, preview_id=candidate.id, action="apply")
    asyncio.run(decide_preview(session, decision, discard))
    assert len(session.stores) == 2 and session.primary_ledger_id == primary
    assert session.last_geometry and session.last_geometry.empty
    assert session.artifacts.get(1, "step") is None
    with pytest.raises(CadenzaError): asyncio.run(decide_preview(session, decision, discard))
    undo = session.stage_undo(); assert undo
    session.commit(undo[0], undo_index=undo[1])
    assert len(session.stores) == 1
    redo = session.stage_redo(); assert redo
    session.commit(redo, redo=True)
    assert len(session.stores) == 2
    with pytest.raises(CadenzaError, match="already has"):
        asyncio.run(create_part(session, command(session, " plate "), discard))


@pytest.mark.parametrize("failed_call", [1, 2])
def test_build_failures_do_not_publish_orphan(monkeypatch, failed_call):
    from cadenza_backend import part_commands, edit_preview
    calls = 0
    async def build(*args):
        nonlocal calls
        calls += 1
        return geometry_bridge.BuildOutcome(ok=calls != failed_call, stub=False, detail="test failure", empty=True)
    monkeypatch.setattr(part_commands, "build_design", build)
    monkeypatch.setattr(edit_preview, "build_design", build)
    session = Session(); before = session.snapshot()
    with pytest.raises(CadenzaError): asyncio.run(create_part(session, command(session), discard))
    assert session.snapshot() == before and session.preview is None and not session.history


def test_stale_and_limit_reject_before_build(monkeypatch):
    from cadenza_backend import part_commands
    build = AsyncMock(); monkeypatch.setattr(part_commands, "build_design", build)
    session = Session(); request = command(session).model_copy(update={"base_revision": 9})
    with pytest.raises(CadenzaError): asyncio.run(create_part(session, request, discard))
    for i in range(32):
        ledger = Ledger.model_validate(dict(features=[dict(id="feat_box", kind="box", operation="add",
            parameters=dict(length=2, width=2, height=2))]))
        session.stores[ledger.project_id] = LedgerStore(ledger)
    with pytest.raises(CadenzaError, match="32 source"):
        asyncio.run(create_part(session, command(session), discard))
    build.assert_not_called()


def test_offline_creation_and_clarification():
    class Client:
        def __init__(self, result): self.complete_json = AsyncMock(return_value=result)
    session = Session(); before = session.snapshot()
    clarification = Client(dict(kind="clarification", question="What thickness?"))
    request = PartPrompt(message_id="ai", base_revision=0, name="Plate", prompt="Make a plate")
    asyncio.run(prompt_part(session, request, discard, client=clarification))
    assert session.snapshot() == before and session.preview is None
    op = command(session).operations[0].model_dump(mode="json")
    client = Client(dict(kind="proposal", description="Plate", operations=[op], assumptions=[]))
    asyncio.run(prompt_part(session, request, discard, client=client))
    assert session.preview and session.preview.display_outcome
    assert session.preview.display_outcome.volume_mm3 == pytest.approx(6000)
    assert client.complete_json.call_count == 1
    assert '"mode": "new_part"' in client.complete_json.call_args.kwargs["user"]


@pytest.mark.parametrize("primary_kind", ["empty", "part", "assembly"])
@pytest.mark.parametrize("length", [20, 40, 80])
def test_primary_is_preserved_for_three_source_variants(primary_kind, length):
    session = Session()
    if primary_kind != "empty":
        root = session.store.ledger.model_dump(mode="json")
        stock = dict(id="feat_box", kind="box", operation="add", parameters=dict(length=10, width=10, height=10))
        if primary_kind == "part": root["features"] = [stock]
        else: root["assembly"] = dict(parts=[dict(id="part_stock", features=[stock])],
            components=[dict(id="comp_stock", part_id="part_stock")])
        session.stores[session.primary_ledger_id] = LedgerStore(Ledger.model_validate(root))
    before = session.snapshot()
    asyncio.run(create_part(session, command(session, length=length), discard))
    preview = session.preview; assert preview and preview.display_outcome
    assert preview.display_outcome.volume_mm3 == pytest.approx(length * 30 * 5)
    if primary_kind != "empty": assert preview.outcome.volume_mm3 == pytest.approx(1000)
    assert session.snapshot() == before
    assert preview.ledgers[session.primary_ledger_id].features == before[session.primary_ledger_id].features
    assert preview.ledgers[session.primary_ledger_id].assembly == before[session.primary_ledger_id].assembly
    asyncio.run(decide_preview(session, PreviewDecision(message_id="cancel", base_revision=0,
        preview_id=preview.id, action="cancel"), discard))
    assert session.snapshot() == before and session.preview is None and not session.history


def test_saved_multipart_history_and_redo(tmp_path):
    from cadenza_backend.projects import ProjectRepository
    from cadenza_contracts.project import ProjectDocument
    session = Session(); primary = session.primary_ledger_id
    for index in range(3):
        asyncio.run(create_part(session, command(session, f"Part {index}", 20 + index * 10), discard))
        assert session.preview
        asyncio.run(decide_preview(session, PreviewDecision(message_id="apply", base_revision=session.revision,
            preview_id=session.preview.id, action="apply"), discard))
    undo = session.stage_undo(); assert undo
    session.commit(undo[0], undo_index=undo[1])
    document = ProjectDocument(name="Multipart", primary_ledger_id=primary,
        ledgers=session.snapshot(), history=session.history, redo=session.redo)
    repo = ProjectRepository(tmp_path / "multipart.sqlite3")
    pid, version = repo.save(document, None, 0)
    reopened, actual_version = ProjectRepository(repo.path).load(pid)
    assert reopened == document and actual_version == version
    assert len(reopened.ledgers) == 3 and len(reopened.redo[-1]) == 4
    assert all(primary in snapshot for snapshot in [reopened.ledgers, *reopened.history, *reopened.redo])


@pytest.mark.parametrize("operation", [
    dict(op="set_dimension", feature_id="feat_box", parameter="width", value=2),
    dict(op="add_circular_pattern", name="Circle", target_feature="feat_box", count=4, center_xy=[0,0]),
    dict(op="add_feature", kind="extrude", name="Extrusion", operation="add",
         placement=dict(origin=[0,0,0], rotation_deg=[0,0,0]), parameters=dict(amount=10, target_feature="feat_profile")),
])
def test_new_part_ai_cannot_mutate_or_reference_existing_sources(operation):
    class Client:
        complete_json = AsyncMock(return_value=dict(kind="proposal", description="Invalid", operations=[operation], assumptions=[]))
    session = Session(); before = session.snapshot()
    request = PartPrompt(message_id="ai", base_revision=0, name="Bad", prompt="Make a part")
    with pytest.raises(CadenzaError): asyncio.run(prompt_part(session, request, discard, client=Client()))
    assert session.snapshot() == before and session.preview is None


def test_creation_transport_discovery_revision_and_preview(monkeypatch):
    from fastapi.testclient import TestClient
    from cadenza_backend import app as transport, part_commands, edit_preview
    async def build(ledgers, primary):
        empty = not ledgers[primary].features
        return geometry_bridge.BuildOutcome(ok=True, stub=False, empty=empty, detail="transport fixture",
            glb=None if empty else b"glb", step=None if empty else b"step")
    monkeypatch.setattr(part_commands, "build_design", build)
    monkeypatch.setattr(edit_preview, "build_design", build)
    with TestClient(transport.app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json(dict(type="client.hello", contract_version="1.0.0"))
        ready = ws.receive_json()
        assert ready["primary_ledger_id"] in ready["store"]
        ws.send_json(dict(type="design.part.capabilities", message_id="cap"))
        assert ws.receive_json()["max_sources"] == 32
        session = transport.registry.get(ready["session_id"]); assert session
        request = command(session).model_dump(mode="json")
        ws.send_json({**request, "base_revision": 7})
        assert ws.receive_json()["code"] == "REVISION_CONFLICT"
        ws.send_json(request)
        preview = ws.receive_json()
        assert preview["type"] == "design.preview" and preview["source_glb_url"]
        assert len(session.stores) == 1
        ws.send_json(request)
        assert ws.receive_json()["code"] == "BUSY"
        ws.send_json(dict(type="design.preview_decision", message_id="apply", base_revision=0,
            preview_id=preview["preview_id"], action="apply"))
        updated = ws.receive_json()
        assert len(updated["store"]) == 2 and updated["revision"] == 1
        assert ws.receive_json()["empty"]
        assert ws.receive_json()["type"] == "design.preview_closed"


def test_creation_cancelled_during_build_is_private(monkeypatch):
    from cadenza_backend import part_commands
    async def canceled(*args): raise asyncio.CancelledError()
    monkeypatch.setattr(part_commands, "build_design", canceled)
    session = Session(); before = session.snapshot()
    with pytest.raises(asyncio.CancelledError): asyncio.run(create_part(session, command(session), discard))
    assert session.snapshot() == before and session.preview is None and not session.history


def test_primary_edits_retain_project_display():
    from cadenza_backend.design_commands import handle_design_command
    from cadenza_contracts.design_commands import DesignCommand, SetDimension
    session = Session()
    root = Ledger.model_validate(dict(project_id=session.primary_ledger_id, features=[dict(id="feat_box", kind="box",
        parameters=dict(length=20, width=10, height=5))]))
    session.stores[root.project_id] = LedgerStore(root)
    request = DesignCommand(message_id="edit", base_revision=0, ledger_id=root.project_id,
        operations=[SetDimension(feature_id="feat_box", parameter="width", value=15)])
    asyncio.run(handle_design_command(session, request, discard))
    assert session.preview and session.preview.message.glb_url
    assert session.preview.message.display_ledger_id is None and session.preview.display_outcome is None
