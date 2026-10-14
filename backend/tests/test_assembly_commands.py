import asyncio
import math
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest

from cadenza_backend import assembly_commands as commands, geometry_bridge
from cadenza_backend.contracts_bridge import CadenzaError, Ledger
from cadenza_backend.ledger_store import LedgerStore, apply_patch
from cadenza_backend.session import Session
from cadenza_backend.edit_preview import changes_between, decide_preview
from cadenza_contracts.assembly_commands import AssemblyCommand
from cadenza_contracts.messages import PreviewDecision


def fixture(variant=0):
    session = Session()
    for index in range(3):
        ledger = Ledger.model_validate(dict(project_id=f"source_{index}", metadata=dict(name=f"Source {index}"),
            features=[dict(id="feat_stock", kind="box", parameters=dict(length=10 + variant + index, width=8, height=4))]))
        session.stores[ledger.project_id] = LedgerStore(ledger)
    return session


def placement(x=0, rotation=None):
    return dict(origin=[x, 0, 0], rotation_deg=rotation or [0, 0, 0])


def add(index=0, x=0, name="First"):
    return dict(op="add_instance", source_ledger_id=f"source_{index}", name=name, placement=placement(x))


def command(session, *ops):
    return AssemblyCommand.model_validate(dict(message_id="asm_test", base_revision=session.revision,
        ledger_id=session.primary_ledger_id, scope="assembly_definition", operations=list(ops)))


def initial(session, count=2):
    return command(session, dict(op="initialize_assembly", name="Test assembly"),
        *[add(i % 3, i * 60, f"Instance {i}") for i in range(count)])


def compiled(session, request):
    return apply_patch(session.store.ledger, commands.compile_command(session, request)).ledger


def established():
    session = fixture()
    ledger = compiled(session, initial(session))
    session.stores[session.primary_ledger_id] = LedgerStore(ledger)
    assert ledger.assembly is not None
    return session, ledger.assembly.components


async def discard(_): pass


def decide(session, action: Literal["apply", "cancel"]="apply"):
    assert session.preview
    asyncio.run(decide_preview(session, PreviewDecision(message_id="decision", base_revision=session.revision,
        preview_id=session.preview.id, action=action), discard))


def test_compile_private_preserves_primary_sources_and_metadata():
    session = fixture(); before = session.snapshot()
    candidate = compiled(session, initial(session, 6))
    assert candidate.assembly is not None
    assert candidate.project_id == session.primary_ledger_id
    assert len({c.id for c in candidate.assembly.components}) == 6
    assert [c.sub_assembly_id for c in candidate.assembly.components].count("source_0") == 2
    assert session.snapshot() == before and not session.history and session.preview is None
    assert candidate.metadata.global_units == session.store.ledger.metadata.global_units


@pytest.mark.parametrize("kind", ["missing", "self", "empty", "populated", "no_init", "init_only", "wrong_owner", "duplicate_name"])
def test_admission_fails_before_build(monkeypatch, kind):
    session = fixture(); request = initial(session)
    raw = request.model_dump(mode="json")
    if kind == "missing": raw["operations"][1]["source_ledger_id"] = "absent"
    if kind == "self": raw["operations"][1]["source_ledger_id"] = session.primary_ledger_id
    if kind == "empty": session.stores["source_0"] = LedgerStore(Ledger(project_id="source_0"))
    if kind == "populated": session.stores[session.primary_ledger_id] = LedgerStore(session.stores["source_0"].ledger.model_copy(update={"project_id": session.primary_ledger_id}))
    if kind == "no_init": raw["operations"] = raw["operations"][1:]
    if kind == "init_only": raw["operations"] = raw["operations"][:1]
    if kind == "wrong_owner": raw["ledger_id"] = "source_0"
    if kind == "duplicate_name": raw["operations"][2]["name"] = " instance 0 "
    build = AsyncMock(); monkeypatch.setattr(commands, "build_design", build); before = session.snapshot()
    with pytest.raises(CadenzaError): asyncio.run(commands.handle_assembly_command(session, AssemblyCommand.model_validate(raw), discard))
    build.assert_not_called(); assert session.snapshot() == before and session.preview is None


@pytest.mark.parametrize("case", ["missing", "same_name", "same_placement", "rename_twice", "edit_remove", "remove_twice", "duplicate_name"])
def test_component_conflicts_are_atomic(case):
    session, components = established(); a, b = components; before = session.snapshot()
    ops = [dict(op="rename_instance", component_id=a.id, name="New")]
    if case == "missing": ops[0]["component_id"] = "comp_absent"
    if case == "same_name": ops[0]["name"] = a.name
    if case == "same_placement": ops = [dict(op="set_instance_placement", component_id=a.id, placement=placement())]
    if case == "rename_twice": ops *= 2
    if case == "edit_remove": ops += [dict(op="remove_instance", component_id=a.id)]
    if case == "remove_twice": ops = [dict(op="remove_instance", component_id=a.id)] * 2
    if case == "duplicate_name": ops = [dict(op="duplicate_instance", component_id=a.id, name=b.name.upper(), placement=placement(200))]
    with pytest.raises(CadenzaError): compiled(session, command(session, *ops))
    assert session.snapshot() == before


