from copy import deepcopy
from typing import Any

import pytest
from cadenza_contracts.ledger import Ledger
from cadenza_geometry.service import Build123dGeometryService
from cadenza_geometry.contracts import GeometryError
from cadenza_geometry.frames import to_world


def reference(source, role="top_face", kind="box"):
    return dict(ledger_id=source, source_ledger_id=source, feature_id="feat_stock",
                feature_kind=kind, operation="add", role=role)


def fixture():
    parts = {
        "plate": dict(kind="box", parameters=dict(length=100, width=60, height=15)),
        "spacer": dict(kind="cylinder", parameters=dict(diameter=20, height=30)),
        "cap": dict(kind="box", parameters=dict(length=100, width=60, height=8)),
    }
    docs: dict[str, Any] = {lid: dict(project_id=lid, features=[dict(id="feat_stock", **feature)]) for lid, feature in parts.items()}
    components = [dict(id=f"comp_{name}", sub_assembly_id=source,
                       placement=dict(origin=point, rotation_deg=[0, 0, 0]))
                  for name, source, point in (("base", "plate", [0, 0, 0]), ("left", "spacer", [-30, 0, 15]),
                    ("right", "spacer", [30, 0, 15]), ("cap", "cap", [0, 0, 45]))]
    root: dict[str, Any] = dict(project_id="root", assembly=dict(components=components, joints=[]))
    docs["root"] = root
    service = Build123dGeometryService()
    for a, b, source_a, source_b, point_a, point_b, kind_a, kind_b in (
        ("base", "left", "plate", "spacer", [-30, 0, 15], [0, 0, 0], "box", "cylinder"),
        ("base", "right", "plate", "spacer", [30, 0, 15], [0, 0, 0], "box", "cylinder"),
        ("left", "cap", "spacer", "cap", [0, 0, 30], [-30, 0, 0], "cylinder", "box"),
    ):
        op = dict(op="add_planar_attachment", component_a=f"comp_{a}", component_b=f"comp_{b}",
            target_a=dict(reference=reference(source_a, kind=kind_a), point=point_a, normal=[0, 0, 1]),
            target_b=dict(reference=reference(source_b, "bottom_face", kind_b), point=point_b, normal=[0, 0, -1]))
        captured = service.run_probe_query(docs["root"], dict(query_type="capture_planar_attachment", operation=op), docs)
        docs["root"]["assembly"]["joints"].append(dict(id=f"joint_{b}", kind="rigid",
            component_a=op["component_a"], component_b=op["component_b"], **captured))
    return docs


@pytest.mark.parametrize("height", [12, 15, 18])
def test_changed_plate_follows_faces_preserves_offsets_and_chain(height):
    docs = fixture()
    docs["plate"]["features"][0]["parameters"]["height"] = height
    service = Build123dGeometryService()
    built = service._compile(docs["root"], docs)
    for name, point in (("left", [-30, 0, height]), ("right", [30, 0, height]), ("cap", [0, 0, height + 30])):
        assert to_world(built.instances[f"comp_{name}"].location, (0, 0, 0)) == pytest.approx(point)
    assert len(built.contacts) == 3
    assert all(c["area_mm2"] > 300 and c["contact_verified"] for c in built.contacts)
    assert Ledger.model_validate(docs["root"]).assembly is not None


@pytest.mark.parametrize("change", ["shrink", "suppress", "source", "unknown", "cycle", "driver"])
def test_invalid_attachments_fail_closed(change):
    docs = fixture()
    joints = docs["root"]["assembly"]["joints"]
    if change == "shrink": docs["plate"]["features"][0]["parameters"]["length"] = 40
    if change == "suppress": docs["plate"]["features"][0]["suppressed"] = True
    if change == "source": joints[0]["target_a"]["reference"].update(ledger_id="wrong", source_ledger_id="wrong")
    if change == "unknown": joints[0]["target_a"]["mode"] = "future_face"
    if change == "cycle": joints[0]["component_a"] = "comp_cap"
    if change == "driver": joints.append({**deepcopy(joints[0]), "id": "joint_duplicate"})
    with pytest.raises(GeometryError): Build123dGeometryService()._compile(docs["root"], docs)


def test_upstream_twist_and_repeated_rotated_nested_owners():
    from build123d import Location
    docs = fixture()
    docs["root"]["assembly"]["components"][0]["placement"]["rotation_deg"] = [0, 0, 35]
    docs["plate"]["features"][0]["parameters"]["height"] = 12
    docs["outer"] = dict(project_id="outer", assembly=dict(components=[
        dict(id="comp_first", sub_assembly_id="root", placement=dict(origin=[200, 0, 0], rotation_deg=[0, 90, 20])),
        dict(id="comp_second", sub_assembly_id="root", placement=dict(origin=[-200, 0, 0], rotation_deg=[0, 0, -45]))]))
    built = Build123dGeometryService()._compile(docs["outer"], docs)
    for name, outer in (("first", Location((200, 0, 0), (0, 90, 20))), ("second", Location((-200, 0, 0), (0, 0, -45)))):
        rotation = Location((0, 0, 0), (0, 0, 35))
        for part, point in (("left", (-30, 0, 12)), ("right", (30, 0, 12)), ("cap", (0, 0, 42))):
            leaf = built.instances[f"comp_{name}/comp_{part}"]
            assert to_world(leaf.location, (0, 0, 0)) == pytest.approx(to_world(outer * rotation, point))
            # Tangent orientation travels through the cap chain, not only normals.
            from cadenza_geometry.frames import dir_to_world
            assert dir_to_world(leaf.location, (1, 0, 0)) == pytest.approx(dir_to_world(outer * rotation, (1, 0, 0)))


