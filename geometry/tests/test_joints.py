from dataclasses import replace

import pytest
from build123d import Location
from build123d.topology import Shape

from cadenza_geometry.builder import build_assembly
from cadenza_geometry.contracts import GeometryError, GeometryErrorCode
from cadenza_geometry.frames import dir_to_world, to_world
from cadenza_geometry.joints import solve_rigid_joints
from cadenza_geometry.plan import AssemblyPlan, RJoint, resolve


def joint(a="comp_a", b="comp_b", **kwargs):
    return RJoint(id=f"joint_{a}_{b}", kind="rigid", component_a=a, component_b=b,
                  target_a_pt=(0, 0, 10), target_b_pt=(30, 0, 0), **kwargs)


def document(joints):
    return {"assembly": {"parts": [{"id": "part_box", "features": [{"id": "feat_box", "kind": "box",
        "parameters": {"length": 10, "width": 10, "height": 10}}]}],
        "components": [{"id": f"comp_{name}", "part_id": "part_box", "placement": {"origin": [i * 30, 0, 0]}}
                       for i, name in enumerate("abc")], "joints": joints}}


def target_joint(a, b, point_a, point_b):
    return {"id": f"joint_{a}_{b}", "kind": "rigid", "component_a": f"comp_{a}", "component_b": f"comp_{b}",
            "target_a": {"point": point_a, "normal": [0, 0, 1]},
            "target_b": {"point": point_b, "normal": [0, 0, -1]}}


def test_unordered_chain_builds_three_stacked_solids():
    ab = target_joint("a", "b", [0, 0, 10], [30, 0, 0])
    bc = target_joint("b", "c", [30, 0, 10], [60, 0, 0])
    for joints in ([ab, bc], [bc, ab]):
        plan = resolve(document(joints))
        assert isinstance(plan, AssemblyPlan)
        solid = build_assembly(plan)
        assert len(solid.solids()) == 3
        assert solid.volume == pytest.approx(3000)
        box = solid.bounding_box()
        assert box.min.Z == pytest.approx(0) and box.max.Z == pytest.approx(30)
        assert box.size.X == pytest.approx(10)


def test_rotated_component_and_rotated_anchor():
    initial = {"comp_a": Location((5, 6, 7), (0, 90, 0)), "comp_b": Location((30, 0, 0), (90, 0, 0))}
    point_a = to_world(initial["comp_a"], (0, 0, 10))
    normal_a = dir_to_world(initial["comp_a"], (0, 0, 1))
    normal_b = dir_to_world(initial["comp_b"], (0, 0, -1))
    link = replace(joint(target_a_norm=normal_a, target_b_norm=normal_b), target_a_pt=point_a)
    solved = solve_rigid_joints(initial, [link])
    assert to_world(solved["comp_b"], (0, 0, 0)) == pytest.approx(point_a)
    assert dir_to_world(solved["comp_b"], (0, 0, -1)) == pytest.approx(tuple(-v for v in normal_a))
    assert to_world(solved["comp_a"], (0, 0, 0)) == pytest.approx((5, 6, 7))


def test_normals_half_turn_and_translation_only():
    initial = {"comp_a": Location(), "comp_b": Location((30, 0, 0))}
    solved = solve_rigid_joints(initial, [joint(target_a_norm=(0, 0, 2), target_b_norm=(0, 0, 3))])
    assert dir_to_world(solved["comp_b"], (0, 0, 1)) == pytest.approx((0, 0, -1))
    initial["comp_b"] = Location((30, 0, 0), (23, 42, 11))
    solved = solve_rigid_joints(initial, [joint()])
    assert dir_to_world(solved["comp_b"], (1, 0, 0)) == pytest.approx(dir_to_world(initial["comp_b"], (1, 0, 0)))
    assert to_world(solved["comp_b"], (0, 0, 0)) == pytest.approx((0, 0, 10))


@pytest.mark.parametrize("kind", ["revolute", "slider", "cylindrical", "ball"])
def test_motion_joints_are_not_silently_rigid(kind):
    with pytest.raises(GeometryError) as error:
        solve_rigid_joints({"comp_a": Location(), "comp_b": Location()}, [replace(joint(), kind=kind)])
    assert error.value.code == GeometryErrorCode.UNSUPPORTED_FEATURE


@pytest.mark.parametrize("links", [
    [joint("comp_a", "comp_missing")], [joint("comp_a", "comp_a")],
    [joint(), joint("comp_c", "comp_b")], [joint(), joint("comp_b", "comp_a")],
    [joint(target_a_norm=(0, 0, 1))], [joint(target_a_norm=(0, 0, 0), target_b_norm=(0, 0, -1))],
])
def test_invalid_attachment_graphs_and_normals(links):
    with pytest.raises(GeometryError) as error:
        solve_rigid_joints({name: Location() for name in ("comp_a", "comp_b", "comp_c")}, links)
    assert error.value.code == GeometryErrorCode.INVALID_PARAMETER


def test_feature_bound_target_explicitly_rejected():
    link = target_joint("a", "b", [0, 0, 10], [30, 0, 0])
    link["target_a"]["reference_feature"] = "feat_box"
    with pytest.raises(GeometryError) as error:
        resolve(document([link]))
    assert error.value.code == GeometryErrorCode.UNSUPPORTED_FEATURE


def test_interference_check_failure_cannot_accept_unchecked_assembly(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("simulated intersection failure")
    monkeypatch.setattr(Shape, "intersect", broken)
    plan = resolve(document([]))
    assert isinstance(plan, AssemblyPlan)
    with pytest.raises(GeometryError) as error:
        build_assembly(plan)
    assert error.value.code == GeometryErrorCode.KERNEL_FAILURE
    assert "Could not check interference" in error.value.message