def test_rename_place_repeat_remove_id_diffs_and_source_preservation():
    session, (a, b) = established(); before = session.snapshot()
    candidate = compiled(session, command(session,
        dict(op="rename_instance", component_id=a.id, name="Updated"),
        dict(op="set_instance_placement", component_id=a.id, placement=placement(20, [10, 20, 30])),
        dict(op="duplicate_instance", component_id=b.id, name="Repeat", placement=placement(180))))
    assert candidate.assembly is not None
    assert candidate.assembly.components[0].name == "Updated"
    assert candidate.assembly.components[-1].sub_assembly_id == b.sub_assembly_id
    diffs = changes_between(before, {**before, candidate.project_id: candidate})
    assert any(c.path == f"/assembly/components/{a.id}/name" for c in diffs)
    assert any(c.path == f"/assembly/components/{a.id}/placement/rotation_deg" for c in diffs)
    removed = compiled(session, command(session, dict(op="remove_instance", component_id=a.id)))
    assert removed.assembly is not None
    assert len(removed.assembly.components) == 1 and session.snapshot() == before
    with pytest.raises(CadenzaError, match="at least one"):
        compiled(session, command(session, *[dict(op="remove_instance", component_id=c.id) for c in (a,b)]))


@pytest.mark.parametrize("op", ["set_instance_placement", "remove_instance"])
@pytest.mark.parametrize("target", [0, 1])
def test_incoming_and_outgoing_joints_reject(op, target):
    session, components = established(); a, b = components
    raw = session.store.ledger.model_dump(mode="json")
    raw["assembly"]["joints"] = [dict(id="joint_link", kind="rigid", component_a=a.id, component_b=b.id,
        target_a=dict(point=[0,0,4]), target_b=dict(point=[60,0,0]))]
    session.stores[session.primary_ledger_id] = LedgerStore(Ledger.model_validate(raw))
    operation = dict(op=op, component_id=components[target].id)
    if op == "set_instance_placement": operation["placement"] = placement(100)
    with pytest.raises(CadenzaError, match="attachment"): compiled(session, command(session, operation))


def test_component_limit_boundary_and_legacy_eligibility():
    session, components = established(); raw = session.store.ledger.model_dump(mode="json")
    raw["assembly"]["components"] = [{**components[0].model_dump(mode="json"), "id": f"comp_{i}", "name": f"N{i}"} for i in range(31)]
    session.stores[session.primary_ledger_id] = LedgerStore(Ledger.model_validate(raw))
    candidate = compiled(session, command(session, add(name="Boundary")))
    assert candidate.assembly is not None
    assert len(candidate.assembly.components) == 32
    session.stores[session.primary_ledger_id] = LedgerStore(candidate)
    with pytest.raises(CadenzaError, match="32"): compiled(session, command(session, add(name="Over")))
    raw["assembly"]["components"] += [{**raw["assembly"]["components"][0], "id": "comp_more", "name": "More"},
        {**raw["assembly"]["components"][0], "id": "comp_over", "name": "Over"}]
    session.stores[session.primary_ledger_id] = LedgerStore(Ledger.model_validate(raw))
    assert not commands.capabilities(session, "cap").can_edit