@pytest.mark.parametrize("x", [2, 3])
def test_edge_only_and_disjoint_faces_never_pass_contact(x):
    from build123d import Box, Axis, Location, Plane
    from cadenza_geometry.planar_attachments import verify_contact
    a = Box(2, 2, 1).faces().sort_by(Axis.Z)[-1]
    b = Location((x, 0, 1)) * Box(2, 2, 1).faces().sort_by(Axis.Z)[0]
    fa = Plane((1, 0, .5), x_dir=(1, 0, 0), z_dir=(0, 0, 1)).location
    fb = Plane((1, 0, .5), x_dir=(1, 0, 0), z_dir=(0, 0, -1)).location
    with pytest.raises(GeometryError): verify_contact(a, fa, b, fb, "joint_test")


def test_contact_kernel_failure_fails_closed(monkeypatch):
    from build123d import Face
    docs = fixture()
    def fail(*args, **kwargs): raise RuntimeError("injected contact failure")
    monkeypatch.setattr(Face, "intersect", fail)
    with pytest.raises(GeometryError, match="could not be verified"):
        Build123dGeometryService()._compile(docs["root"], docs)


def test_hole_through_recorded_anchor_rejects_edit():
    docs = fixture()
    docs["plate"]["features"].append(dict(id="feat_hole", kind="hole", operation="subtract",
        parameters=dict(diameter=6, depth=15, through=True), placement=dict(origin=[-30, 0, 15])))
    with pytest.raises(GeometryError, match="outside"):
        Build123dGeometryService()._compile(docs["root"], docs)


def test_mixed_chain_preserves_coordinate_semantics_and_unplaced_pose():
    docs = fixture()
    docs["root"]["assembly"]["joints"][-1] = dict(id="joint_cap", kind="rigid", component_a="comp_left", component_b="comp_cap",
        target_a=dict(point=[-30, 0, 45]), target_b=dict(point=[-30, 0, 45]))
    docs["root"]["assembly"]["components"].append(dict(id="comp_free", sub_assembly_id="spacer", placement=dict(origin=[200, 0, 15])))
    docs["plate"]["features"][0]["parameters"]["height"] = 12
    docs["root"]["assembly"]["joints"].reverse()
    built = Build123dGeometryService()._compile(docs["root"], docs)
    assert to_world(built.instances["comp_cap"].location, (0, 0, 0)) == pytest.approx([0, 0, 42])
    assert to_world(built.instances["comp_free"].location, (0, 0, 0)) == pytest.approx([200, 0, 15])
    assert len(built.contacts) == 2


def test_feature_local_rotation_tracks_tangent_frame():
    from build123d import Location
    docs = fixture()
    docs["plate"]["features"][0]["placement"] = dict(origin=[0, 0, 0], rotation_deg=[0, 0, 30])
    built = Build123dGeometryService()._compile(docs["root"], docs)
    assert to_world(built.instances["comp_left"].location, (0, 0, 0)) == pytest.approx(to_world(Location((0, 0, 0), (0, 0, 30)), (-30, 0, 15)))


@pytest.mark.parametrize("case", ["gap", "normal", "small_area", "valid_area"])
def test_native_contact_tolerance_boundaries(case):
    import math
    from build123d import Box, Axis, Location, Plane
    from cadenza_geometry.planar_attachments import verify_contact, DISTANCE_TOL, NORMAL_TOL, AREA_TOL
    width = math.sqrt(AREA_TOL * (0.5 if case == "small_area" else 2))
    a = Box(width, width, 1).faces().sort_by(Axis.Z)[-1]
    b = Location((0, 0, 1)) * Box(width, width, 1).faces().sort_by(Axis.Z)[0]
    fa = Plane((0, 0, .5), x_dir=(1, 0, 0), z_dir=(0, 0, 1)).location
    fb = Plane((0, 0, .5), x_dir=(1, 0, 0), z_dir=(0, 0, -1)).location
    if case == "gap":
        shift = Location((0, 0, DISTANCE_TOL * 2))
        b, fb = shift * b, shift * fb
    if case == "normal":
        fb = fb * Location((0, 0, 0), (math.degrees(math.acos(1 - NORMAL_TOL * 2)), 0, 0))
    if case == "valid_area":
        assert verify_contact(a, fa, b, fb, "joint_test")["area_mm2"] == pytest.approx(AREA_TOL * 2)
    else:
        with pytest.raises(GeometryError): verify_contact(a, fa, b, fb, "joint_test")
