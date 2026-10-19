"""Occurrence isolation uses native workers and the existing acceptance transaction."""
import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError, TypeAdapter
from cadenza_contracts.assembly_commands import AssemblyCommand, AssemblyCapabilitiesRequest, CoordinateAssemblyOperation
from cadenza_contracts.errors import CadenzaError, ErrorCode
from cadenza_contracts.ledger import Ledger, Component, Joint
from cadenza_contracts.surface_reference import SurfaceReference
from cadenza_contracts.design_commands import DesignCommand
from cadenza_contracts.project import ProjectDocument, document_version
from cadenza_backend import assembly_commands as commands
from cadenza_backend.instance_independence import prepare, validate_delta, verify_measurements, verify
from cadenza_backend.geometry_bridge import BuildOutcome
from cadenza_backend.session import LedgerStore
from cadenza_backend.projects import ProjectRepository
from cadenza_backend.design_commands import handle_design_command
from cadenza_backend.geometry_jobs import inspect
from .test_planar_attachment_commands import fixture, emit, decide, request as attachment_request


def setup(nested=False):
    session = fixture()
    root = session.store.ledger
    assert root.assembly
    root.assembly.components.append(Component.model_validate(dict(id="comp_right", name="RightSpacer", sub_assembly_id="spacer", placement=dict(origin=[30, 0, 15]))))
    if nested:
        inner = root.model_copy(deep=True, update={"project_id": "inner"})
        session.stores["inner"] = LedgerStore(inner)
        outer = Ledger.model_validate(dict(project_id="root", assembly=dict(components=[
            dict(id="comp_one", sub_assembly_id="inner", placement=dict(rotation_deg=[0, 0, 30])),
            dict(id="comp_two", sub_assembly_id="inner", placement=dict(origin=[200, 0, 0]))])))
        session.stores["root"] = LedgerStore(outer)
    return session


def request(session, nested=False, **overrides):
    data: dict[str, Any] = dict(message_id="independent", base_revision=session.revision,
        ledger_id="inner" if nested else "root", owner_path=["comp_one"] if nested else [], scope="occurrence",
        operations=[dict(op="make_independent", component_id="comp_spacer", name="Custom spacer")])
    data.update(overrides)
    return AssemblyCommand.model_validate(data)


@pytest.mark.parametrize("nested", [False, True])
def test_preparation_is_private_and_copies_only_selected_branch(nested):
    session = setup(nested); before = session.snapshot()
    result = prepare(session, request(session, nested))
    assert session.snapshot() == before and session.preview is None
    assert len(result.ledgers) == len(before) + (2 if nested else 1)
    rows = commands.assembly_instances(result.ledgers, "root")
    assert [r.path for r in rows if r.source_id == result.new_source_id] == [tuple(result.path)]
    for lid in before:
        if lid != "root":
            assert before[lid].model_dump(exclude={"revision"}) == result.ledgers[lid].model_dump(exclude={"revision"})
    assert result.ledgers[result.new_source_id].features == before["spacer"].features


@pytest.mark.parametrize("change", ["dimension", "placement", "metadata", "remove", "extra"])
def test_delta_rejects_mutated_candidate(change):
    session = setup(True); before = session.snapshot(); result = prepare(session, request(session, True))
    if change == "dimension":
        feature = result.ledgers[result.new_source_id].features[0]
        assert feature.kind == "cylinder"
        feature.parameters.height = 99
    if change == "placement": result.ledgers[result.new_source_id].features[0].placement.origin = [1, 0, 0]
    if change == "metadata": result.ledgers["spacer"].metadata.name = "Changed original"
    if change == "remove": del result.ledgers["plate"]
    if change == "extra": result.ledgers["extra"] = Ledger(project_id="extra")
    with pytest.raises(CadenzaError): validate_delta(before, result, "root")


@pytest.mark.parametrize("scope,operations", [
    ("assembly_definition", [dict(op="make_independent", component_id="comp_spacer", name="New")]),
    ("occurrence", [dict(op="rename_instance", component_id="comp_spacer", name="New")]),
    ("occurrence", [dict(op="make_independent", component_id="comp_spacer", name="New"), dict(op="remove_instance", component_id="comp_right")]),
    ("occurrence", [dict(op="make_independent", component_id="comp_spacer", name=" ")]),
])
def test_strict_contract(scope, operations):
    with pytest.raises(ValidationError): request(setup(), scope=scope, operations=operations)