def test_nested_occurrences_limits_cycles_and_inline_compatibility():
    session = fixture()
    inner = Ledger.model_validate(dict(project_id="inner", assembly=dict(components=[
        dict(id=f"comp_leaf_{i}", sub_assembly_id="source_0") for i in range(16)])))
    root = Ledger.model_validate(dict(project_id=session.primary_ledger_id, assembly=dict(components=[
        dict(id=f"comp_group_{i}", sub_assembly_id="inner") for i in range(8)])))
    docs = {**session.snapshot(), "inner": inner, root.project_id: root}
    assert len(commands.source_uses(docs, root.project_id, "source_0")) == 128
    raw = root.model_dump(mode="json"); raw["assembly"]["components"].append(dict(id="comp_over", sub_assembly_id="source_1"))
    with pytest.raises(ValueError, match="128"):
        commands.assembly_instances({**docs, root.project_id: Ledger.model_validate(raw)},root.project_id)
    for i in reversed(range(9)):
        docs[f"depth_{i}"] = Ledger.model_validate(dict(project_id=f"depth_{i}",assembly=dict(components=[
            dict(id="comp_child",sub_assembly_id=f"depth_{i+1}" if i<8 else "source_0")])))
    assert len(commands.source_uses(docs,"depth_1","source_0")) == 1
    with pytest.raises(ValueError,match="depth"): commands.assembly_instances(docs,"depth_0")
    cycle=Ledger.model_validate(dict(project_id="cycle",assembly=dict(components=[dict(id="comp_loop",sub_assembly_id="cycle")])))
    with pytest.raises(ValueError,match="cycle"): commands.assembly_instances({"cycle":cycle},"cycle")
    inline=Ledger.model_validate(dict(project_id=session.primary_ledger_id,assembly=dict(
        parts=[dict(id="part_stock",features=session.stores["source_0"].ledger.model_dump()["features"])],
        components=[dict(id="comp_inline",part_id="part_stock",name="Legacy")])))
    session.stores[session.primary_ledger_id] = LedgerStore(inline)
    candidate=compiled(session, command(session,dict(op="duplicate_instance",component_id="comp_inline",name="Copy",placement=placement(50))))
    assert candidate.assembly is not None and inline.assembly is not None
    assert candidate.assembly.parts == inline.assembly.parts
    assert len(candidate.assembly.components)==2 and candidate.assembly.components[-1].part_id=="part_stock"


def test_relative_placement_rejected_and_saved_annotations_preserved_on_removal():
    session, (a,b) = established()
    raw=session.store.ledger.model_dump(mode="json")
    raw["assembly"]["components"][1]["placement"]["relative_to"]="feat_stock"
    session.stores[session.primary_ledger_id]=LedgerStore(Ledger.model_validate(raw))
    for op in [dict(op="duplicate_instance",component_id=b.id,name="Copy",placement=placement(100)),
        dict(op="set_instance_placement",component_id=b.id,placement=placement(100))]:
        with pytest.raises(CadenzaError): compiled(session,command(session,op))
    raw["surface_references"]={"ref_top":dict(ledger_id=session.primary_ledger_id,component_path=[a.id],
        source_ledger_id="source_0",feature_id="feat_stock",feature_kind="box",operation="add",role="top_face")}
    raw["assembly"]["components"][1]["placement"]["relative_to"] = None
    session.stores[session.primary_ledger_id]=LedgerStore(Ledger.model_validate(raw))
    candidate=compiled(session,command(session,dict(op="remove_instance",component_id=a.id)))
    assert candidate.surface_references == session.store.ledger.surface_references
    assert candidate.assembly is not None and all(c.id != a.id for c in candidate.assembly.components)


def test_transport_capabilities_busy_stale_preview_and_cancel(monkeypatch):
    from fastapi.testclient import TestClient
    from cadenza_backend import app as transport
    build=AsyncMock(return_value=geometry_bridge.BuildOutcome(ok=True,stub=False,empty=True,detail="Transport fixture"))
    monkeypatch.setattr(commands,"build_design",build)
    with TestClient(transport.app) as client,client.websocket_connect("/ws") as ws:
        ws.send_json(dict(type="client.hello",contract_version="1.0.0")); ready=ws.receive_json()
        session=transport.registry.get(ready["session_id"]); assert session
        session.stores.update({k:v for k,v in fixture().stores.items() if k.startswith("source_")})
        ws.send_json(dict(type="design.assembly.capabilities",message_id="cap")); cap=ws.receive_json()
        assert cap["can_initialize"] and not cap["can_edit"] and cap["ledger_id"]==session.primary_ledger_id
        request=initial(session).model_dump(mode="json")
        ws.send_json({**request,"base_revision":99}); assert ws.receive_json()["code"]=="REVISION_CONFLICT"
        asyncio.run(session.lock.acquire())
        try:
            ws.send_json(request); assert ws.receive_json()["code"]=="BUSY"
        finally: session.lock.release()
        build.assert_not_called()
        before=session.snapshot(); ws.send_json(request); preview=ws.receive_json()
        assert preview["type"]=="design.preview" and session.snapshot()==before
        ws.send_json(request); assert ws.receive_json()["code"]=="BUSY"
        ws.send_json(dict(type="design.preview_decision",message_id="cancel",preview_id=preview["preview_id"],base_revision=0,action="cancel"))
        assert ws.receive_json()["type"]=="design.preview_closed"
        assert session.snapshot()==before and session.preview is None


