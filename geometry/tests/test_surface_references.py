import pytest

from cadenza_contracts.fixtures import HOLE_ID, PLATE_ID, plate_with_hole
from cadenza_contracts.ledger import Ledger
from cadenza_geometry.service import Build123dGeometryService


def test_primitive_export_references_match_native_capture():
    import json
    import struct
    service = Build123dGeometryService(); plate = plate_with_hole()
    result = service.build(plate)
    size = struct.unpack_from("<I", result.glb, 12)[0]
    nodes = json.loads(result.glb[20:20 + size])["nodes"]
    references = [n.get("extras", {}).get("surface_reference") for n in nodes]
    for point, normal in [([20, 20, 20], [0, 0, 1]), ([5, 0, 10], [-1, 0, 0])]:
        assert capture(service, plate, point, normal)["reference"] in references


def capture(service, ledger, point, normal=None, documents=None):
    return service.run_probe_query(ledger, {"query_type": "capture_surface_reference", "point": point, "normal": normal}, documents)


def resolve(service, ledger, reference, documents=None):
    return service.run_probe_query(ledger, {"query_type": "resolve_surface_reference", "reference": reference}, documents)


def test_top_and_bore_roles_follow_dimension_edits():
    service = Build123dGeometryService()
    plate = plate_with_hole()
    top = capture(service, plate, [20, 20, 20], [0, 0, 1])
    bore = capture(service, plate, [5, 0, 10], [-1, 0, 0])
    assert top["status"] == bore["status"] == "resolved"
    assert top["reference"]["role"] == "top_face" and bore["reference"]["role"] == "bore"
    assert top["reference"]["feature_id"] == PLATE_ID and bore["reference"]["feature_id"] == HOLE_ID
    changed = plate_with_hole(height=40, hole_diameter=16)
    assert resolve(service, changed, top["reference"])["face"]["center"][2] == pytest.approx(40)
    wall = resolve(service, changed, bore["reference"])
    assert wall["status"] == "resolved"
    assert wall["face"]["area_mm2"] == pytest.approx(2 * 3.141592653589793 * 8 * 40)
    assert set(top["reference"]) == {"version", "ledger_id", "source_ledger_id", "source_part_id",
                                     "component_path", "feature_id", "feature_kind", "operation", "role", "profile_id", "entity_id"}
    assert top["reference"]["profile_id"] is None and top["reference"]["entity_id"] is None


@pytest.mark.parametrize("edit", ["delete", "suppress", "kind", "consume", "split"])
def test_changed_topology_is_explicitly_unresolved(edit):
    service = Build123dGeometryService()
    plate = plate_with_hole()
    reference = capture(service, plate, [20, 20, 20])["reference"]
    doc = plate.model_dump(mode="json")
    if edit in {"delete", "suppress"}:
        doc["features"] = [] if edit == "delete" else [dict(f, suppressed=True) for f in doc["features"]]
    elif edit == "kind":
        doc["features"][0] = {"id": PLATE_ID, "kind": "cylinder", "parameters": {"diameter": 100, "height": 20}}
    else:
        doc["features"].append({"id": "feat_cut", "kind": "box", "operation": "subtract",
            "parameters": {"length": 120 if edit == "consume" else 10, "width": 120, "height": 10},
            "placement": {"origin": [0, 0, 15]}})
    result = resolve(service, Ledger.model_validate(doc), reference)
    assert result["status"] == ("ambiguous" if edit == "split" else "unsupported" if edit == "kind" else "missing")
    assert "face" not in result
    if edit == "split":
        fresh = capture(service, Ledger.model_validate(doc), [20, 20, 20])
        assert fresh["status"] == "ambiguous" and "reference" not in fresh


