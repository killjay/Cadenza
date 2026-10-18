"""Emit actual Python wire output for TypeScript compiler conformance tests."""
import json
from pathlib import Path

from cadenza_contracts.messages import parse_client_message, parse_server_message
from cadenza_contracts.fixtures import plate_with_hole
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.surface_reference import SurfaceReference

fixtures = json.loads(Path(__file__).with_name("transport.json").read_text(encoding="utf-8"))
fixtures["client"].append({"type": "session.redo", "message_id": "msg_redo", "base_revision": 4})
plate = plate_with_hole()
plate.surface_references["ref_plate_top"] = SurfaceReference.model_validate_json(
    Path(__file__).with_name("surface_reference.json").read_text(encoding="utf-8"))
fixtures["client"].append({"type": "user.prompt", "message_id": "msg_surface", "base_revision": 0,
    "prompt": "Edit the saved top", "targets": [{"point": [0, 0, 20], "saved_reference": {
        "ledger_id": plate.project_id, "reference_id": "ref_plate_top"}}]})
assembly = Ledger.model_validate({"assembly": {"components": [
    {"id": "comp_plate", "sub_assembly_id": plate.project_id}]}})
fixtures["client"].extend([
    {"type": "user.prompt", "message_id": "msg_profile_identity", "base_revision": 1,
     "prompt": "Enlarge this opening", "targets": [{"point": [10, 0, 4], "surface_reference": {
         "version": 1, "ledger_id": "prj_sketch", "source_ledger_id": "prj_sketch", "source_part_id": None,
         "component_path": [], "feature_id": "feat_extrusion", "feature_kind": "extrude", "operation": "add",
         "profile_id": "feat_profile", "entity_id": "ent_bore", "role": "bore"}}]},
    {"type": "design.sketch_dimension", "message_id": "msg_sketch", "base_revision": 1,
     "ledger_id": "prj_sketch", "feature_id": "feat_profile", "constraint_id": "con_width", "value": 140},
    {"type": "design.named_parameter", "message_id": "msg_named", "base_revision": 1,
     "ledger_id": "prj_parameters", "parameter": "plate_width", "value": 140},
    {"type": "design.parameter", "message_id": "msg_numeric", "base_revision": 1,
     "ledger_id": plate.project_id, "feature_id": plate.features[0].id, "parameter": "height", "value": 40},
    {"type": "design.preview_decision", "message_id": "msg_apply", "base_revision": 1,
     "preview_id": "preview_fixture", "action": "apply"},
    {"type": "design.command", "message_id": "msg_cmd", "base_revision": 1,
     "ledger_id": plate.project_id, "scope": "source", "operations": [
         {"op": "add_feature", "kind": "hole", "name": "New Hole", "operation": "subtract",
          "placement": {"origin": [0, 0, 0]},
          "parameters": {"diameter": 10, "depth": 20, "through": False}},
         {"op": "set_dimension", "feature_id": plate.features[0].id, "parameter": "length", "value": 80},
         {"op": "set_origin", "feature_id": plate.features[0].id, "axis": "x", "value": 10},
         {"op": "set_suppressed", "feature_id": plate.features[0].id, "suppressed": True},
         {"op": "remove_feature", "feature_id": plate.features[0].id}
     ]},
])
fixtures["server"].extend([
    {"type": "design.preview", "message_id": "msg_numeric", "preview_id": "preview_fixture", "base_revision": 1,
     "summary": "Change height", "changes": [{"ledger_id": plate.project_id,
        "path": "/features/feat_main_body_0001/parameters/height", "before": 20, "after": 40}], "checks": ["Built successfully"]},
    {"type": "design.preview_closed", "message_id": "msg_apply", "preview_id": "preview_fixture"},
])
fixtures["server"][0]["store"] = {
    plate.project_id: plate.model_dump(mode="json"),
    assembly.project_id: assembly.model_dump(mode="json"),
    "prj_parameters": Ledger.model_validate_json(Path(__file__).with_name("parameter_plate.json").read_text(encoding="utf-8")).model_dump(mode="json"),
    "prj_sketch": Ledger.model_validate_json(Path(__file__).with_name("profile_plate.json").read_text(encoding="utf-8")).model_dump(mode="json"),
}
draft_request = {"message_id": "msg_draft", "base_revision": 0, "draft_id": "draft_fixture", "draft_revision": 0}
fixtures["client"].extend([
    {"type": "sketch.draft_start", "message_id": "msg_start", "base_revision": 0},
    {"type": "sketch.draft_update", **draft_request, "operations": [
        {"op": "add_entity", "entity": {"kind": "rectangle", "width": 100, "height": 60}},
        {"op": "add_entity", "entity": {"kind": "circle", "operation": "subtract", "diameter": 20}},
        {"op": "remove_entity", "entity_id": "ent_hole"},
        {"op": "set_seed", "entity_id": "ent_plate", "parameter": "x", "value": 0},
        {"op": "add_dimension", "entity_id": "ent_plate", "parameter": "width", "value": 100},
        {"op": "add_centre", "entity_a": "ent_plate", "entity_b": "ent_hole"},
        {"op": "remove_constraint", "constraint_id": "con_width"},
        {"op": "set_dimension", "constraint_id": "con_width", "value": 120},
        {"op": "set_driver", "parameter": "plate_width", "value": 120},
        {"op": "set_extrusion", "amount": 8},
        {"op": "add_entity", "alias": "opening", "entity": {"kind": "circle", "operation": "subtract", "diameter": 6}},
        {"op": "add_dimension", "alias": "diameter", "entity_id": {"entity_alias": "opening"}, "parameter": "diameter", "value": 6},
        {"op": "add_driver", "parameter": "opening_diameter", "value": 6},
        {"op": "bind_dimension", "constraint_id": {"constraint_alias": "diameter"}, "expression": "opening_diameter"},
    ]},
    *[{"type": "sketch.draft_action", **draft_request, "action": action} for action in ("finish", "discard")],
])
fixtures["server"].extend([
    {"type": "sketch.draft_state", "message_id": "msg_start", "session_id": "session_fixture", "draft": {
        "id": "draft_fixture", "base_revision": 0, "revision": 0, "ledger_id": "prj_sketch",
        "profile_id": "feat_draft", "extrusion_id": "feat_extrusion", "is_new": True, "definition": {}, "relationships": {}}},
    {"type": "sketch.draft_closed", "message_id": "msg_draft", "session_id": "session_fixture", "draft_id": "draft_fixture"},
    {"type": "design.preview", "message_id": "msg_draft", "preview_id": "preview_draft", "base_revision": 0,
        "draft_id": "draft_fixture", "draft_revision": 3, "summary": "Draft review", "changes": [], "checks": []},
])
fixtures["client"].extend([
    {"type": "sketch.prompt", **draft_request, "prompt": "Make the selected opening 8 mm", "selected_entity_id": "ent_bore"},
    {"type": "sketch.prompt", **draft_request, "prompt": "8 mm", "reply_to": "proposal_fixture"},
    {"type": "sketch.proposal_decision", **draft_request, "proposal_id": "proposal_fixture", "action": "update"},
])
fixtures["server"].append({"type": "sketch.proposal", **draft_request, "session_id": "session_fixture",
    "proposal_id": "proposal_fixture", "summary": "Resize selected opening", "selected_entity_id": "ent_bore",
    "interpretation": ["Set Opening 1 diameter to 8 mm"], "changes": [{"label": "Opening 1 diameter (mm)", "before": "6", "after": "8"}],
    "checks": ["Dimensions checked"], "assumptions": []})