@pytest.mark.parametrize("failure", ["failed", "stub", "cancelled", "stale", "busy"])
def test_failed_lifecycle_preserves_state(monkeypatch, failure):
    session, _ = established(); before = session.snapshot(); request = command(session, add(x=200,name="Third"))
    build = AsyncMock(return_value=geometry_bridge.BuildOutcome(ok=failure == "stub", stub=failure == "stub", detail="fixture"))
    if failure == "cancelled": build.side_effect = asyncio.CancelledError()
    if failure == "stale": request = request.model_copy(update={"base_revision": 999})
    if failure == "busy": monkeypatch.setattr(session, "draft", object())
    monkeypatch.setattr(commands, "build_design", build)
    with pytest.raises((CadenzaError, asyncio.CancelledError)): asyncio.run(commands.handle_assembly_command(session, request, discard))
    assert session.snapshot() == before and not session.history and session.preview is None
    if failure in ("stale", "busy"): build.assert_not_called()


@pytest.mark.parametrize("variant", [0, 7])
def test_real_six_instances_source_edit_history_export_reopen(tmp_path, variant):
    from build123d import import_step
    from cadenza_geometry.service import Build123dGeometryService
    from cadenza_backend.design_commands import handle_design_command
    from cadenza_contracts.design_commands import DesignCommand, SetDimension
    from cadenza_backend.projects import ProjectRepository
    from cadenza_contracts.project import ProjectDocument
    session = fixture(variant); before = session.snapshot()
    asyncio.run(commands.handle_assembly_command(session, initial(session, 6), discard))
    assert session.preview is not None
    assert session.snapshot() == before and session.preview.message.display_ledger_id is None
    expected = 2 * sum((10 + variant + i) * 8 * 4 for i in range(3))
    assert session.preview.outcome.volume_mm3 == pytest.approx(expected)
    decide(session, "cancel"); assert session.snapshot() == before
    asyncio.run(commands.handle_assembly_command(session, initial(session, 6), discard)); decide(session)
    assert session.store.ledger.assembly is not None
    components = session.store.ledger.assembly.components; a, b = components[:2]
    accepted = session.snapshot(); accepted_step = session.artifacts.get(session.revision, "step")
    with pytest.raises(CadenzaError):
        asyncio.run(commands.handle_assembly_command(session, command(session,
            dict(op="set_instance_placement", component_id=b.id, placement=placement())), discard))
    assert session.snapshot() == accepted and session.artifacts.get(session.revision, "step") == accepted_step and session.preview is None
    angles = [17, 31, 43]
    asyncio.run(commands.handle_assembly_command(session, command(session,
        dict(op="set_instance_placement", component_id=a.id, placement=placement(-100, angles))), discard))
    decide(session)
    # Independent intrinsic XYZ rotation: Rx * Ry * Rz, applied to box corners.
    import itertools
    x, y, z = [math.radians(v) for v in angles]
    cx,sx,cy,sy,cz,sz = math.cos(x),math.sin(x),math.cos(y),math.sin(y),math.cos(z),math.sin(z)
    points=[]
    for px,py,pz in itertools.product([-(10+variant)/2,(10+variant)/2],[-4,4],[0,4]):
        qx,qy = cz*px-sz*py, sz*px+cz*py
        rx,rz = cy*qx+sy*pz, -sy*qx+cy*pz
        points.append([rx-100, cx*qy-sx*rz, sx*qy+cx*rz])
    service = Build123dGeometryService()
    bounds = service.run_probe_query(session.store.ledger, dict(query_type="get_bounding_box",component_1_id=a.id),session.snapshot())["final_component"]
    assert bounds["min"] == pytest.approx([min(p[i] for p in points) for i in range(3)])
    assert bounds["max"] == pytest.approx([max(p[i] for p in points) for i in range(3)])
    request = DesignCommand(message_id="source", base_revision=session.revision, ledger_id="source_0",
        operations=[SetDimension(feature_id="feat_stock", parameter="height", value=6)])
    asyncio.run(handle_design_command(session, request, discard))
    assert session.preview is not None
    from cadenza_backend.edit_preview import pending
    preview = pending(session); assert preview is not None
    assert any("affects 2 instance(s)" in check for check in preview.message.checks)
    assert preview.outcome.volume_mm3 == pytest.approx(expected + 2*(10+variant)*8*2)
    decide(session)
    artifact = session.artifacts.get(session.revision, "step"); assert artifact
    path = tmp_path / "assembly.step"; path.write_bytes(artifact.data)
    imported = import_step(str(path))
    assert len(imported.solids()) == 6 and imported.volume == pytest.approx(expected + 2*(10+variant)*8*2)
    undo = session.stage_undo(); assert undo; session.commit(undo[0], undo_index=undo[1])
    assert session.stores["source_0"].ledger.features[0].model_dump()["parameters"]["height"] == 4
    redo = session.stage_redo(); assert redo; session.commit(redo, redo=True)
    assert session.stores["source_0"].ledger.features[0].model_dump()["parameters"]["height"] == 6
    doc = ProjectDocument(name="Assembly", primary_ledger_id=session.primary_ledger_id,
        ledgers=session.snapshot(),history=session.history,redo=session.redo)
    repo=ProjectRepository(tmp_path / "assembly.db"); pid,version=repo.save(doc,None,0)
    reopened,actual=ProjectRepository(repo.path).load(pid)
    assert reopened == doc and actual == version
    assert commands.source_uses(reopened.ledgers,reopened.primary_ledger_id,"source_0") == commands.source_uses(session.snapshot(),session.primary_ledger_id,"source_0")


