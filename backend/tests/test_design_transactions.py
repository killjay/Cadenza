import asyncio
from unittest.mock import AsyncMock
import time
from typing import Awaitable, Callable

import pytest
from pydantic import BaseModel

from cadenza_backend import geometry_bridge, graph
from cadenza_backend.agents.model_client import ModelClient
from cadenza_backend.contracts_bridge import BoxFeature, ErrorCode, Ledger, PatchOp
from cadenza_backend.design import accept_design, build_design
from cadenza_backend.session import Session, LedgerStore
from cadenza_backend.wire import AgentStatus, GeometryReady, LedgerUpdated


async def discard(_):
    pass


def accept(session, ledgers, *, undo_index=None):
    outcome = geometry_bridge.BuildOutcome(ok=True, detail="fixture", stub=False, glb=b"glb", step=b"step")
    asyncio.run(accept_design(session, ledgers, outcome, "msg_test", discard, undo_index=undo_index))


def base_box(ledger: Ledger) -> BoxFeature:
    feature = ledger.features[0]
    assert isinstance(feature, BoxFeature)
    return feature


def node_state(
    session: Session, ops: list[PatchOp], emit: Callable[[BaseModel], Awaitable[None]],
) -> graph.AgentState:
    return graph.AgentState(
        session=session, message_id="msg_test", prompt="Change thickness",
        targets=[], images=[], active_ledger_ids=[], emit=emit, emit_binary=None,
        client=ModelClient(), started=time.perf_counter(), resolved_list=[],
        enriched_prompt="Change thickness", is_assembly=False, is_edit=True,
        bom=[], catalog_matches=[], ops=ops, summary="Changed thickness",
        assumptions=[], needs_clarification=False, clarification_text="",
        attempt=0, candidate=None, built_ledgers=None, outcome=None, done=False,
    )


def test_secondary_edits_and_undo_have_unique_whole_design_revisions(plate_ledger):
    session = Session()
    secondary_id = plate_ledger.project_id
    accept(session, session.stage({secondary_id: plate_ledger}))
    changed = plate_ledger.model_copy(deep=True)
    base_box(changed).parameters.height = 30
    accept(session, session.stage({secondary_id: changed}))
    assert session.revision == 2
    candidate = session.stage_undo()
    assert candidate is not None
    ledgers, index = candidate
    assert base_box(session.stores[secondary_id].ledger).parameters.height == 30
    accept(session, ledgers, undo_index=index)
    assert session.revision == 3
    assert base_box(session.stores[secondary_id].ledger).parameters.height == 20
    assert {s.ledger.revision for s in session.stores.values()} == {3}
    assert session.artifacts.get(2, "glb") is not session.artifacts.get(3, "glb")
    candidate = session.stage_undo(0)
    assert candidate is not None
    ledgers, index = candidate
    assert secondary_id not in ledgers
    assert build_design(ledgers, session.primary_ledger_id).empty
    accept(session, ledgers, undo_index=index)
    assert session.revision == 4
    assert not session.history
    assert session.stage_undo(999) is None


def test_every_secondary_is_checked_before_primary(monkeypatch, plate_ledger):
    session = Session()
    ledgers = session.stage({plate_ledger.project_id: plate_ledger})
    calls = []
    def fail(ledger, all_ledgers):
        calls.append(ledger.project_id)
        assert all_ledgers == ledgers
        return geometry_bridge.BuildOutcome(ok=False, detail="invalid part", stub=False,
                                           code=ErrorCode.GEOMETRY_KERNEL_FAILURE)
    monkeypatch.setattr(geometry_bridge, "build", fail)
    assert not build_design(ledgers, session.primary_ledger_id).ok
    assert calls == [plate_ledger.project_id]
    assert session.revision == 0 and not session.history and len(session.artifacts) == 0


def test_rejected_snapshot_does_not_change_history(plate_ledger):
    session = Session()
    snapshot = session.stage({session.primary_ledger_id: plate_ledger})
    with pytest.raises(ValueError):
        session.commit(snapshot)  # mismatched document identity
    assert session.revision == 0 and not session.history