def test_agent_union_and_legacy_discovery_stay_bounded():
    session = setup()
    with pytest.raises(ValidationError): TypeAdapter(CoordinateAssemblyOperation).validate_python(request(session).operations[0].model_dump())
    for version in (1, 2, 3, 4):
        args = {} if version == 1 else dict(version=version, ledger_id="root", owner_path=[])
        cap = commands.capabilities(session, AssemblyCapabilitiesRequest.model_validate(dict(message_id="cap", **args)))
        assert ("make_independent" in cap.operations) == (version == 4)
        assert ("owner_path" in cap.model_dump()) == (version != 1)


@pytest.mark.parametrize("case", ["duplicate", "owner", "path", "missing", "assembly", "parts_limit", "definitions_limit", "relative", "mixed_owner"])
def test_admission_failures_preserve_state(case):
    session = setup(True); kwargs: dict[str, Any] = {}
    if case == "duplicate": kwargs["operations"] = [dict(op="make_independent", component_id="comp_spacer", name=" sPaCeR ")]
    if case == "owner": kwargs["ledger_id"] = "root"
    if case == "path": kwargs["owner_path"] = ["comp_missing"]
    if case == "missing": kwargs["operations"] = [dict(op="make_independent", component_id="comp_missing", name="New")]
    if case == "assembly": kwargs.update(ledger_id="root", owner_path=[], operations=[dict(op="make_independent", component_id="comp_one", name="New")])
    if case == "parts_limit":
        for i in range(30): session.stores[f"extra{i}"] = LedgerStore(session.stores["spacer"].ledger.model_copy(deep=True, update={"project_id":f"extra{i}"}))
    if case == "definitions_limit":
        for i in range(7): session.stores[f"extra{i}"] = LedgerStore(session.stores["inner"].ledger.model_copy(deep=True, update={"project_id":f"extra{i}"}))
    if case == "relative":
        assembly = session.stores["inner"].ledger.assembly
        assert assembly
        assembly.components[0].placement.relative_to = "feat_stock"
    if case == "mixed_owner": session.stores["inner"].ledger.features = session.stores["spacer"].ledger.features
    before = session.snapshot()
    with pytest.raises(CadenzaError): prepare(session, request(session, True, **kwargs))
    assert session.snapshot() == before


def reference(owner="spacer", source="spacer", path=None):
    return SurfaceReference.model_validate(dict(ledger_id=owner, source_ledger_id=source,
        component_path=path or [], feature_id="feat_stock", feature_kind="cylinder", operation="add", role="top_face"))


def test_reference_remapping_is_path_specific_and_retains_originals():
    session = setup(True)
    session.stores["spacer"].ledger.surface_references["ref_own"] = reference()
    session.stores["inner"].ledger.surface_references.update(ref_left=reference("inner", path=["comp_spacer"]), ref_right=reference("inner", path=["comp_right"]))
    session.store.ledger.surface_references.update(ref_left=reference("root", path=["comp_one", "comp_spacer"]), ref_other=reference("root", path=["comp_two", "comp_spacer"]))
    result = prepare(session, request(session, True)); new = result.new_source_id
    assert result.ledgers[new].surface_references["ref_own"].ledger_id == new
    assert result.ledgers["root"].surface_references["ref_left"].source_ledger_id == new
    assert result.ledgers["root"].surface_references["ref_other"].source_ledger_id == "spacer"
    inner = result.ledgers[result.copies["inner"]].surface_references
    assert inner["ref_left"].source_ledger_id == new and inner["ref_right"].source_ledger_id == "spacer"
    assert result.ledgers["inner"].surface_references == session.stores["inner"].ledger.surface_references
    session.stores["plate"].ledger.surface_references["ref_foreign"] = reference("root", path=["comp_one", "comp_spacer"])
    with pytest.raises(CadenzaError, match="external saved surface"): prepare(session, request(session, True))


