import math

import pytest

from cadenza_contracts.fixtures import HOLE_ID, PLATE_ID, plate_with_hole
from cadenza_contracts.ledger import Ledger
from cadenza_geometry.service import Build123dGeometryService


@pytest.fixture(scope="module")
def nested_query():
    source = plate_with_hole()
    inner = Ledger.model_validate({"assembly": {"components": [{"id": "comp_inner",
        "sub_assembly_id": source.project_id, "placement": {"origin": [10, 0, 0], "rotation_deg": [0, 90, 0]}}]}})
    root = Ledger.model_validate({"assembly": {"components": [
        {"id": "comp_left", "sub_assembly_id": inner.project_id,
         "placement": {"origin": [200, 0, 0], "rotation_deg": [0, 0, 90]}},
        {"id": "comp_right", "sub_assembly_id": inner.project_id, "placement": {"origin": [-200, 0, 0]}}]}})
    service = Build123dGeometryService()
    documents = {ledger.project_id: ledger for ledger in (source, inner, root)}

    def query(kind, **kwargs):
        return service.run_probe_query(root, {"query_type": kind, **kwargs}, documents)
    return query


def test_nested_feature_and_instance_bounds_mass_in_assembly_frame(nested_query):
    target = {"component_1_id": "comp_left/comp_inner"}
    for feature in ({}, {"feature_1_id": PLATE_ID}):
        bounds = nested_query("get_bounding_box", **target, **feature)
        selected = bounds["independent_feature" if feature else "final_component"]
        assert selected["min"] == pytest.approx([150, 10, -50])
        assert selected["max"] == pytest.approx([250, 30, 50])
        assert bounds["final_assembly"]["min"] == pytest.approx([-190, -50, -50])
    mass = nested_query("get_mass_properties", **target)
    volume = 200000 - math.pi * 25 * 20
    assert mass["final_component"]["volume_mm3"] == pytest.approx(volume)
    assert mass["final_component"]["center_of_mass"] == pytest.approx([200, 20, 0])
    assert mass["final_assembly"]["volume_mm3"] == pytest.approx(2 * volume)
    feature_mass = nested_query("get_mass_properties", **target, feature_1_id=PLATE_ID)
    assert feature_mass["independent_feature"]["volume_mm3"] == pytest.approx(200000)
    assert feature_mass["surviving_feature"]["volume_mm3"] == pytest.approx(volume)
    assert feature_mass["surviving_feature"]["center_of_mass"] == pytest.approx([200, 20, 0])


def test_nested_faces_cylinder_and_edges_use_selected_instance(nested_query):
    target = {"component_1_id": "comp_left/comp_inner", "feature_1_id": PLATE_ID, "direction": "+Y"}
    face = nested_query("get_face_center", **target)["surviving_face_in_assembly"]
    assert face["center"] == pytest.approx([200, 30, 0])
    props = nested_query("get_face_properties", **target)["surviving_face_in_assembly"]
    assert props["normal"] == pytest.approx([0, 1, 0])
    for path, direction in (("comp_left/comp_inner", [0, 1, 0]), ("comp_right/comp_inner", [1, 0, 0])):
        target = {"component_1_id": path, "feature_1_id": HOLE_ID}
        axis = nested_query("get_cylinder_axis", **target)
        assert [abs(axis["direction"][key]) for key in ("x", "y", "z")] == pytest.approx(direction)
        origin = axis["origin"]
        if path.startswith("comp_left"):
            assert [origin["x"], origin["z"]] == pytest.approx([200, 0])
        else:
            assert [origin["y"], origin["z"]] == pytest.approx([0, 0])
        props = nested_query("get_face_properties", **target, direction="cylindrical")
        assert props["surviving_face_in_assembly"]["radius"] == pytest.approx(5)
        assert nested_query("get_edge_length", **target, edge_index=0)["length"] > 0
        assert "out of range" in nested_query("get_edge_length", **target, edge_index=999)["error"]


@pytest.mark.parametrize("features", [{}, {"feature_1_id": PLATE_ID, "feature_2_id": PLATE_ID}])
def test_pair_measurements_do_not_merge_repeated_source_features(nested_query, features):
    targets = {"component_1_id": "comp_left/comp_inner", "component_2_id": "comp_right/comp_inner", **features}
    assert nested_query("measure_distance", **targets)["distance_mm"] == pytest.approx(320)
    assert nested_query("get_minimum_clearance", **targets)["clearance"] == pytest.approx(320)
    collision = nested_query("check_collision", **targets)
    assert collision["collides"] is False and collision["touching"] is False
    assert collision["distance_mm"] == pytest.approx(320)


@pytest.mark.parametrize("kwargs", [
    {"feature_1_id": PLATE_ID},
    {"component_1_id": "comp_left"},
    {"component_1_id": "comp_missing/comp_inner"},
    {"component_1_id": "comp_left/comp_inner", "feature_1_id": "feat_missing"},
    {"component_1_id": "comp_left/comp_inner", "feature_1_id": ""},
    {"component_2_id": "comp_left/comp_inner"},
])
def test_invalid_instance_queries_fail_without_fallback(nested_query, kwargs):
    assert "error" in nested_query("get_bounding_box", **kwargs)


def test_pair_requires_both_explicit_instance_paths(nested_query):
    assert "component_2_id" in nested_query("measure_distance", component_1_id="comp_left/comp_inner",
        feature_1_id=PLATE_ID, feature_2_id=PLATE_ID)["error"]


def test_joint_placement_is_used_for_instance_measurements():
    root = Ledger.model_validate({"assembly": {"parts": [{"id": "part_box", "features": [
        {"id": "feat_box", "kind": "box", "parameters": {"length": 10, "width": 10, "height": 10}}]}],
        "components": [{"id": "comp_a", "part_id": "part_box"},
                       {"id": "comp_b", "part_id": "part_box", "placement": {"origin": [30, 0, 0]}}],
        "joints": [{"id": "joint_stack", "kind": "rigid", "component_a": "comp_a", "component_b": "comp_b",
                    "target_a": {"point": [0, 0, 10]}, "target_b": {"point": [30, 0, 0]}}]}})
    service = Build123dGeometryService()
    face = service.run_probe_query(root, {"query_type": "get_face_center", "component_1_id": "comp_b",
        "feature_1_id": "feat_box", "direction": "top"})
    assert face["surviving_face_in_assembly"]["center"] == pytest.approx([0, 0, 20])
    contact = service.run_probe_query(root, {"query_type": "check_collision", "component_1_id": "comp_a", "component_2_id": "comp_b"})
    assert contact["touching"] is True and contact["collides"] is False
    assert contact["intersection_volume_mm3"] == pytest.approx(0)
