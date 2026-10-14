"""Identity must follow design intent, not face ordinals or old coordinates."""
import copy
import json
import struct
from pathlib import Path

import pytest

from cadenza_contracts.ledger import Ledger
from cadenza_geometry.service import Build123dGeometryService
from cadenza_geometry.glb import MeshGroup, tessellate_face


@pytest.fixture
def raw():
    return json.loads((Path(__file__).parents[2] / "shared/contract_fixtures/profile_plate.json").read_text())


def capture(service, raw, point, normal=None, docs=None):
    return service.run_probe_query(raw, {"query_type": "capture_surface_reference", "point": point, "normal": normal}, docs)


def resolve(service, raw, ref, docs=None):
    return service.run_probe_query(raw, {"query_type": "resolve_surface_reference", "reference": ref}, docs)


def glb_nodes(result):
    size = struct.unpack_from("<I", result.glb, 12)[0]
    return json.loads(result.glb[20:20 + size])["nodes"]


def test_all_seven_faces_and_exported_identity(raw):
    service = Build123dGeometryService()
    built = service._compile(raw)
    assert len(built.faces) == len(built.attribution) == 7 and not built.ambiguity
    nodes = glb_nodes(service.build(raw))
    refs = [n["extras"]["surface_reference"] for n in nodes]
    assert len(refs) == 7 and all(r["feature_id"] == "feat_extrusion" and r["profile_id"] == "feat_profile" for r in refs)
    bore = capture(service, raw, [10, 0, 4], [-1, 0, 0])
    assert bore["status"] == "resolved" and bore["reference"]["entity_id"] == "ent_bore"
    assert next(r for r in refs if r["role"] == "bore") == bore["reference"]
    assert all(resolve(service, raw, r)["status"] == "resolved" for r in refs)


@pytest.mark.parametrize("rotated", [False, True])
def test_render_triangles_follow_outward_face_normals(raw, rotated):
    if rotated:
        raw["features"][0]["placement"] = {"origin": [10, 20, 30], "rotation_deg": [90, 0, 0]}
    built = Build123dGeometryService()._compile(raw)
    for face in built.faces:
        group = MeshGroup("test")
        tessellate_face(face, group, 0.1, 0.1)
        assert group.indices
        # Reverse glTF's Y-up mapping before comparing with the exact B-rep.
        points = [(x, -z, y) for x, y, z in group.positions]
        for start in range(0, len(group.indices), 3):
            a, b, c = [points[i] for i in group.indices[start:start + 3]]
            u = [b[i] - a[i] for i in range(3)]
            v = [c[i] - a[i] for i in range(3)]
            normal = (u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0])
            if sum(n*n for n in normal) < 1e-20:
                continue
            center = tuple((a[i]+b[i]+c[i])/3 for i in range(3))
            outward = face.normal_at(center)
            assert sum(n*r for n, r in zip(normal, outward)) > 0


def test_identity_survives_driving_edits_and_rotated_plane(raw):
    service = Build123dGeometryService()
    bore = capture(service, raw, [10, 0, 4])["reference"]
    top = capture(service, raw, [20, 0, 8])["reference"]
    raw["relationships"]["parameters"]["plate_width"]["value"] = 140
    raw["relationships"]["parameters"]["bore_diameter"]["value"] = 30
    raw["relationships"]["parameters"]["thickness"]["value"] = 12
    raw["features"][0]["placement"] = {"origin": [10, 20, 30], "rotation_deg": [90, 0, 0]}
    assert resolve(service, raw, bore)["face"]["area_mm2"] == pytest.approx(3.141592653589793 * 30 * 12)
    cap = resolve(service, raw, top)
    assert cap["status"] == "resolved" and cap["face"]["normal"] == pytest.approx([0, -1, 0])
    assert cap["face"]["point"][1] == pytest.approx(8)


@pytest.mark.parametrize("mutation,status", [("entity", "missing"), ("profile", "missing"), ("split", "ambiguous"), ("consumer", "ambiguous"), ("taper", "unsupported")])
def test_no_retarget_on_deleted_replaced_or_ambiguous_geometry(raw, mutation, status):
    service = Build123dGeometryService()
    ref = capture(service, raw, [10, 0, 4])["reference"]
    if mutation == "entity":
        params = raw["features"][0]["parameters"]
        params["entities"][1]["id"] = "ent_replacement"
        for c in params["constraints"]:
            for key in ("entity_id", "entity_a", "entity_b"):
                if c.get(key) == "ent_bore": c[key] = "ent_replacement"
    elif mutation == "profile":
        raw["features"][0]["id"] = "feat_replacement"
        raw["features"][1]["parameters"]["target_feature"] = "feat_replacement"
        for b in raw["relationships"]["bindings"]:
            if b["feature_id"] == "feat_profile": b["feature_id"] = "feat_replacement"
    elif mutation == "split":
        raw["features"].append({"id": "feat_cut", "kind": "box", "operation": "subtract",
            "parameters": {"length": 120, "width": 5, "height": 8}, "placement": {"origin": [0, 0, 0]}})
    elif mutation == "consumer":
        raw["features"].append({**copy.deepcopy(raw["features"][1]), "id": "feat_duplicate"})
    else: raw["features"][1]["parameters"]["taper"] = 2
    assert resolve(service, raw, ref)["status"] == status
    if mutation == "consumer":
        nodes = glb_nodes(service.build(raw))
        assert all(n["extras"]["surface_reference"] is None for n in nodes)
        assert all(n["extras"]["selection_error"] for n in nodes)


def test_two_identical_openings_do_not_merge_in_export(raw):
    service = Build123dGeometryService()
    params = raw["features"][0]["parameters"]
    params["entities"].append({"id": "ent_second", "kind": "circle", "operation": "subtract", "diameter": 20, "x": 30})
    params["constraints"].extend([{"id": "con_second_" + p, "kind": "dimension", "entity_id": "ent_second", "parameter": p, "value": v}
                                  for p, v in [("x", 30), ("y", 0), ("diameter", 20)]])
    refs = [n["extras"]["surface_reference"] for n in glb_nodes(service.build(raw)) if n["extras"]["label"] == "bore"]
    assert {r["entity_id"] for r in refs} == {"ent_bore", "ent_second"}
    assert all(resolve(service, raw, ref)["status"] == "resolved" for ref in refs)


def test_repeated_nested_instances_keep_source_and_component_identity(raw):
    service = Build123dGeometryService()
    part = Ledger.model_validate(raw)
    inner = Ledger.model_validate({"assembly": {"components": [{"id": "comp_inner", "sub_assembly_id": part.project_id}]}})
    root = Ledger.model_validate({"assembly": {"components": [
        {"id": "comp_left", "sub_assembly_id": inner.project_id, "placement": {"origin": [-100, 0, 0]}},
        {"id": "comp_right", "sub_assembly_id": inner.project_id, "placement": {"origin": [100, 0, 0]}}]}})
    docs = {l.project_id: l for l in [part, inner, root]}
    ref = capture(service, root, [-90, 0, 4], docs=docs)["reference"]
    assert ref["component_path"] == ["comp_left", "comp_inner"] and ref["source_ledger_id"] == part.project_id
    refs = [n["extras"]["surface_reference"] for n in glb_nodes(service.build(root, all_ledgers=docs)) if n["extras"]["label"] == "bore"]
    assert {tuple(r["component_path"]) for r in refs} == {("comp_left", "comp_inner"), ("comp_right", "comp_inner")}
    assert all(resolve(service, root, r, docs)["status"] == "resolved" for r in refs)