from cadenza_contracts.feature_capabilities import CapabilitiesResponse, feature_capabilities
fixtures["client"].append({"type": "design.capabilities", "message_id": "msg_capabilities"})
fixtures["server"].append(CapabilitiesResponse(message_id="msg_capabilities", session_id="session_fixture",
    features=feature_capabilities()).model_dump(mode="json"))
fixtures["client"].extend([
    {"type": "design.prompt.capabilities", "message_id": "msg_prompt_cap"},
    {"type": "design.prompt", "message_id": "msg_target_prompt", "base_revision": 1,
     "ledger_id": plate.project_id, "feature_id": plate.features[0].id, "scope": "source", "prompt": "Set height to 15 mm"},
])
fixtures["server"].append({"type": "design.prompt.capabilities", "message_id": "msg_prompt_cap",
    "session_id": "session_fixture", "version": 1, "scope": "source", "max_operations": 32})
from cadenza_contracts.feature_capabilities import PatternCapabilitiesResponse
fixtures["client"].append({"type": "design.pattern.capabilities", "message_id": "msg_pattern_cap"})
fixtures["server"].append(PatternCapabilitiesResponse(message_id="msg_pattern_cap", session_id="session_fixture").model_dump(mode="json"))
fixtures["client"].append({"type": "design.command", "message_id": "msg_patterns", "base_revision": 1,
    "ledger_id": plate.project_id, "operations": [
        dict(op="add_linear_pattern", name="Row", target_feature="feat_seed", count=4, spacing=10, direction="+x"),
        dict(op="add_circular_pattern", name="Circle", target_feature="feat_seed", count=4, center_xy=[0, 0]),
        dict(op="update_linear_pattern", feature_id="feat_pattern", count=3, spacing=15, direction="-y"),
        dict(op="update_circular_pattern", feature_id="feat_pattern", count=8, center_xy=[5, -5]),
    ]})
