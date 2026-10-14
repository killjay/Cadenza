import json
import struct

import pytest
from build123d import Location

from cadenza_contracts.fixtures import plate_with_hole
from cadenza_contracts.ledger import Ledger
from cadenza_geometry.contracts import SpatialQuery
from cadenza_geometry.frames import to_world, dir_to_world
from cadenza_geometry.service import Build123dGeometryService


def nested_design():
    part = plate_with_hole()
    inner = Ledger.model_validate({"assembly": {"components": [{"id": "comp_inner",
        "sub_assembly_id": part.project_id, "placement": {"origin": [10, 0, 0], "rotation_deg": [0, 90, 0]}}]}})
    root = Ledger.model_validate({"assembly": {"components": [
        {"id": "comp_left", "sub_assembly_id": inner.project_id,
         "placement": {"origin": [200, 0, 0], "rotation_deg": [0, 0, 90]}},
        {"id": "comp_right", "sub_assembly_id": inner.project_id, "placement": {"origin": [-200, 0, 0]}}]}})
    return root, part, {ledger.project_id: ledger for ledger in (root, inner, part)}


def test_rotated_nested_repeated_sources_export_and_probe():
    root, part, documents = nested_design()
    service = Build123dGeometryService()
    result = service.build(root, documents)
    glb_size = struct.unpack_from("<I", result.glb, 12)[0]
    gltf = json.loads(result.glb[20:20 + glb_size])
    paths = {node["extras"]["component_id"] for node in gltf["nodes"]}
    assert paths == {"comp_left/comp_inner", "comp_right/comp_inner"}
    assert all(any(key.startswith(path + ":") for key in result.entities) for path in paths)
    assert not any("sub_comp" in path for path in paths)
    assert service._compile(root, documents).ambiguity == {}
    for name, parent in (("comp_left", Location((200, 0, 0), (0, 0, 90))),
                         ("comp_right", Location((-200, 0, 0)))):
        transform = parent * Location((10, 0, 0), (0, 90, 0))
        point = to_world(transform, (20, 20, 20))
        normal = dir_to_world(transform, (0, 0, 1))
        hit = service.probe(root, SpatialQuery(point, normal=normal), documents)
        assert hit is not None
        assert hit.component_id == name + "/comp_inner"
        assert hit.source_ledger_id == part.project_id and hit.source_part_id is None
        assert hit.feature_id == part.features[0].id
        assert hit.source_point == pytest.approx((20, 20, 20))
        assert hit.source_normal == pytest.approx((0, 0, 1))
        assert hit.confidence == pytest.approx(1)
        assert hit.to_dict()["component_id"] == hit.component_id


def test_dependency_edit_invalidates_transformed_attribution():
    root, part, documents = nested_design()
    service = Build123dGeometryService()
    before = service._compile(root, documents)
    edited = part.model_dump(mode="json")
    edited["features"][0]["parameters"]["height"] = 30
    documents[part.project_id] = Ledger.model_validate(edited)
    after = service._compile(root, documents)
    assert after is not before
    transform = Location((200, 0, 0), (0, 0, 90)) * Location((10, 0, 0), (0, 90, 0))
    hit = service.probe(root, SpatialQuery(to_world(transform, (20, 20, 30)),
        normal=dir_to_world(transform, (0, 0, 1))), documents)
    assert hit is not None and hit.source_point == pytest.approx((20, 20, 30))
    assert hit.component_id == "comp_left/comp_inner"


def boxes(*, joined=False):
    return Ledger.model_validate({"assembly": {"parts": [{"id": "part_box", "features": [
        {"id": "feat_box", "kind": "box", "parameters": {"length": 10, "width": 10, "height": 10}}]}],
        "components": [{"id": "comp_a", "part_id": "part_box"},
                       {"id": "comp_b", "part_id": "part_box", "placement": {"origin": [30 if joined else 10, 0, 0]}}],
        "joints": [{"id": "joint_stack", "kind": "rigid", "component_a": "comp_a", "component_b": "comp_b",
                    "target_a": {"point": [0, 0, 10]}, "target_b": {"point": [30, 0, 0]}}] if joined else []}})


def test_joint_moved_instance_keeps_feature_identity():
    root = boxes(joined=True)
    service = Build123dGeometryService()
    hit = service.probe(root, SpatialQuery((2, 2, 20), normal=(0, 0, 1)))
    assert hit is not None and hit.component_id == "comp_b" and hit.feature_id == "feat_box"
    assert hit.source_part_id == "part_box" and hit.source_ledger_id == root.project_id
    assert hit.source_point == pytest.approx((2, 2, 10))
    assert service.build(root).entities


def test_same_feature_on_two_touching_instances_is_ambiguous():
    root = boxes()
    hit = Build123dGeometryService().probe(root, SpatialQuery((5, 0, 5)))
    assert hit is not None and hit.confidence <= 0.5
    assert any(rival.endswith(":feat_box") for rival in hit.ambiguous_with)
    hit = Build123dGeometryService().probe(root, SpatialQuery((5, 0, 5), normal=(-1, 0, 0)))
    assert hit is not None and hit.component_id == "comp_b" and hit.confidence <= 0.5


def test_unattributed_instance_does_not_select_neighbor():
    root = boxes()
    service = Build123dGeometryService()
    built = service._compile(root)
    built.attribution = {i: value for i, value in built.attribution.items() if value[0] != "comp_a"}
    assert service.probe(root, SpatialQuery((0, 0, 10))) is None
