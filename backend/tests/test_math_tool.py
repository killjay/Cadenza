import pytest

from cadenza_backend.tools import registry


def calculate(operation, **arguments):
    return registry.get("math_sandbox").execute(operation=operation, arguments=arguments)


def test_bolt_circle_and_nested_point():
    points = calculate("bolt_circle", radius=10, count=4)["result"]
    assert len(points) == 4
    assert points[1]["x"] == pytest.approx(0, abs=1e-12)
    assert points[1]["y"] == pytest.approx(10)
    assert calculate("mirror_2d", point={"x": 2, "y": 3})["result"] == {"x": 2, "y": -3}


@pytest.mark.parametrize("count", [0, -1, 1025, 2.5, True, "4", float("inf")])
def test_invalid_counts_are_rejected(count):
    assert "error" in calculate("bolt_circle", radius=10, count=count)


@pytest.mark.parametrize("radius", [float("nan"), float("inf"), -1, 0, True, "__import__('os')", 1e20])
def test_invalid_numbers_are_rejected(radius):
    assert "error" in calculate("bolt_circle", radius=radius, count=4)


def test_scripts_unknown_operations_and_extra_arguments_are_rejected(tmp_path):
    tool = registry.get("math_sandbox")
    marker = tmp_path / "should-not-exist"
    assert "error" in tool.execute(script=f"open({str(marker)!r}, 'w').write('executed')")
    assert not marker.exists()
    assert "error" in tool.execute(operation="__import__", arguments={})
    assert "error" in tool.execute(operation=[], arguments={})
    assert "error" in calculate("gear_ratio", driving_teeth=20, driven_teeth=40, script="anything")


def test_limits_apply_to_result_size_and_degenerate_geometry():
    assert "error" in calculate("grid_array", start_x=0, start_y=0, step_x=1, step_y=1, count_x=100, count_y=100)
    assert "error" in calculate("regular_polygon", radius=10, sides=2)
    assert "error" in calculate("gear_ratio", driving_teeth=0, driven_teeth=20)
    assert calculate("gear_ratio", driving_teeth=20, driven_teeth=40)["result"] == 2
    assert "warning" in calculate("bearing_bore", bearing_od=20)