def test_cancelled_graph_before_build_preserves_accepted_design(plate_ledger):
    session = Session()
    accept(session, session.stage({plate_ledger.project_id: plate_ledger}))
    before = session.snapshot()
    async def cancel_on_build(frame: BaseModel):
        if isinstance(frame, AgentStatus) and frame.phase == "building":
            raise asyncio.CancelledError()
    state = node_state(session,
        [PatchOp(op="replace", path="/features/0/parameters/height", value=25,
                 ledger_id=plate_ledger.project_id)], cancel_on_build)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(graph.build_node(state))
    assert session.snapshot() == before and session.revision == 1
    artifact = session.artifacts.get(1, "glb")
    assert artifact is not None
    assert len(session.history) == 1 and artifact.data == b"glb"


def test_graph_secondary_only_commit_and_failed_build_preservation(monkeypatch, plate_ledger):
    session = Session()
    session.stores[plate_ledger.project_id] = LedgerStore(plate_ledger)
    emitted: list[BaseModel] = []
    async def emit(frame: BaseModel):
        emitted.append(frame)
    state = node_state(session,
        [PatchOp(op="replace", path="/features/0/parameters/height", value=25,
                 ledger_id=plate_ledger.project_id)], emit)
    monkeypatch.setattr(graph, "build_design", AsyncMock(return_value=geometry_bridge.BuildOutcome(
        ok=True, detail="built", stub=False, glb=b"glb")))
    state.update(asyncio.run(graph.build_node(state)))
    assert session.revision == 0
    asyncio.run(graph.review_node(state))
    updated = next(f for f in emitted if isinstance(f, LedgerUpdated))
    assert updated.revision == 1 and updated.patch[0]["ledger_id"] == plate_ledger.project_id
    assert base_box(session.stores[plate_ledger.project_id].ledger).parameters.height == 25
    before, history, artifacts = session.snapshot(), list(session.history), len(session.artifacts)
    monkeypatch.setattr(graph, "build_design", AsyncMock(return_value=geometry_bridge.BuildOutcome(
        ok=False, detail="kernel failure", stub=False, code=ErrorCode.GEOMETRY_KERNEL_FAILURE)))
    state.update(asyncio.run(graph.build_node(state)))
    asyncio.run(graph.review_node(state))
    assert session.snapshot() == before and session.history == history
    assert len(session.artifacts) == artifacts and session.revision == 1


def test_interruption_before_commit_preserves_state_and_after_commit_can_replay(plate_ledger):
    from cadenza_backend.design import replay_geometry
    session = Session()
    staged = session.stage({session.primary_ledger_id: plate_ledger.model_copy(update={"project_id": session.primary_ledger_id})})
    assert session.revision == 0  # staging alone never commits
    async def disconnect(_):
        raise asyncio.CancelledError()
    outcome = geometry_bridge.BuildOutcome(ok=True, detail="built", stub=False, glb=b"accepted")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(accept_design(session, staged, outcome, "msg_edit", disconnect))
    artifact = session.artifacts.get(1, "glb")
    assert artifact is not None
    assert session.revision == 1 and artifact.data == b"accepted"
    frames: list[BaseModel | bytes] = []
    async def capture(frame: BaseModel | bytes):
        frames.append(frame)
    asyncio.run(replay_geometry(session, capture, capture))
    assert isinstance(frames[0], GeometryReady)
    assert frames[0].revision == 1 and isinstance(frames[1], bytes)


@pytest.mark.skipif(not geometry_bridge.REAL_GEOMETRY, reason="requires OCCT")
def test_assembly_cache_changes_when_external_part_changes(plate_ledger):
    from cadenza_geometry.service import Build123dGeometryService
    service = Build123dGeometryService()
    assembly = Ledger.model_validate({
        "assembly": {
            "components": [{
                "id": "comp_plate",
                "sub_assembly_id": plate_ledger.project_id,
            }],
        },
    })
    ledgers = {assembly.project_id: assembly, plate_ledger.project_id: plate_ledger}
    first = service._compile(assembly, ledgers)
    assert service._compile(assembly, dict(ledgers)) is first
    changed = plate_ledger.model_copy(deep=True)
    base_box(changed).parameters.length = 120
    ledgers[plate_ledger.project_id] = changed
    second = service._compile(assembly, ledgers)
    assert second is not first
    first_volume = sum(s.volume for s in first.solid.solids())
    second_volume = sum(s.volume for s in second.solid.solids())
    assert second_volume - first_volume == pytest.approx(20 * 100 * 20)
    assert service.build(assembly, ledgers).volume == pytest.approx(second_volume)
    # Changing dependency contents at the same revision must still invalidate.
    assert changed.revision == plate_ledger.revision