from cadenza_contracts.part_commands import PartCapabilitiesResponse
fixtures["client"].extend([
    dict(type="design.part.capabilities", message_id="msg_part_cap"),
    dict(type="design.part.create", message_id="msg_part_create", base_revision=1, name="Spacer", operations=[
        dict(op="add_feature", kind="cylinder", name="Stock", operation="add", placement={}, parameters=dict(diameter=12, height=8))]),
    dict(type="design.part.prompt", message_id="msg_part_prompt", base_revision=1, name="Spacer", prompt="Create a spacer"),
    dict(type="design.part.rename", message_id="msg_rename", base_revision=1, ledger_id=plate.project_id, name="Renamed Plate"),
    dict(type="design.part.view", message_id="msg_part_view", base_revision=1, ledger_id=plate.project_id),
    dict(type="design.part.view", message_id="msg_context_view", base_revision=1, ledger_id=plate.project_id, component_path=["comp_plate"]),
])
fixtures["server"].extend([
    PartCapabilitiesResponse(message_id="msg_part_cap", session_id="session_fixture").model_dump(mode="json"),
    dict(type="design.part.view_ready", message_id="msg_part_view", session_id="session_fixture", revision=1,
        ledger_id=plate.project_id, view_id="view_fixture", glb_url="/model.glb", step_url="/model.step", empty=False),
    dict(type="design.part.view_ready", message_id="msg_context_view", session_id="session_fixture", revision=1,
        ledger_id=plate.project_id, view_id="context_fixture", glb_url="/model.glb", step_url="/model.step", empty=False,
        component_path=["comp_plate"], context_glb_url="/context.glb", source_to_project=[1,0,0,20,0,1,0,0,0,0,1,0,0,0,0,1]),
])
from cadenza_contracts.assembly_commands import AssemblyCapabilitiesResponse, E2_OPERATIONS
fixtures["client"].extend([
    dict(type="design.assembly.capabilities", message_id="msg_assembly_cap"),
    dict(type="design.assembly.command", message_id="msg_assembly", base_revision=1, ledger_id=plate.project_id,
        scope="assembly_definition", operations=[dict(op="initialize_assembly", name="Fixture"),
        dict(op="add_instance", source_ledger_id="source", name="Plate", placement=dict(origin=[0,0,0], rotation_deg=[0,0,0])),
        dict(op="duplicate_instance", component_id="comp_plate", name="Plate 2", placement=dict(origin=[50,0,0], rotation_deg=[0,0,0])),
        dict(op="set_instance_placement", component_id="comp_plate", placement=dict(origin=[100,0,0], rotation_deg=[10,20,30])),
        dict(op="rename_instance", component_id="comp_plate", name="Renamed"),
        dict(op="remove_instance", component_id="comp_plate")]),
])
fixtures["server"].append(AssemblyCapabilitiesResponse(message_id="msg_assembly_cap", session_id="session_fixture",
    revision=1, ledger_id=plate.project_id, can_initialize=True, can_edit=False, reason=None).model_dump(mode="json"))