@pytest.mark.parametrize("failure", ["stub", "missing", "bounds", "volume", "nan"])
def test_missing_or_changed_native_measurements_rejected(failure):
    original = BuildOutcome(ok=True, stub=False, detail="ok", volume_mm3=10, bbox={"min":[0,0,0], "max":[10,10,10]})
    changed = replace(original, **{"stub":dict(stub=True), "missing":dict(bbox=None), "bounds":dict(bbox={"min":[0,0,0], "max":[10,10,11]}),
        "volume":dict(volume_mm3=11), "nan":dict(volume_mm3=float("nan"))}[failure])
    with pytest.raises(CadenzaError): verify_measurements(original, changed)


@pytest.mark.parametrize("nested", [False, True])
def test_real_copy_contacts_edit_cancel_history_and_reopen(tmp_path, nested):
    async def run():
        session = setup()
        await commands.handle_assembly_command(session, attachment_request(session), emit)
        await decide(session)
        if nested:
            inner = session.store.ledger.model_copy(deep=True, update={"project_id":"inner"})
            outer = setup(True).store.ledger.model_copy(update={"revision":session.revision})
            session.stores["inner"], session.stores["root"] = LedgerStore(inner), LedgerStore(outer)
        session.stores["spacer"].ledger.surface_references["ref_top"] = reference()
        before = session.snapshot(); history = list(session.history)
        await commands.handle_assembly_command(session, request(session, nested), emit)
        assert session.preview and session.preview.message.independence and session.preview.outcome.step and session.preview.outcome.glb
        assert session.snapshot() == before and session.history == history
        assert session.preview.message.attachment_contacts
        repo = ProjectRepository(tmp_path / "independent.sqlite3")
        def save():
            doc = ProjectDocument(document_version=document_version([session.snapshot(), *session.history, *session.redo]),
                name="Independent", primary_ledger_id="root", ledgers=session.snapshot(), history=session.history, redo=session.redo)
            pid, _ = repo.save(doc, None, 0)
            return ProjectRepository(tmp_path / "independent.sqlite3").load(pid)[0]
        assert save().ledgers == before  # Saving during preview never leaks the copy.
        await decide(session, "cancel")
        assert session.snapshot() == before
        await commands.handle_assembly_command(session, request(session, nested), emit)
        assert session.preview and session.preview.message.independence
        new = session.preview.message.independence.new_source_id
        await decide(session)
        assert len(session.history) == len(history) + 1 and new in session.stores
        accepted = session.snapshot()
        assert save().ledgers == accepted
        staged = session.stage_undo(); assert staged
        session.commit(staged[0], undo_index=staged[1]); assert new not in session.stores
        redo = session.stage_redo(); assert redo
        session.commit(redo, redo=True); assert new in session.stores
        edit = DesignCommand.model_validate(dict(message_id="edit", base_revision=session.revision, ledger_id=new, scope="source",
            operations=[dict(op="set_dimension", feature_id="feat_stock", parameter="diameter", value=16)]))
        await handle_design_command(session, edit, emit)
        assert session.preview and session.preview.message.attachment_contacts
        await decide(session)
        assert session.stores["spacer"].ledger.features == before["spacer"].features
        assert session.stores[new].ledger.features != before["spacer"].features
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["worker", "stub", "stale", "cancel"])
def test_worker_failure_and_stale_generation_publish_nothing(monkeypatch, failure):
    async def run():
        session = setup(); before = session.snapshot()
        if failure == "worker":
            build = AsyncMock(side_effect=CadenzaError(ErrorCode.GEOMETRY_KERNEL_FAILURE, "worker failed"))
        elif failure == "cancel":
            build = AsyncMock(side_effect=asyncio.CancelledError())
        else:
            build = AsyncMock(return_value=BuildOutcome(ok=True, stub=True, detail="stub"))
        monkeypatch.setattr(commands, "build_design", build)
        req = request(session, base_revision=99) if failure == "stale" else request(session)
        with pytest.raises((CadenzaError, asyncio.CancelledError)): await commands.handle_assembly_command(session, req, emit)
        assert session.snapshot() == before and session.preview is None and not session.history
    asyncio.run(run())


