import asyncio
import math
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cadenza_backend import geometry_bridge, part_commands, edit_preview
from cadenza_backend.assembly_commands import handle_assembly_command
from cadenza_backend.contracts_bridge import CadenzaError, Ledger
from cadenza_backend.edit_preview import decide_preview
from cadenza_backend.ledger_store import LedgerStore
from cadenza_backend.part_commands import create_part, rename_part
from cadenza_backend.session import Session
from cadenza_contracts.assembly_commands import AssemblyCommand
from cadenza_contracts.messages import PreviewDecision
from cadenza_contracts.part_commands import PartCreate, PartRename


async def discard(_): pass


def decide(session, action="apply"):
    assert session.preview
    request = PreviewDecision.model_validate(dict(message_id="decision", base_revision=session.revision,
        preview_id=session.preview.id, action=action))
    asyncio.run(decide_preview(session, request, discard))
    return request


def fixture():
    session = Session()
    source = Ledger.model_validate(dict(project_id="prj_plate", metadata=dict(name="Plate"),
        features=[dict(id="feat_stock", kind="box", parameters=dict(length=20, width=10, height=5))]))
    session.stores[source.project_id] = LedgerStore(source)
    return session


def rename(session, **updates):
    return PartRename.model_validate(dict(dict(message_id="rename", base_revision=session.revision,
        ledger_id="prj_plate", name="BasePlate"), **updates))


@pytest.mark.parametrize("case", ["stale", "missing", "empty", "assembly", "same", "duplicate", "draft", "preview"])
def test_rename_admission_preserves_accepted_state(monkeypatch, case):
    session = fixture(); updates = {}
    if case == "stale": updates["base_revision"] = 9
    if case == "missing": updates["ledger_id"] = "absent"
    if case == "empty": updates["ledger_id"] = session.primary_ledger_id
    if case == "assembly":
        source = session.stores["prj_plate"].ledger.model_dump(mode="json")
        source.update(features=[], assembly=dict(components=[]))
        session.stores["prj_plate"] = LedgerStore(Ledger.model_validate(source))
    if case == "same": updates["name"] = " Plate "
    if case == "duplicate":
        session.stores["other"] = LedgerStore(Ledger.model_validate(dict(project_id="other", metadata=dict(name=" BASEPLATE "))))
    if case == "draft": monkeypatch.setattr(session, "draft", object())
    if case == "preview":
        from types import SimpleNamespace
        from time import monotonic
        monkeypatch.setattr(session, "preview", SimpleNamespace(created=monotonic(),
            message=SimpleNamespace(base_revision=session.revision)))
    before = session.snapshot(); previous_preview = session.preview
    build = AsyncMock(); monkeypatch.setattr(part_commands, "build_design", build)
    with pytest.raises(CadenzaError): asyncio.run(rename_part(session, rename(session, **updates), discard))
    build.assert_not_called()
    assert session.snapshot() == before and session.preview is previous_preview and not session.history


@pytest.mark.parametrize("stage", ["project", "source", "cancelled"])
def test_rename_build_failure_is_private(monkeypatch, stage):
    session = fixture(); before = session.snapshot()
    async def build(*_):
        if stage == "cancelled": raise asyncio.CancelledError()
        return geometry_bridge.BuildOutcome(ok=stage == "source", stub=False, empty=True, detail="failure")
    monkeypatch.setattr(part_commands, "build_design", build)
    monkeypatch.setattr(edit_preview, "build_design", AsyncMock(return_value=geometry_bridge.BuildOutcome(ok=False, stub=False, detail="failure")))
    with pytest.raises((CadenzaError, asyncio.CancelledError)):
        asyncio.run(rename_part(session, rename(session), discard))
    assert session.snapshot() == before and session.preview is None and not session.history