def test_capture_rejects_remote_edge_and_competing_feature_hits():
    service = Build123dGeometryService()
    plate = plate_with_hole()
    assert capture(service, plate, [20, 20, 200])["status"] == "missing"
    assert capture(service, plate, [50, 50, 20])["status"] == "ambiguous"
    assert capture(service, plate, [20, 20, 20], [0, 0, -1])["status"] == "missing"
    doc = plate.model_dump(mode="json")
    doc["features"].append({"id": "feat_duplicate", "kind": "box", "parameters": {"length": 100, "width": 100, "height": 20}})
    assert capture(service, Ledger.model_validate(doc), [20, 20, 20])["status"] == "ambiguous"


def test_nested_instance_reference_follows_placement_but_not_source_replacement():
    service = Build123dGeometryService()
    part = plate_with_hole()
    inner = Ledger.model_validate({"assembly": {"components": [{"id": "comp_inner", "sub_assembly_id": part.project_id,
        "placement": {"origin": [10, 0, 0], "rotation_deg": [0, 90, 0]}}]}})
    root = Ledger.model_validate({"assembly": {"components": [
        {"id": "comp_left", "sub_assembly_id": inner.project_id, "placement": {"origin": [200, 0, 0]}},
        {"id": "comp_right", "sub_assembly_id": inner.project_id, "placement": {"origin": [-200, 0, 0]}}]}})
    docs = {doc.project_id: doc for doc in (root, inner, part)}
    result = capture(service, root, [230, 20, -20], [1, 0, 0], docs)
    assert result["status"] == "resolved"
    reference = result["reference"]
    assert reference["component_path"] == ["comp_left", "comp_inner"]
    changed = root.model_dump(mode="json")
    changed["assembly"]["components"][0]["placement"]["origin"] = [300, 0, 0]
    root = Ledger.model_validate(changed)
    docs[root.project_id] = root
    result = resolve(service, root, reference, docs)
    assert result["status"] == "resolved" and result["face"]["center"] == pytest.approx([330, 0, 0])
    replacement = Ledger.model_validate({**part.model_dump(mode="json"), "project_id": "prj_replacement"})
    docs[replacement.project_id] = replacement
    inner_doc = inner.model_dump(mode="json")
    inner_doc["assembly"]["components"][0]["sub_assembly_id"] = replacement.project_id
    docs[inner.project_id] = Ledger.model_validate(inner_doc)
    assert resolve(service, root, reference, docs)["status"] == "missing"


def test_cylinder_role_and_drafted_primitive_limits():
    service = Build123dGeometryService()
    cylinder = Ledger.model_validate({"features": [{"id": "feat_cylinder", "kind": "cylinder",
        "parameters": {"diameter": 20, "height": 30}}]})
    result = capture(service, cylinder, [10, 0, 15], [1, 0, 0])
    assert result["status"] == "resolved" and result["reference"]["role"] == "lateral_face"
    doc = cylinder.model_dump(mode="json")
    doc["features"][0]["parameters"]["draft_angle"] = 2
    assert resolve(service, Ledger.model_validate(doc), result["reference"])["status"] == "unsupported"


def test_local_part_identity_and_removed_instances_do_not_retarget():
    service = Build123dGeometryService()
    feature = {"id": "feat_box", "kind": "box", "parameters": {"length": 10, "width": 10, "height": 10}}
    root = Ledger.model_validate({"assembly": {
        "parts": [{"id": "part_a", "features": [feature]}, {"id": "part_b", "features": [feature]}],
        "components": [{"id": "comp_a", "part_id": "part_a"},
                       {"id": "comp_b", "part_id": "part_b", "placement": {"origin": [30, 0, 0]}}]}})
    reference = capture(service, root, [0, 0, 10])["reference"]
    assert reference["source_part_id"] == "part_a" and reference["component_path"] == ["comp_a"]
    for change in ("source", "remove", "empty"):
        doc = root.model_dump(mode="json")
        if change == "source":
            doc["assembly"]["components"][0]["part_id"] = "part_b"
        else:
            doc["assembly"]["components"] = doc["assembly"]["components"][1:] if change == "remove" else []
        assert resolve(service, Ledger.model_validate(doc), reference)["status"] == "missing"