def test_same_source_attachment_endpoints_and_offline_edit_follow_only_selected_geometry():
    from cadenza_backend.design_prompts import handle_design_prompt
    from cadenza_contracts.messages import DesignPrompt
    async def run():
        session = setup()
        await commands.handle_assembly_command(session, attachment_request(session), emit); await decide(session)
        op = dict(op="add_planar_attachment", component_a="comp_spacer", component_b="comp_right",
            target_a=dict(reference=reference().model_dump(mode="json"), point=[0,0,30], normal=[0,0,1]),
            target_b=dict(reference=reference().model_copy(update={"role":"bottom_face"}).model_dump(mode="json"), point=[0,0,0], normal=[0,0,-1]))
        await commands.handle_assembly_command(session, attachment_request(session, op), emit); await decide(session)
        before = session.snapshot()
        await commands.handle_assembly_command(session, request(session), emit)
        assert session.preview and session.preview.message.independence
        new = session.preview.message.independence.new_source_id
        assembly = session.preview.ledgers["root"].assembly; assert assembly
        joint = assembly.joints[1]
        assert joint.target_a.mode == "planar_face"
        assert joint.target_b.mode == "planar_face"
        assert joint.target_a.reference.source_ledger_id == new and joint.target_b.reference.source_ledger_id == "spacer"
        await decide(session)
        class Offline:
            complete_json = AsyncMock(return_value=dict(kind="proposal", description="Set height to 34 mm", assumptions=[],
                operations=[dict(op="set_dimension", feature_id="feat_stock", parameter="height", value=34)]))
        client = Offline()
        await handle_design_prompt(session, DesignPrompt(message_id="offline", base_revision=session.revision, ledger_id=new,
            scope="source", feature_id="feat_stock", prompt="Set height to 34 mm"), emit, client=client)
        assert client.complete_json.await_count == 1 and session.preview and len(session.preview.message.attachment_contacts) == 2
        await decide(session)
        pose = await inspect(session.store.ledger, dict(query_type="instance_context", component_path=["comp_right"], source_ledger_id="spacer"), session.snapshot())
        assert pose["source_to_project"][11] == pytest.approx(49)
        assert session.stores["spacer"].ledger.features == before["spacer"].features
        stable = session.snapshot()
        invalid = DesignCommand.model_validate(dict(message_id="invalid", base_revision=session.revision, ledger_id=new, scope="source",
            operations=[dict(op="set_suppressed", feature_id="feat_stock", suppressed=True)]))
        with pytest.raises(CadenzaError): await handle_design_command(session, invalid, emit)
        assert session.snapshot() == stable and session.preview is None
    asyncio.run(run())


def test_unresolved_annotation_stays_unresolved_without_blocking_native_copy():
    async def run():
        session = setup()
        session.stores["spacer"].ledger.surface_references["ref_missing"] = reference().model_copy(update={"feature_id":"feat_missing"})
        # Unrelated foreign/root annotation is unchanged, not treated as a failed attachment.
        session.store.ledger.surface_references["ref_foreign"] = reference()
        before = session.snapshot(); result = prepare(session, request(session))
        checks = await verify(before, result, "root")
        assert any("was not repaired" in text for text in checks)
        assert result.ledgers["root"].surface_references == before["root"].surface_references
    asyncio.run(run())


def test_revision_change_during_native_checks_and_stale_apply_reject(monkeypatch):
    import cadenza_backend.instance_independence as module
    async def run():
        session = setup()
        real_verify = module.verify
        async def changing(*args):
            result = await real_verify(*args)
            session.store.ledger.revision += 1
            return result
        monkeypatch.setattr(module, "verify", changing)
        with pytest.raises(CadenzaError, match="changed while preparing"):
            await commands.handle_assembly_command(session, request(session), emit)
        assert session.preview is None and len(session.stores) == 3
        monkeypatch.setattr(module, "verify", real_verify)
        await commands.handle_assembly_command(session, request(session), emit)
        session.store.ledger.revision += 1
        with pytest.raises(CadenzaError, match="expired|design changed"): await decide(session)
        assert session.preview is None and len(session.stores) == 3
    asyncio.run(run())


