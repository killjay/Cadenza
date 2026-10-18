"""Real worker capture, private previews, source edits and persistence."""
import asyncio
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError
from cadenza_backend import assembly_commands as commands
from cadenza_backend.design_commands import handle_design_command
from cadenza_backend.edit_preview import decide_preview
from cadenza_backend.geometry_jobs import inspect
from cadenza_backend.projects import ProjectRepository
from cadenza_backend.session import Session, LedgerStore
from cadenza_contracts.assembly_commands import AssemblyCommand, AssemblyCapabilitiesRequest
from cadenza_contracts.design_commands import DesignCommand
from cadenza_contracts.errors import CadenzaError
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.messages import PreviewDecision
from cadenza_contracts.project import ProjectDocument, document_version, load_document


async def emit(_): pass


def fixture():
    plate = Ledger.model_validate(dict(project_id="plate", metadata=dict(name="Plate"), features=[dict(
        id="feat_stock", kind="box", parameters=dict(length=100, width=60, height=15))]))
    spacer = Ledger.model_validate(dict(project_id="spacer", metadata=dict(name="Spacer"), features=[dict(
        id="feat_stock", kind="cylinder", parameters=dict(diameter=20, height=30))]))
    root = Ledger.model_validate(dict(project_id="root", assembly=dict(components=[
        dict(id="comp_base", sub_assembly_id="plate"),
        dict(id="comp_spacer", sub_assembly_id="spacer", placement=dict(origin=[-30, 0, 15]))])))
    return Session(primary_ledger_id="root", stores={l.project_id: LedgerStore(l) for l in (root, plate, spacer)})


def operation() -> dict[str, Any]:
    def pick(source, kind, role, point, normal):
        return dict(reference=dict(ledger_id=source, source_ledger_id=source, feature_id="feat_stock",
            feature_kind=kind, operation="add", role=role), point=point, normal=normal)
    return dict(op="add_planar_attachment", component_a="comp_base", component_b="comp_spacer",
        target_a=pick("plate", "box", "top_face", [-30, 0, 15], [0, 0, 1]),
        target_b=pick("spacer", "cylinder", "bottom_face", [0, 0, 0], [0, 0, -1]))


def request(session, op=None):
    return AssemblyCommand.model_validate(dict(message_id="planar", base_revision=session.revision,
        ledger_id="root", owner_path=[], scope="assembly_definition", operations=[op or operation()]))


async def decide(session, action="apply"):
    assert session.preview
    await decide_preview(session, PreviewDecision.model_validate(dict(message_id="decision", base_revision=session.revision,
        preview_id=session.preview.id, action=action)), emit)


@pytest.mark.parametrize("ai", [False, True])
def test_real_worker_capture_preview_edit_history_and_persistence(tmp_path, ai):
    async def run():
        session = fixture(); before = session.snapshot()
        await commands.handle_assembly_command(session, request(session), emit)
        assert session.preview and session.snapshot() == before
        assert session.preview.message.attachment_contacts[0].area_mm2 == pytest.approx(100 * 3.141592653589793)
        await decide(session, "cancel")
        assert session.snapshot() == before and not session.history
        await commands.handle_assembly_command(session, request(session), emit)
        await decide(session)
        accepted = session.snapshot()
        cap = commands.capabilities(session, AssemblyCapabilitiesRequest(message_id="cap", version=2, ledger_id="root", owner_path=[]))
        assert not cap.can_edit and cap.reason and "version 3" in cap.reason
        assert commands.capabilities(session, AssemblyCapabilitiesRequest(message_id="cap", version=3, ledger_id="root", owner_path=[])).can_edit
        edit = DesignCommand.model_validate(dict(message_id="edit", base_revision=session.revision, ledger_id="plate", scope="source",
            operations=[dict(op="set_dimension", feature_id="feat_stock", parameter="height", value=12)]))
        if ai:
            from unittest.mock import AsyncMock
            from cadenza_backend.design_prompts import handle_design_prompt
            from cadenza_contracts.messages import DesignPrompt
            class Offline:
                complete_json = AsyncMock(return_value=dict(kind="proposal", description="Set height to 12 mm",
                    operations=[op.model_dump(mode="json") for op in edit.operations], assumptions=[]))
            client = Offline()
            await handle_design_prompt(session, DesignPrompt(message_id="ai_edit", base_revision=session.revision,
                ledger_id="plate", scope="source", feature_id="feat_stock", prompt="Set height to 12 mm"), emit, client=client)
            assert client.complete_json.await_count == 1
        else:
            await handle_design_command(session, edit, emit)
        assert session.snapshot() == accepted and session.preview
        assert len(session.preview.message.attachment_contacts) == 1
        repo = ProjectRepository(tmp_path / "planar.sqlite3")
        doc = ProjectDocument(document_version=document_version([session.snapshot(), *session.history, *session.redo]),
            name="Planar", primary_ledger_id="root", ledgers=session.snapshot(), history=session.history, redo=session.redo)
        pid, _ = repo.save(doc, None, 0)
        restored, _ = ProjectRepository(tmp_path / "planar.sqlite3").load(pid)
        assert restored.document_version == 2 and restored.ledgers == accepted
        await decide(session)
        context = await inspect(session.store.ledger, dict(query_type="instance_context",
            component_path=["comp_spacer"], source_ledger_id="spacer"), session.snapshot())
        assert context["source_to_project"][11] == pytest.approx(12)
        with pytest.raises(ValidationError): load_document({**doc.model_dump(mode="json"), "document_version": 1})
        stable = session.snapshot(); history = list(session.history)
        invalid = DesignCommand.model_validate(dict(message_id="shrink", base_revision=session.revision,
            ledger_id="plate", scope="source", operations=[dict(op="set_dimension", feature_id="feat_stock", parameter="length", value=40)]))
        with pytest.raises(CadenzaError, match="outside"):
            await handle_design_command(session, invalid, emit)
        assert session.snapshot() == stable and session.history == history and session.preview is None
        # Only history contains the attachment; still incompatible with old readers.
        assert document_version([before, accepted]) == 2
    asyncio.run(run())