def joint(a, b, point_a=None, point_b=None, normals=False) -> dict[str, Any]:
    return dict(op="add_rigid_joint", component_a=a.id, component_b=b.id,
        target_a=dict(point=point_a or [0, 0, 4], normal=[0, 0, 2] if normals else None),
        target_b=dict(point=point_b or [60, 0, 0], normal=[0, 0, -3] if normals else None))


def nested_request(session, **updates):
    operation = dict(op="create_subassembly", name="Module", instance_name="Module A", placement=placement(150),
        children=[dict(source_ledger_id="source_0", name="Plate", placement=placement()),
                  dict(source_ledger_id="source_1", name="Spacer", placement=placement(40))])
    operation.update(updates)
    return command(session, operation)


def test_e2_private_subassembly_owner_paths_and_repeat():
    session, _ = established(); before = session.snapshot()
    staged, patches = commands.prepare_command(session, nested_request(session))
    new_id = next(lid for lid in staged if lid not in before)
    assert session.snapshot() == before and len(patches) == 1
    definition = staged[new_id].assembly
    assert definition is not None
    session.commit(staged)
    owner = session.store.ledger; assert owner.assembly
    outer = owner.assembly.components[-1]
    inner = definition.components[0]
    raw = command(session, dict(op="rename_instance", component_id=inner.id, name="Shared plate")).model_dump(mode="json")
    raw.update(ledger_id=new_id, owner_path=[outer.id])
    changed, _ = commands.prepare_command(session, AssemblyCommand.model_validate(raw))
    changed_definition = changed[new_id].assembly
    assert changed_definition and changed_definition.components[0].name == "Shared plate"
    assert changed[session.primary_ledger_id].assembly == owner.assembly
    for path, lid in [([], new_id), ([outer.id], "source_0"), (["comp_fake"], new_id), ([outer.id, inner.id], "source_0")]:
        with pytest.raises(CadenzaError): commands.prepare_command(session, AssemblyCommand.model_validate({**raw, "ledger_id":lid,"owner_path":path}))
    repeated = compiled(session, command(session, dict(op="duplicate_instance", component_id=outer.id, name="Module B", placement=placement(300))))
    assert repeated.assembly and repeated.assembly.components[-1].sub_assembly_id == new_id
    assert len(commands.source_uses({**session.snapshot(),session.primary_ledger_id:repeated},session.primary_ledger_id,new_id)) == 2


@pytest.mark.parametrize("failure", ["self", "two_parents", "cycle", "new_target", "placed", "removed", "duplicate_remove", "unchanged"])
def test_e2_attachment_preflight_atomic(failure):
    session, (a,b) = established(); before = session.snapshot()
    ops = [joint(a,b)]
    if failure == "self": ops = [joint(a,a)]
    if failure == "two_parents": ops += [joint(a,b)]
    if failure == "cycle": ops += [joint(b,a)]
    if failure == "new_target": ops[0]["component_b"] = "comp_unknown"
    if failure == "placed": ops += [dict(op="set_instance_placement",component_id=b.id,placement=placement(100))]
    if failure == "removed": ops += [dict(op="remove_instance",component_id=b.id)]
    if failure in ("duplicate_remove", "unchanged"):
        owner = compiled(session, command(session,*ops)); session.stores[owner.project_id] = LedgerStore(owner)
        before = session.snapshot(); assert owner.assembly
        removal = dict(op="remove_joint",joint_id=owner.assembly.joints[0].id)
        ops = [removal, removal] if failure == "duplicate_remove" else [removal, joint(a,b)]
    with pytest.raises(CadenzaError): commands.prepare_command(session,command(session,*ops))
    assert session.snapshot() == before and session.preview is None


def test_e2_detach_reposition_and_replacement():
    session,(a,b)=established()
    owner=compiled(session,command(session,joint(a,b))); session.stores[owner.project_id]=LedgerStore(owner)
    assert owner.assembly
    remove=dict(op="remove_joint",joint_id=owner.assembly.joints[0].id)
    staged,_=commands.prepare_command(session,command(session,dict(op="set_instance_placement",component_id=b.id,placement=placement(120)),remove))
    candidate=staged[owner.project_id]; assert candidate.assembly
    assert candidate.assembly.joints == [] and candidate.assembly.components[1].placement.origin[0] == 120
    replacement=compiled(session,command(session,joint(a,b,point_a=[0,0,5]),remove))
    assert replacement.assembly and replacement.assembly.joints[0].id != owner.assembly.joints[0].id