def test_profile_copy_preserves_local_ids_constraints_bindings_and_saved_face():
    async def run():
        profile = Ledger.model_validate_json((Path(__file__).parents[2] / "shared/contract_fixtures/profile_plate.json").read_text())
        profile.metadata.name = "Profile plate"
        profile.surface_references["ref_cap"] = SurfaceReference.model_validate(dict(ledger_id=profile.project_id,
            source_ledger_id=profile.project_id, feature_id="feat_extrusion", feature_kind="extrude", profile_id="feat_profile",
            operation="add", role="top_face"))
        session = setup()
        root = Ledger.model_validate(dict(project_id="root", assembly=dict(components=[
            dict(id="comp_spacer", sub_assembly_id=profile.project_id),
            dict(id="comp_right", sub_assembly_id=profile.project_id, placement=dict(origin=[200,0,0]))])))
        session.stores = {"root":LedgerStore(root),profile.project_id:LedgerStore(profile)}
        await commands.handle_assembly_command(session, request(session), emit)
        assert session.preview and session.preview.message.independence
        copy = session.preview.ledgers[session.preview.message.independence.new_source_id]
        assert copy.features == profile.features and copy.relationships == profile.relationships
        assert copy.surface_references["ref_cap"].profile_id == "feat_profile"
    asyncio.run(run())


def test_native_inspection_cannot_silently_skip_reference_errors(monkeypatch):
    import cadenza_backend.instance_independence as module
    async def run():
        session = setup()
        session.stores["spacer"].ledger.surface_references["ref_top"] = reference()
        result = prepare(session, request(session))
        async def incomplete(ledger, query, documents):
            if query["query_type"] == "instance_context": return dict(source_to_project=[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1])
            return {"error":"unknown query"}
        monkeypatch.setattr(module,"inspect",incomplete)
        with pytest.raises(CadenzaError,match="incomplete evidence"): await verify(session.snapshot(),result,"root")
    asyncio.run(run())


def test_deeper_repeated_branch_preserves_mixed_coordinate_and_planar_links():
    async def run():
        session = setup()
        await commands.handle_assembly_command(session, attachment_request(session), emit); await decide(session)
        inner = session.store.ledger.model_copy(deep=True, update={"project_id":"inner"})
        assert inner.assembly
        joint = Joint.model_validate(dict(id="joint_coordinate",kind="rigid",component_a="comp_spacer",component_b="comp_right",
            target_a=dict(point=[-30,0,45]),target_b=dict(point=[30,0,15])))
        inner.assembly.joints.append(joint)
        inner.surface_references["ref_left"] = reference("inner",path=["comp_spacer"])
        parent = Ledger.model_validate(dict(project_id="parent",revision=session.revision,assembly=dict(components=[
            dict(id="comp_nested",sub_assembly_id="inner",placement=dict(rotation_deg=[0,0,20]))])))
        root = Ledger.model_validate(dict(project_id="root",revision=session.revision,assembly=dict(components=[
            dict(id="comp_one",sub_assembly_id="parent",placement=dict(rotation_deg=[0,0,25])),
            dict(id="comp_two",sub_assembly_id="parent",placement=dict(origin=[400,0,0]))])))
        session.stores.update(root=LedgerStore(root),inner=LedgerStore(inner),parent=LedgerStore(parent))
        before = session.snapshot()
        await commands.handle_assembly_command(session, request(session, ledger_id="inner",owner_path=["comp_one","comp_nested"]), emit)
        assert session.preview and session.preview.message.independence
        result = session.preview.message.independence
        assert result.copied_assemblies == 2
        rows = commands.assembly_instances(session.preview.ledgers,"root")
        assert [row.path for row in rows if row.source_id == result.new_source_id] == [("comp_one","comp_nested","comp_spacer")]
        private_parent = next(row.source_id for row in rows if row.path == ("comp_one","comp_nested"))
        assert private_parent
        copied_owner = session.preview.ledgers[private_parent]
        assert copied_owner.assembly and copied_owner.assembly.joints[1] == joint
        assert copied_owner.surface_references["ref_left"].source_ledger_id == result.new_source_id
        assert session.snapshot() == before
    asyncio.run(run())