fixtures["client"].extend([
    dict(type="design.assembly.capabilities", message_id="msg_e2_cap", version=2, ledger_id=plate.project_id, owner_path=["comp_outer"]),
    dict(type="design.assembly.command", message_id="msg_e2", base_revision=1, ledger_id=plate.project_id,
        scope="assembly_definition", owner_path=["comp_outer"], operations=[
            dict(op="add_rigid_joint", component_a="comp_a", component_b="comp_b", target_a=dict(point=[0,0,4],normal=[0,0,1]), target_b=dict(point=[30,0,0],normal=[0,0,-1])),
            dict(op="remove_joint",joint_id="joint_old"),
            dict(op="create_subassembly",name="Module",instance_name="Module A",placement=dict(origin=[0,0,0],rotation_deg=[0,0,0]),
                children=[dict(source_ledger_id="source",name="Child",placement=dict(origin=[0,0,0],rotation_deg=[0,0,0]))])]),
])
fixtures["server"].append(AssemblyCapabilitiesResponse(message_id="msg_e2_cap",session_id="session_fixture",revision=1,
    ledger_id=plate.project_id,version=2,owner_path=["comp_outer"],can_initialize=False,can_edit=True,reason=None,
    operations=list(E2_OPERATIONS)).model_dump(mode="json"))
from cadenza_contracts.assembly_commands import AssemblyPromptCapabilitiesResponse
fixtures["client"].append(dict(type="design.assembly.prompt.capabilities", message_id="e3cap_fixture",
    base_revision=1, ledger_id=plate.project_id, owner_path=[]))
for target in [dict(kind="definition"), dict(kind="instance", component_id="comp_a", detach_joint_ids=["joint_old"]),
               dict(kind="attachment", component_a="comp_a", component_b="comp_b", joint_id="joint_old")]:
    fixtures["client"].append(dict(type="design.assembly.prompt", message_id="e3_fixture", base_revision=1,
        ledger_id=plate.project_id, owner_path=[], scope="assembly_definition", target=target, prompt="Edit this assembly"))
fixtures["server"].append(AssemblyPromptCapabilitiesResponse(message_id="e3cap_fixture", session_id="session_fixture",
    revision=1, ledger_id=plate.project_id, owner_path=[], available=True, vision_available=False).model_dump(mode="json"))
from cadenza_contracts.assembly_commands import PLANAR_OPERATIONS
face_ref = dict(ledger_id="prj_face", source_ledger_id="prj_face", feature_id="feat_box", feature_kind="box", operation="add", role="top_face")
pick = dict(reference=face_ref, point=[0, 0, 10], normal=[0, 0, 1])
fixtures["client"].extend([
    dict(type="design.assembly.capabilities", message_id="face_cap", version=3, ledger_id="prj_owner", owner_path=[]),
    dict(type="design.assembly.command", message_id="face_command", base_revision=0, ledger_id="prj_owner",
        owner_path=[], scope="assembly_definition", operations=[dict(op="add_planar_attachment",
            component_a="comp_a", component_b="comp_b", target_a=pick, target_b=pick)])])
fixtures["server"].append(AssemblyCapabilitiesResponse(message_id="face_cap", session_id="session_fixture", revision=0,
    ledger_id="prj_owner", version=3, owner_path=[], can_initialize=False, can_edit=True, reason=None,
    operations=list(PLANAR_OPERATIONS)).model_dump(mode="json"))
fixtures["server"].append(dict(type="design.preview", message_id="face_command", preview_id="preview_faces", base_revision=0,
    summary="Selected face contact verified", changes=[], checks=[], attachment_contacts=[dict(definition_id="prj_owner",
        joint_id="joint_faces", separation_mm=0, normal_residual=0, area_mm2=20, references_resolved=True, contact_verified=True)]))
# Exercise the persisted joint union in a real serialized server ledger.
face_owner = Ledger.model_validate(dict(project_id="prj_owner", assembly=dict(components=[
    dict(id="comp_a", sub_assembly_id="prj_face"), dict(id="comp_b", sub_assembly_id="prj_face")], joints=[
    dict(id="joint_faces", kind="rigid", component_a="comp_a", component_b="comp_b", twist_deg=0,
        target_a=dict(mode="planar_face", reference=face_ref, uv=[0, 0]),
        target_b=dict(mode="planar_face", reference=face_ref, uv=[0, 0]))])))
fixtures["server"].append(dict(type="ledger.updated", message_id="face_ledger", revision=0, can_undo=False, store={face_owner.project_id:face_owner.model_dump(mode="json")}))
print(json.dumps({
    "client": [parse_client_message(frame).model_dump(mode="json") for frame in fixtures["client"]],
    "server": [parse_server_message(frame).model_dump(mode="json") for frame in fixtures["server"]],
}))