def test_e2_subassembly_creation_limits_and_cycle_are_atomic():
    session,_=established(); before=session.snapshot()
    for changes in [dict(name=session.store.ledger.metadata.name),
        dict(children=[dict(source_ledger_id=session.primary_ledger_id,name="Cycle",placement=placement())]),
        dict(children=[dict(source_ledger_id="missing",name="Missing",placement=placement())])]:
        with pytest.raises(CadenzaError): commands.prepare_command(session,nested_request(session,**changes))
        assert session.snapshot()==before
    raw=nested_request(session).model_dump(mode="json")
    raw["operations"].append(add(2,200,"Extra"))
    with pytest.raises(CadenzaError): commands.prepare_command(session,AssemblyCommand.model_validate(raw))
    for i in range(8):
        source=Ledger.model_validate(dict(project_id=f"module_{i}",metadata=dict(name=f"Module {i}"),assembly=dict(components=[dict(id="comp_leaf",sub_assembly_id="source_0")])) )
        session.stores[source.project_id]=LedgerStore(source)
    with pytest.raises(CadenzaError): commands.prepare_command(session,nested_request(session))
    # Reuse remains available at the definition creation limit.
    assert compiled(session,command(session,dict(op="add_instance",source_ledger_id="module_0",name="Reuse",placement=placement(300)))).assembly


def test_e2_discovery_v1_shape_and_v2_scope():
    from cadenza_contracts.assembly_commands import AssemblyCapabilitiesRequest, OPERATIONS, E2_OPERATIONS
    session,_=established()
    legacy=commands.capabilities(session,"cap").model_dump(mode="json")
    assert legacy["operations"]==list(OPERATIONS)
    assert not {"owner_path","max_subassemblies","max_initial_children"}&legacy.keys()
    request=AssemblyCapabilitiesRequest(message_id="cap",version=2,ledger_id=session.primary_ledger_id)
    current=commands.capabilities(session,request)
    assert current.can_edit and current.operations==list(E2_OPERATIONS)
    assert commands.capabilities(session,request.model_copy(update={"owner_path":["comp_fake"]})).reason


def test_e2_real_joint_and_nested_cancel_apply_undo():
    session,(a,b)=established(); before=session.snapshot()
    asyncio.run(commands.handle_assembly_command(session,command(session,joint(a,b,normals=True)),discard))
    assert session.snapshot()==before and session.preview
    decide(session,"cancel"); assert session.snapshot()==before
    asyncio.run(commands.handle_assembly_command(session,command(session,joint(a,b,normals=True)),discard))
    decide(session)
    assert session.store.ledger.assembly and len(session.store.ledger.assembly.joints)==1
    before=session.snapshot()
    asyncio.run(commands.handle_assembly_command(session,nested_request(session),discard))
    assert session.preview and len(session.preview.ledgers)==len(before)+1
    decide(session,"cancel"); assert session.snapshot()==before
    asyncio.run(commands.handle_assembly_command(session,nested_request(session),discard))
    decide(session)
    assert len(session.stores)==len(before)+1