def test_rename_transport_is_discovered_and_revision_bound(monkeypatch):
    from fastapi.testclient import TestClient
    from cadenza_backend import app as transport
    async def build(*_): return geometry_bridge.BuildOutcome(ok=True, stub=False, empty=True, detail="fixture")
    monkeypatch.setattr(part_commands, "build_design", build)
    monkeypatch.setattr(edit_preview, "build_design", build)
    with TestClient(transport.app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json(dict(type="client.hello", contract_version="1.0.0")); ready = ws.receive_json()
        session = transport.registry.get(ready["session_id"]); assert session
        session.stores["prj_plate"] = fixture().stores["prj_plate"]
        ws.send_json(dict(type="design.part.capabilities", message_id="discover"))
        assert ws.receive_json()["rename_source"] is True
        ws.send_json(rename(session, base_revision=9).model_dump(mode="json"))
        assert ws.receive_json()["code"] == "REVISION_CONFLICT"
        ws.send_json(rename(session).model_dump(mode="json"))
        assert ws.receive_json()["type"] == "design.preview"
        assert session.stores["prj_plate"].ledger.metadata.name == "Plate"
        ws.send_json(rename(session).model_dump(mode="json"))
        assert ws.receive_json()["code"] == "BUSY"


def test_primary_part_rename_retains_project_preview():
    session = fixture()
    source = session.stores.pop("prj_plate").ledger
    source = source.model_copy(update={"project_id": session.primary_ledger_id})
    session.stores[session.primary_ledger_id] = LedgerStore(source)
    asyncio.run(rename_part(session, rename(session, ledger_id=session.primary_ledger_id), discard))
    assert session.preview and session.preview.display_outcome is None
    assert session.preview.message.display_ledger_id is None
    decide(session)
    assert session.store.ledger.metadata.name == "BasePlate"
    assert session.store.ledger.features == source.features


def test_three_sources_four_instances_rename_history_save_and_shared_edit(tmp_path):
    from build123d import import_step, ShapeList
    from cadenza_backend.design_commands import handle_design_command
    from cadenza_backend.projects import ProjectRepository
    from cadenza_contracts.design_commands import DesignCommand
    from cadenza_contracts.project import ProjectDocument
    session = Session(); ids = {}
    for name, kind, parameters in [
        ("BasePlate", "box", dict(length=100, width=60, height=15)),
        ("Spacer", "cylinder", dict(diameter=20, height=30)),
        ("TopPlate", "box", dict(length=80, width=40, height=8)),
    ]:
        request = PartCreate.model_validate(dict(message_id="create", base_revision=session.revision, name=name,
            operations=[dict(op="add_feature", kind=kind, name="Stock", operation="add", placement={}, parameters=parameters)]))
        asyncio.run(create_part(session, request, discard)); decide(session)
        ids[name] = next(lid for lid, store in session.stores.items() if store.ledger.metadata.name == name)
    before_sources = session.snapshot()
    ops: list[dict[str, Any]] = [dict(op="initialize_assembly", name="TestFixture")]
    for source, name, origin in [("BasePlate", "Base", [0,0,0]), ("Spacer", "LeftSpacer", [-30,0,15]),
                                 ("Spacer", "RightSpacer", [30,0,15]), ("TopPlate", "Cap", [0,0,45])]:
        ops.append(dict(op="add_instance", source_ledger_id=ids[source], name=name,
            placement=dict(origin=origin, rotation_deg=[0,0,0])))
    request = AssemblyCommand.model_validate(dict(message_id="assembly", base_revision=session.revision,
        ledger_id=session.primary_ledger_id, scope="assembly_definition", operations=ops))
    asyncio.run(handle_assembly_command(session, request, discard)); decide(session)
    assembly = session.store.ledger.assembly; assert assembly and len(assembly.components) == 4 and not assembly.joints
    assert [c.placement.origin for c in assembly.components] == [[0,0,0],[-30,0,15],[30,0,15],[0,0,45]]
    assert all(c.placement.rotation_deg == [0,0,0] for c in assembly.components)
    assert assembly.components[1].sub_assembly_id == assembly.components[2].sub_assembly_id == ids["Spacer"]
    for lid in ids.values():
        assert session.stores[lid].ledger.features == before_sources[lid].features
    artifact = session.artifacts.get(session.revision, "step"); assert artifact
    path = tmp_path / "assembly.step"; path.write_bytes(artifact.data); shape = import_step(str(path))
    assert len(shape.solids()) == 4
    assert shape.volume == pytest.approx(100*60*15 + 2*math.pi*10**2*30 + 80*40*8)
    assert tuple(shape.bounding_box().size) == pytest.approx((100,60,53))
    solids = list(shape.solids())
    for i, a in enumerate(solids):
        for b in solids[i+1:]:
            overlap = a.intersect(b)
            volume = sum(item.volume for item in overlap) if isinstance(overlap, ShapeList) else overlap.volume if overlap else 0
            assert abs(volume) < 1e-6
    before = session.snapshot(); source_id = ids["Spacer"]
    request = rename(session, ledger_id=source_id, name="Spacer Revised")
    asyncio.run(rename_part(session, request, discard)); assert session.snapshot() == before
    assert session.preview and {c.path for c in session.preview.message.changes} == {"/metadata/name"}
    decide(session, "cancel"); assert session.snapshot() == before
    asyncio.run(rename_part(session, request, discard)); applied = decide(session)
    with pytest.raises(CadenzaError): asyncio.run(decide_preview(session, applied, discard))
    after = session.snapshot()
    assert after[source_id].metadata.name == "Spacer Revised"
    assert after[session.primary_ledger_id].assembly == before[session.primary_ledger_id].assembly
    for lid in before:
        assert after[lid].features == before[lid].features
    undo = session.stage_undo(); assert undo; session.commit(undo[0], undo_index=undo[1])
    assert session.stores[source_id].ledger.metadata.name == "Spacer"
    redo = session.stage_redo(); assert redo; session.commit(redo, redo=True)
    document = ProjectDocument(name="Fixture", primary_ledger_id=session.primary_ledger_id,
        ledgers=session.snapshot(), history=session.history, redo=session.redo)
    repo = ProjectRepository(tmp_path / "saved.sqlite3"); pid, _ = repo.save(document, None, 0)
    loaded, _ = repo.load(pid); assert loaded == document
    assert loaded.ledgers[source_id].metadata.name == "Spacer Revised"
    # The next AI request resolves the current human name against the same stable source ID.
    import json
    from cadenza_backend.assembly_prompts import handle_assembly_prompt
    from cadenza_contracts.messages import AssemblyPrompt
    client = AsyncMock()
    client.complete_json.return_value = dict(kind="clarification", question="Where should the additional instance go?")
    prompt = AssemblyPrompt.model_validate(dict(message_id="context", base_revision=session.revision,
        ledger_id=session.primary_ledger_id, owner_path=[], scope="assembly_definition", target=dict(kind="definition"),
        prompt="Add another Spacer Revised"))
    asyncio.run(handle_assembly_prompt(session, prompt, discard, client=client))
    context = json.loads(client.complete_json.call_args.kwargs["user"])
    assert next(item for item in context["sources"] if item["id"] == source_id)["name"] == "Spacer Revised"
    assert session.preview is None and session.snapshot() == document.ledgers
    stock = session.stores[source_id].ledger.features[0]
    edit = DesignCommand.model_validate(dict(message_id="edit", base_revision=session.revision, ledger_id=source_id,
        operations=[dict(op="set_dimension", feature_id=stock.id, parameter="diameter", value=18)]))
    asyncio.run(handle_design_command(session, edit, discard)); decide(session)
    artifact = session.artifacts.get(session.revision, "step"); assert artifact
    path.write_bytes(artifact.data); edited = import_step(str(path))
    assert edited.volume == pytest.approx(100*60*15 + 2*math.pi*9**2*30 + 80*40*8)
    assert session.store.ledger.assembly == assembly