@pytest.mark.parametrize("corruption", ["point", "normal", "source", "path"])
def test_native_admission_failure_preserves_accepted_state(corruption):
    async def run():
        session = fixture(); before = session.snapshot(); op = deepcopy(operation())
        if corruption == "point": op["target_a"]["point"][2] += 1
        if corruption == "normal": op["target_a"]["normal"] = [0, 0, -1]
        if corruption == "source": op["target_a"]["reference"].update(ledger_id="spacer", source_ledger_id="spacer")
        req = request(session, op)
        if corruption == "path": req = req.model_copy(update={"owner_path": ["comp_missing"]})
        with pytest.raises(CadenzaError): await commands.handle_assembly_command(session, req, emit)
        assert session.snapshot() == before and session.preview is None and not session.history
    asyncio.run(run())


def test_revision_change_during_capture_cannot_publish(monkeypatch):
    from unittest.mock import AsyncMock
    async def run():
        session = fixture(); before = session.snapshot(); req = request(session)
        async def capture(*args):
            session.store.ledger = session.store.ledger.model_copy(update={"revision": 1})
            return {}
        build = AsyncMock()
        monkeypatch.setattr(commands, "inspect", capture)
        monkeypatch.setattr(commands, "build_design", build)
        with pytest.raises(CadenzaError, match="changed during face capture"):
            await commands.handle_assembly_command(session, req, emit)
        build.assert_not_called()
        assert session.preview is None and not session.history
        assert session.snapshot()["root"].assembly == before["root"].assembly
    asyncio.run(run())


def test_planar_attachment_ai_edit_refuses_before_inference():
    from unittest.mock import AsyncMock
    from cadenza_backend.assembly_prompts import handle_assembly_prompt
    from cadenza_contracts.messages import AssemblyPrompt
    from cadenza_contracts.ledger import Joint
    async def run():
        session = fixture(); assembly = session.store.ledger.assembly
        assert assembly is not None
        op = operation()
        assembly.joints.append(Joint.model_validate(dict(id="joint_existing", kind="rigid",
            component_a=op["component_a"], component_b=op["component_b"], twist_deg=0,
            target_a=dict(mode="planar_face", reference=op["target_a"]["reference"], uv=[-30, 0]),
            target_b=dict(mode="planar_face", reference=op["target_b"]["reference"], uv=[0, 0]))))
        before = session.snapshot()
        class Offline:
            complete_json = AsyncMock()
        client = Offline()
        req = AssemblyPrompt.model_validate(dict(message_id="edit", base_revision=session.revision,
            ledger_id="root", owner_path=[], scope="assembly_definition", prompt="Move the attachment",
            target=dict(kind="attachment", component_a=op["component_a"], component_b=op["component_b"], joint_id="joint_existing")))
        with pytest.raises(CadenzaError, match="Use Attach faces"):
            await handle_assembly_prompt(session, req, emit, client=client)
        client.complete_json.assert_not_called()
        assert session.snapshot() == before and session.preview is None
    asyncio.run(run())