@pytest.mark.parametrize("variant", [0,1])
def test_e2_eight_leaves_rigid_chain_shared_edits_export_history(tmp_path, variant):
    from build123d import import_step
    from cadenza_geometry.service import Build123dGeometryService
    from cadenza_backend.design_commands import handle_design_command
    from cadenza_contracts.design_commands import DesignCommand, SetDimension
    from cadenza_backend.projects import ProjectRepository
    from cadenza_contracts.project import ProjectDocument
    session=fixture(variant)
    extra=Ledger.model_validate(dict(project_id="source_3",metadata=dict(name="Support"),
        features=[dict(id="feat_stock",kind="box",parameters=dict(length=16,width=10,height=6))]))
    session.stores[extra.project_id]=LedgerStore(extra)
    if variant:
        shaft=Ledger.model_validate(dict(project_id="source_1",metadata=dict(name="Shaft"),
            features=[dict(id="feat_stock",kind="cylinder",parameters=dict(diameter=6,height=4))]))
        session.stores[shaft.project_id]=LedgerStore(shaft)
    def apply(request):
        asyncio.run(commands.handle_assembly_command(session,request,discard)); decide(session)
    children=[dict(source_ledger_id=f"source_{i}",name=f"Child {i}",placement=placement(i*40,[0,0,i*90])) for i in range(3)]
    creation=nested_request(session,placement=placement(100),children=children).operations[0]
    apply(command(session,dict(op="initialize_assembly",name="Fixture"),creation.model_dump(mode="json")))
    assert session.store.ledger.assembly
    outer=session.store.ledger.assembly.components[0]; module_id=outer.sub_assembly_id; assert module_id
    module=session.stores[module_id].ledger.assembly; assert module
    a,b,c=module.components
    def scoped(*operations):
        return command(session,*operations).model_copy(update={"ledger_id":module_id,"owner_path":[outer.id]})
    ab=joint(a,b,[0,0,4],[40,0,0],normals=not variant)
    bc=joint(b,c,[40,0,4],[80,0,0],normals=not variant)
    apply(scoped(*([ab,bc] if not variant else [bc,ab])))
    apply(command(session,dict(op="duplicate_instance",component_id=outer.id,name="Module B",placement=placement(300,[0,0,90])),
        add(0,-100,"Loose plate"),dict(op="add_instance",source_ledger_id="source_3",name="Support",placement=placement(-200))))
    assert len([r for r in commands.assembly_instances(session.snapshot(),session.primary_ledger_id) if r.source_id and session.stores[r.source_id].ledger.assembly is None])==8
    service=Build123dGeometryService()
    def bounds(path):
        return service.run_probe_query(session.store.ledger,dict(query_type="get_bounding_box",component_1_id=path),session.snapshot())["final_component"]
    assert bounds(f"{outer.id}/{a.id}")["max"][2]==pytest.approx(4)
    assert bounds(f"{outer.id}/{b.id}")["min"][2]==pytest.approx(4)
    assert bounds(f"{outer.id}/{c.id}")["min"][2]==pytest.approx(8)
    assert session.store.ledger.assembly
    repeated=session.store.ledger.assembly.components[1]
    assert bounds(f"{repeated.id}/{c.id}")["min"][2]==pytest.approx(8)
    # Width change preserves planar contact while all three shared plate occurrences update.
    def source_edit(parameter,value):
        asyncio.run(handle_design_command(session,DesignCommand(message_id="source",base_revision=session.revision,
            ledger_id="source_0",operations=[SetDimension(feature_id="feat_stock",parameter=parameter,value=value)]),discard))
    source_edit("width",9); assert session.preview
    assert any("affects 3 instance(s)" in check for check in session.preview.message.checks)
    assert any("does not prove face contact" in check for check in session.preview.message.checks)
    decide(session)
    assert bounds(f"{outer.id}/{b.id}")["min"][2]-bounds(f"{outer.id}/{a.id}")["max"][2]==pytest.approx(0)
    source_edit("height",3); decide(session)
    assert bounds(f"{outer.id}/{b.id}")["min"][2]-bounds(f"{outer.id}/{a.id}")["max"][2]==pytest.approx(1)
    before=session.snapshot(); history=list(session.history); artifact=session.artifacts.get(session.revision,"step")
    with pytest.raises(CadenzaError): source_edit("height",5)
    assert session.snapshot()==before and session.history==history and session.artifacts.get(session.revision,"step")==artifact
    # Shared child placement changes both occurrences; independent outer placement only one.
    current=session.stores[module_id].ledger.assembly; assert current
    link=next(j for j in current.joints if j.component_b==c.id)
    apply(scoped(dict(op="remove_joint",joint_id=link.id),dict(op="set_instance_placement",component_id=c.id,placement=placement(80))))
    assert bounds(f"{outer.id}/{c.id}")["min"][2]==pytest.approx(0)
    assert bounds(f"{repeated.id}/{c.id}")["min"][2]==pytest.approx(0)
    unaffected=bounds(f"{outer.id}/{a.id}")
    apply(command(session,dict(op="set_instance_placement",component_id=repeated.id,placement=placement(400,[0,0,90]))))
    assert bounds(f"{outer.id}/{a.id}")==unaffected
    # Independent box/cylinder volumes and exported solid count.
    plate_volume=(10+variant)*9*3
    middle_volume=math.pi*3**2*4 if variant else 11*8*4
    expected=3*plate_volume+2*middle_volume+2*(12+variant)*8*4+16*10*6
    artifact=session.artifacts.get(session.revision,"step"); assert artifact
    path=tmp_path / "nested.step";path.write_bytes(artifact.data); shape=import_step(str(path))
    assert len(shape.solids())==8 and shape.volume==pytest.approx(expected)
    assert shape.bounding_box().min.X==pytest.approx(-208)
    assert shape.bounding_box().max.Y==pytest.approx(80+(12+variant)/2)
    # Geometry dimensions are also checked after history restoration and persistence.
    undo=session.stage_undo();assert undo;session.commit(undo[0],undo_index=undo[1])
    redo=session.stage_redo();assert redo;session.commit(redo,redo=True)
    doc=ProjectDocument(name="E2",primary_ledger_id=session.primary_ledger_id,ledgers=session.snapshot(),history=session.history,redo=session.redo)
    repo=ProjectRepository(tmp_path / "nested.db");pid,version=repo.save(doc,None,0)
    reopened,actual=ProjectRepository(repo.path).load(pid)
    assert reopened==doc and actual==version


