"""Native source-face admission and connected candidate assembly regressions."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from cadenza_backend import design_prompts
from cadenza_backend.design_commands import handle_design_command
from cadenza_backend.edit_preview import decide_preview
from cadenza_backend.geometry_jobs import inspect, build_design
from cadenza_backend.part_views import view_part
from cadenza_backend.session import Session, LedgerStore
from cadenza_contracts.design_commands import DesignCommand
from cadenza_contracts.errors import CadenzaError, ErrorCode
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.messages import DesignPrompt, PreviewDecision
from cadenza_contracts.part_commands import PartViewRequest


async def emit(_):
    pass


def fixture():
    base = Ledger.model_validate(dict(project_id="prj_base", metadata=dict(name="BasePlate"), features=[
        dict(id="feat_base", kind="box", parameters=dict(length=100, width=60, height=15))]))
    spacer = Ledger.model_validate(dict(project_id="prj_spacer", metadata=dict(name="Spacer"), features=[
        dict(id="feat_spacer", kind="cylinder", parameters=dict(diameter=20, height=30))]))
    root = Ledger.model_validate(dict(project_id="prj_root", metadata=dict(name="TestFixture"), assembly=dict(components=[
        dict(id="comp_base", name="Base", sub_assembly_id=base.project_id),
        dict(id="comp_left", name="LeftSpacer", sub_assembly_id=spacer.project_id, placement=dict(origin=[-30, 0, 15])),
        dict(id="comp_right", name="RightSpacer", sub_assembly_id=spacer.project_id, placement=dict(origin=[30, 0, 15]))])))
    return Session(primary_ledger_id=root.project_id, stores={l.project_id: LedgerStore(l) for l in (root, base, spacer)})


async def face_request(session, mode="edit_feature", **updates):
    result = await inspect(session.stores["prj_base"].ledger, dict(query_type="capture_surface_reference",
        point=[0, 0, 15], normal=[0, 0, 1]), session.snapshot())
    raw = dict(message_id="face_edit", base_revision=session.revision, scope="source", ledger_id="prj_base",
        feature_id="feat_base" if mode == "edit_feature" else None, prompt="Set height to 12 mm",
        face=dict(mode=mode, reference=result["reference"], point=[0, 0, 15], normal=[0, 0, 1], component_path=["comp_base"]))
    raw.update(updates)
    return DesignPrompt.model_validate(raw)


def provider(operations):
    class Offline:
        complete_json = AsyncMock(return_value=dict(kind="proposal", description="Proposed edit", operations=operations, assumptions=[]))
    return Offline()


@pytest.mark.parametrize("ai", [False, True])
@pytest.mark.parametrize("height", [12, 18])
def test_numeric_and_face_edit_whole_assembly_atomicity(ai, height):
    async def run():
        session = fixture(); before = session.snapshot()
        await view_part(session, PartViewRequest(message_id="view", base_revision=0, ledger_id="prj_base", component_path=["comp_base"]), emit)
        ops = [dict(op="set_dimension", feature_id="feat_base", parameter="height", value=height)]
        async def attempt():
            if ai:
                await design_prompts.handle_design_prompt(session, await face_request(session, prompt=f"Set height to {height} mm"), emit, client=provider(ops))
            else:
                await handle_design_command(session, DesignCommand.model_validate(dict(message_id="numeric", base_revision=0, ledger_id="prj_base", operations=ops)), emit)
        if height == 18:
            with pytest.raises(CadenzaError) as caught: await attempt()
            report = caught.value.interference
            assert report and report.definition_id == "prj_root"
            assert {tuple(report.component_a), tuple(report.component_b)} == {("comp_base",), ("comp_left",)}
            assert report.volume_mm3 == pytest.approx(3 * 100 * 3.141592653589793)
            assert session.preview is None and not session.history and not session.redo
        else:
            await attempt(); preview = session.preview
            assert preview and preview.message.context_component_path == ["comp_base"]
            assert preview.message.source_to_project == [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
            plate = preview.ledgers["prj_base"].features[0]
            assert plate.kind == "box" and plate.parameters.height == 12
            assert preview.ledgers["prj_root"].assembly == before["prj_root"].assembly
            assert preview.display_outcome and preview.display_outcome.volume_mm3 == pytest.approx(72000)
            assert preview.outcome.volume_mm3 == pytest.approx(72000 + 6000 * 3.141592653589793)
            await decide_preview(session, PreviewDecision(message_id="cancel", base_revision=0, preview_id=preview.id, action="cancel"), emit)
        assert session.snapshot() == before and session.revision == 0
    asyncio.run(run())


@pytest.mark.parametrize("bad", ["point", "normal", "reference", "path", "stale"])
def test_face_preflight_rejects_before_provider(bad):
    async def run():
        session = fixture(); req = await face_request(session); raw = req.model_dump(mode="json")
        if bad == "point": raw["face"]["point"] = [0, 0, 999]
        if bad == "normal": raw["face"]["normal"] = [0, 0, -1]
        if bad == "reference": raw["face"]["reference"]["role"] = "bottom_face"
        if bad == "path": raw["face"]["component_path"] = ["comp_left"]
        if bad == "stale": raw["base_revision"] = 9
        client = provider([]); before = session.snapshot()
        with pytest.raises(CadenzaError):
            await design_prompts.handle_design_prompt(session, DesignPrompt.model_validate(raw), emit, client=client)
        client.complete_json.assert_not_called()
        assert session.snapshot() == before and session.preview is None
    asyncio.run(run())


@pytest.mark.parametrize("variant", ["valid", "boss", "pad", "remote", "wrong_direction", "wrong_mode", "pattern"])
def test_face_additions_require_native_placement_and_intent(variant):
    async def run():
        session = fixture(); before = session.snapshot()
        op: dict = dict(op="add_feature", kind="hole", operation="subtract", name="Mounting hole",
            parameters=dict(diameter=6, through=True, depth=None), placement=dict(origin=[0, 0, 15], rotation_deg=[0, 0, 0]))
        if variant == "remote": op["placement"]["origin"] = [0, 0, 25]
        if variant == "wrong_direction": op["placement"]["rotation_deg"] = [180, 0, 0]
        if variant == "pattern": op = dict(op="add_linear_pattern", target_feature="feat_base", name="Copies", count=2, direction="+x", spacing=200)
        if variant in ("boss", "pad"):
            op.update(kind="cylinder" if variant == "boss" else "box", operation="add",
                parameters=dict(diameter=6, height=4) if variant == "boss" else dict(length=8, width=6, height=4))
        client = provider([op])
        req = await face_request(session, "edit_feature" if variant == "wrong_mode" else "add_on_face",
            prompt="Add a boss or pad with the specified dimensions and orientation." if variant in ("boss", "pad")
            else "Add one 6 mm through hole at source X=0, Y=0 on this face.")
        if variant in ("valid", "boss", "pad"):
            await design_prompts.handle_design_prompt(session, req, emit, client=client)
            assert session.preview and session.preview.display_outcome
            volume = {"valid": 90000 - 135 * 3.141592653589793,
                "boss": 90000 + 36 * 3.141592653589793, "pad": 90000 + 192}[variant]
            assert session.preview.display_outcome.volume_mm3 == pytest.approx(volume)
            context = json.loads(client.complete_json.call_args.kwargs["user"])
            assert context["selected_face"]["face"]["planar"]
            assert session.preview.message.context_component_path == ["comp_base"]
        else:
            with pytest.raises(CadenzaError): await design_prompts.handle_design_prompt(session, req, emit, client=client)
            assert session.preview is None
        assert session.snapshot() == before
    asyncio.run(run())


def test_shared_source_candidate_updates_both_instances():
    async def run():
        session = fixture(); before = session.snapshot()
        await view_part(session, PartViewRequest(message_id="view", base_revision=0, ledger_id="prj_spacer", component_path=["comp_right"]), emit)
        await handle_design_command(session, DesignCommand.model_validate(dict(message_id="numeric", base_revision=0,
            ledger_id="prj_spacer", operations=[dict(op="set_dimension", feature_id="feat_spacer", parameter="diameter", value=16)])), emit)
        preview = session.preview
        assert preview and preview.message.context_component_path == ["comp_right"]
        assert preview.message.source_to_project and preview.message.source_to_project[3] == 30
        assert preview.outcome.volume_mm3 == pytest.approx(90000 + 2 * 64 * 30 * 3.141592653589793)
        assert any("2 instance(s)" in check for check in preview.message.checks)
        assert session.snapshot() == before
        await decide_preview(session, PreviewDecision(message_id="apply", base_revision=0, preview_id=preview.id, action="apply"), emit)
        assert session.revision == 1 and len(session.history) == 1
        spacer = session.stores["prj_spacer"].ledger.features[0]
        assert spacer.kind == "cylinder" and spacer.parameters.diameter == 16
    asyncio.run(run())


def test_secondary_interference_keeps_checked_definition_identity():
    async def run():
        session = fixture()
        inner = session.stores["prj_root"].ledger.model_copy(deep=True, update={"project_id": "prj_unplaced"})
        assert inner.assembly is not None
        inner.assembly.components[1].placement.origin = [0, 0, 0]
        docs = session.snapshot(); docs[inner.project_id] = inner
        outcome = await build_design(docs, session.primary_ledger_id)
        assert not outcome.ok and outcome.interference and outcome.interference["definition_id"] == inner.project_id
        assert outcome.interference["component_a"] == ["comp_base"]
        assert outcome.interference["component_b"] == ["comp_left"]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["clarification", "provider", "race", "ambiguous"])
def test_face_failure_and_clarification_preserve_accepted_state(failure, monkeypatch):
    async def run():
        session = fixture(); req = await face_request(session); before = session.snapshot()
        client = provider([dict(op="set_dimension", feature_id="feat_base", parameter="height", value=12)])
        frames = []
        async def collect(frame): frames.append(frame)
        if failure == "clarification":
            client.complete_json.return_value = dict(kind="clarification", question="What thickness should the plate have?")
            await design_prompts.handle_design_prompt(session, req, collect, client=client)
            assert any(getattr(frame, "needs_clarification", False) for frame in frames)
        elif failure == "provider":
            from cadenza_backend.agents.model_client import AIError
            client.complete_json.side_effect = AIError("offline failure")
            with pytest.raises(CadenzaError): await design_prompts.handle_design_prompt(session, req, collect, client=client)
        else:
            original = design_prompts.inspect
            async def inspect_changed(*args):
                result = await original(*args)
                if failure == "ambiguous": return dict(status="ambiguous")
                session.stores["prj_base"].ledger.metadata.name = "Externally changed"
                return result
            monkeypatch.setattr(design_prompts, "inspect", inspect_changed)
            with pytest.raises(CadenzaError): await design_prompts.handle_design_prompt(session, req, collect, client=client)
            client.complete_json.assert_not_called()
            if failure == "race": before["prj_base"].metadata.name = "Externally changed"
        assert session.snapshot() == before and session.preview is None and not session.history
    asyncio.run(run())


def test_candidate_context_failure_falls_back_to_whole_candidate(monkeypatch):
    from cadenza_backend import edit_preview
    async def run():
        session = fixture()
        await view_part(session, PartViewRequest(message_id="view", base_revision=0, ledger_id="prj_base", component_path=["comp_base"]), emit)
        original = edit_preview.inspect
        async def fail_context(*args):
            if args[1]["query_type"] == "instance_context": raise CadenzaError(ErrorCode.GEOMETRY_TIMEOUT, "injected")
            return await original(*args)
        monkeypatch.setattr(edit_preview, "inspect", fail_context)
        await handle_design_command(session, DesignCommand.model_validate(dict(message_id="edit", base_revision=0, ledger_id="prj_base",
            operations=[dict(op="set_dimension", feature_id="feat_base", parameter="height", value=12)])), emit)
        preview = session.preview
        assert preview and preview.message.context_warning
        assert preview.message.context_component_path is None and preview.message.source_to_project is None
        assert preview.message.glb_url and preview.outcome.glb and session.revision == 0
    asyncio.run(run())


def test_save_during_face_preview_excludes_candidate_and_view_context(tmp_path, monkeypatch):
    from cadenza_backend import app as transport
    from cadenza_backend.projects import ProjectRepository
    from cadenza_backend.session import registry
    repo = ProjectRepository(tmp_path / "face-preview.sqlite3")
    monkeypatch.setattr(transport, "projects", repo)
    async def run():
        session = fixture(); registry.register(session)
        try:
            await view_part(session, PartViewRequest(message_id="view", base_revision=0,
                ledger_id="prj_base", component_path=["comp_base"]), emit)
            await design_prompts.handle_design_prompt(session, await face_request(session), emit,
                client=provider([dict(op="set_dimension", feature_id="feat_base", parameter="height", value=12)]))
            assert session.preview and session.source_view
            saved = await transport.save_project(session.id, transport.SaveProject(name="Accepted only", base_revision=0))
            document, _ = ProjectRepository(repo.path).load(saved["id"])
            assert document.ledgers == session.snapshot()
            plate = document.ledgers["prj_base"].features[0]
            assert plate.kind == "box" and plate.parameters.height == 15
            assert not document.history and not document.redo
            assert "context_component_path" not in document.model_dump_json()
        finally:
            registry.drop(session.id)
    asyncio.run(run())