@pytest.mark.parametrize("failure", ["stub","timeout","cancel","stale","expired"])
def test_e2_new_definition_failures_leave_no_orphan(monkeypatch,failure):
    from cadenza_backend.edit_preview import pending
    from cadenza_backend.contracts_bridge import ErrorCode
    session,_=established();before=session.snapshot(); request=nested_request(session)
    if failure=="expired":
        asyncio.run(commands.handle_assembly_command(session,request,discard)); assert session.preview
        session.preview.created-=901
        assert pending(session) is None
    else:
        build=AsyncMock(return_value=geometry_bridge.BuildOutcome(ok=False,stub=False,detail="test failure",
            code=ErrorCode.GEOMETRY_TIMEOUT if failure == "timeout" else ErrorCode.GEOMETRY_KERNEL_FAILURE))
        if failure=="stub":build.return_value=geometry_bridge.BuildOutcome(ok=True,stub=True,detail="stub")
        if failure=="cancel":build.side_effect=asyncio.CancelledError()
        if failure=="stale":request=request.model_copy(update={"base_revision":999})
        with monkeypatch.context() as patcher:
            patcher.setattr(commands,"build_design",build)
            with pytest.raises((CadenzaError,asyncio.CancelledError)):asyncio.run(commands.handle_assembly_command(session,request,discard))
    assert session.snapshot()==before and session.preview is None and not session.history and not session.redo
    asyncio.run(commands.handle_assembly_command(session,nested_request(session),discard)); decide(session)
    assert len(session.stores)==len(before)+1


def test_e2_scaled_normals_are_not_a_changed_attachment():
    session,(a,b)=established()
    owner=compiled(session,command(session,joint(a,b,normals=True))); session.stores[owner.project_id]=LedgerStore(owner)
    assert owner.assembly
    replacement=joint(a,b,normals=True)
    replacement["target_a"]["normal"]=[0,0,20]
    replacement["target_b"]["normal"]=[0,0,-30]
    with pytest.raises(CadenzaError,match="has not changed"):
        commands.prepare_command(session,command(session,dict(op="remove_joint",joint_id=owner.assembly.joints[0].id),replacement))
    # Same stored coordinates are a valid explicit detach-and-place, not a no-op.
    staged,_=commands.prepare_command(session,command(session,dict(op="remove_joint",joint_id=owner.assembly.joints[0].id),
        dict(op="set_instance_placement",component_id=b.id,placement=placement(60))))
    assembly=staged[owner.project_id].assembly
    assert assembly and not assembly.joints


def test_e2_detach_subtree_retains_downstream_links():
    from cadenza_geometry.service import Build123dGeometryService
    session=fixture();owner=compiled(session,initial(session,3));session.stores[owner.project_id]=LedgerStore(owner)
    assert owner.assembly
    a,b,c=owner.assembly.components
    owner=compiled(session,command(session,joint(a,b,[0,0,4],[60,0,0]),joint(b,c,[60,0,4],[120,0,0])))
    session.stores[owner.project_id]=LedgerStore(owner); assert owner.assembly
    staged,_=commands.prepare_command(session,command(session,dict(op="remove_joint",joint_id=owner.assembly.joints[0].id)))
    candidate=staged[owner.project_id]; assert candidate.assembly
    assert len(candidate.assembly.joints)==1 and candidate.assembly.joints[0].id==owner.assembly.joints[1].id
    outcome=asyncio.run(commands.build_design(staged,owner.project_id));assert outcome.ok and not outcome.stub
    service=Build123dGeometryService()
    result=service.run_probe_query(candidate,dict(query_type="get_bounding_box",component_1_id=c.id),staged)["final_component"]
    assert result["min"]==pytest.approx([54,-4,4]) and result["max"]==pytest.approx([66,4,8])


def test_e2_overlapping_private_children_do_not_leave_definition():
    session,_=established(); before=session.snapshot()
    children=[dict(source_ledger_id=f"source_{i}",name=f"Child {i}",placement=placement()) for i in (0,1)]
    with pytest.raises(CadenzaError):
        asyncio.run(commands.handle_assembly_command(session,nested_request(session,children=children),discard))
    assert session.snapshot()==before and not session.history and session.preview is None
    asyncio.run(commands.handle_assembly_command(session,nested_request(session),discard));decide(session)
    assert len(session.stores)==len(before)+1
