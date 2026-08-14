"""Ledger -> solid -> GLB + STEP. The milestone-1 path."""

from __future__ import annotations

import math

import pytest
from conftest import BOX_VOLUME, feature, hole_volume, ledger, plate_with_hole

from cadenza_geometry import GeometryError, GeometryErrorCode, ledger_to_glb_step, resolve
from cadenza_geometry.builder import build_solid
from cadenza_geometry.glb import parse_glb


# --------------------------------------------------------------------------- #
# volumes — the check that the kernel did what the ledger said
# --------------------------------------------------------------------------- #


def test_box_volume_and_placement():
    plan = resolve(ledger(feature("feat_b_1", "box", {"length": 100, "width": 60, "height": 20})))
    solid = build_solid(plan)
    assert solid.volume == pytest.approx(100 * 60 * 20)
    bb = solid.bounding_box()
    # Placement contract: origin is the CENTRE of the footprint at the BASE,
    # so the solid runs from z=0 up, and is centred in x/y.
    assert (bb.min.X, bb.min.Y, bb.min.Z) == pytest.approx((-50, -30, 0))
    assert (bb.max.X, bb.max.Y, bb.max.Z) == pytest.approx((50, 30, 20))


def test_cylinder_volume():
    plan = resolve(ledger(feature("feat_c_1", "cylinder", {"diameter": 40, "height": 15})))
    solid = build_solid(plan)
    assert solid.volume == pytest.approx(math.pi * 20**2 * 15, rel=1e-6)


def test_plate_with_through_hole_matches_analytic_volume():
    plan = resolve(plate_with_hole())
    solid = build_solid(plan)
    assert solid.volume == pytest.approx(BOX_VOLUME - hole_volume(10, 20), rel=1e-9)
    assert solid.is_valid
    # 6 box faces + 1 bore. If a boolean had gone wrong this is where it shows.
    assert len(solid.faces()) == 7


def test_blind_hole_leaves_a_floor():
    led = ledger(
        feature("feat_b_1", "box", {"length": 50, "width": 50, "height": 30}),
        feature(
            "feat_pocket_1",
            "hole",
            {"diameter": 10, "through": False, "depth": 12},
            origin=(0, 0, 30),
        ),
    )
    solid = build_solid(resolve(led))
    assert solid.volume == pytest.approx(50 * 50 * 30 - hole_volume(10, 12), rel=1e-6)
    # A blind hole adds a bore AND a floor: 6 + 2 = 8.
    assert len(solid.faces()) == 8
    # The floor sits exactly at depth below the entry face, not at EPS below it.
    assert solid.bounding_box().min.Z == pytest.approx(0.0)


def test_offset_hole_is_cut_where_the_ledger_says():
    solid = build_solid(resolve(plate_with_hole(diameter=8, hole_xy=(20, -15))))
    assert solid.volume == pytest.approx(BOX_VOLUME - hole_volume(8, 20), rel=1e-9)
    bore = [f for f in solid.faces() if str(f.geom_type) == "GeomType.CYLINDER"]
    assert len(bore) == 1
    axis = bore[0].axis_of_rotation
    assert (axis.position.X, axis.position.Y) == pytest.approx((20, -15))


def test_rotated_feature_is_placed_by_the_same_frame_provenance_uses():
    """A 90-degree rotation about X turns a tall box into a deep one. If
    builder.py and frames.py ever disagree about Euler convention, this is
    the test that catches it."""
    led = ledger(
        feature("feat_b_1", "box", {"length": 40, "width": 10, "height": 60}, rotation=(90, 0, 0))
    )
    solid = build_solid(resolve(led))
    bb = solid.bounding_box()
    assert solid.volume == pytest.approx(40 * 10 * 60)
    assert (bb.max.X - bb.min.X) == pytest.approx(40)
    assert (bb.max.Y - bb.min.Y) == pytest.approx(60)   # height swung into Y
    assert (bb.max.Z - bb.min.Z) == pytest.approx(10)


def test_stacked_boxes_fuse():
    led = ledger(
        feature("feat_base_1", "box", {"length": 60, "width": 60, "height": 10}),
        feature("feat_riser_1", "box", {"length": 20, "width": 20, "height": 30}, origin=(0, 0, 10)),
    )
    solid = build_solid(resolve(led))
    assert solid.volume == pytest.approx(60 * 60 * 10 + 20 * 20 * 30)
    assert len(solid.solids()) == 1


def test_suppressed_features_are_invisible_to_geometry():
    led = ledger(
        feature("feat_b_1", "box", {"length": 50, "width": 50, "height": 10}),
        feature("feat_h_1", "hole", {"diameter": 10, "through": True}, origin=(0, 0, 10),
                suppressed=True),
    )
    solid = build_solid(resolve(led))
    assert solid.volume == pytest.approx(50 * 50 * 10)


# --------------------------------------------------------------------------- #
# exports
# --------------------------------------------------------------------------- #


def test_ledger_to_glb_step_returns_bytes():
    glb, step = ledger_to_glb_step(plate_with_hole())
    assert isinstance(glb, bytes) and isinstance(step, bytes)
    assert len(glb) > 0 and len(step) > 0


def test_step_is_a_real_step_file():
    _, step = ledger_to_glb_step(plate_with_hole())
    text = step.decode("utf-8", "replace")
    assert text.startswith("ISO-10303-21;")
    assert text.rstrip().endswith("END-ISO-10303-21;")
    # STEP carries the exact B-rep, so the bore must be there as a real
    # cylindrical surface — this is what occt-wasm reloads for raycasting.
    assert "CYLINDRICAL_SURFACE" in text
    assert "ADVANCED_BREP_SHAPE_REPRESENTATION" in text


def test_glb_is_structurally_valid_and_loadable():
    glb, _ = ledger_to_glb_step(plate_with_hole())
    gltf, binary = parse_glb(glb)  # raises on bad magic/version/length

    assert gltf["asset"]["version"] == "2.0"
    assert gltf["buffers"][0]["byteLength"] == len(binary)

    # Every accessor must address memory that actually exists, and POSITION
    # accessors must carry min/max — the two things viewers reject GLBs over.
    for mesh in gltf["meshes"]:
        for prim in mesh["primitives"]:
            pos = gltf["accessors"][prim["attributes"]["POSITION"]]
            assert pos["type"] == "VEC3" and "min" in pos and "max" in pos
            assert len(pos["min"]) == 3 and len(pos["max"]) == 3
            idx = gltf["accessors"][prim["indices"]]
            assert idx["count"] % 3 == 0

            for acc in (pos, gltf["accessors"][prim["attributes"]["NORMAL"]], idx):
                view = gltf["bufferViews"][acc["bufferView"]]
                width = 12 if acc["type"] == "VEC3" else 4
                assert view["byteOffset"] % 4 == 0
                assert view["byteOffset"] + acc["count"] * width <= len(binary)

            # Indices must stay inside the vertex array they index.
            import struct

            view = gltf["bufferViews"][idx["bufferView"]]
            raw = binary[view["byteOffset"] : view["byteOffset"] + view["byteLength"]]
            values = struct.unpack(f"<{idx['count']}I", raw)
            assert max(values) < pos["count"]


def test_glb_nodes_are_named_with_ledger_feature_ids():
    """The provenance-in-the-GLB trick: a Three.js raycast hit carries the
    ledger id in `object.name`, so the common case needs no round trip."""
    glb, _ = ledger_to_glb_step(plate_with_hole())
    gltf, _ = parse_glb(glb)
    names = {n["name"] for n in gltf["nodes"]}
    assert names == {"feat_base_plate_9f2a", "feat_bore_1a2b"}
    by_name = {n["name"]: n["extras"] for n in gltf["nodes"]}
    assert by_name["feat_bore_1a2b"]["labels"] == ["bore"]
    assert "top_face" in by_name["feat_base_plate_9f2a"]["labels"]


def test_glb_geometry_lands_in_the_right_place_after_the_y_up_swap():
    """glTF is Y-up, OCC is Z-up. A sign error here is invisible in the JSON
    and shows up as a model lying on its side in the viewer."""
    glb, _ = ledger_to_glb_step(plate_with_hole())
    gltf, _ = parse_glb(glb)
    mins = [gltf["accessors"][p["attributes"]["POSITION"]]["min"]
            for m in gltf["meshes"] for p in m["primitives"]]
    maxs = [gltf["accessors"][p["attributes"]["POSITION"]]["max"]
            for m in gltf["meshes"] for p in m["primitives"]]
    lo = [min(c[i] for c in mins) for i in range(3)]
    hi = [max(c[i] for c in maxs) for i in range(3)]
    # Model is 100(x) x 100(y) x 20(z) in OCC -> 100(x) x 20(y) x 100(z) in glTF
    assert (hi[0] - lo[0]) == pytest.approx(100, abs=0.1)
    assert (hi[1] - lo[1]) == pytest.approx(20, abs=0.1)
    assert (hi[2] - lo[2]) == pytest.approx(100, abs=0.1)


def test_build_result_reports_entities_and_no_untraceable_faces():
    from cadenza_geometry import build

    result = build(plate_with_hole())
    assert result.entities["feat_bore_1a2b"] == ["bore"]
    assert set(result.entities["feat_base_plate_9f2a"]) == {
        "top_face", "bottom_face", "side_x_pos", "side_x_neg", "side_y_pos", "side_y_neg",
    }
    assert result.volume == pytest.approx(BOX_VOLUME - hole_volume(10, 20), rel=1e-9)
    assert not [w for w in result.warnings if "could not be traced" in w]


# --------------------------------------------------------------------------- #
# invariant gates — rejected before the kernel is touched
# --------------------------------------------------------------------------- #


def test_negative_dimension_is_rejected_with_the_feature_named():
    with pytest.raises(GeometryError) as e:
        resolve(ledger(feature("feat_b_1", "box", {"length": -5, "width": 10, "height": 2})))
    assert e.value.code is GeometryErrorCode.INVALID_PARAMETER
    assert e.value.feature_id == "feat_b_1"


def test_blind_hole_without_depth_is_rejected():
    led = ledger(
        feature("feat_b_1", "box", {"length": 50, "width": 50, "height": 10}),
        feature("feat_h_1", "hole", {"diameter": 5, "through": False}, origin=(0, 0, 10)),
    )
    with pytest.raises(GeometryError) as e:
        resolve(led)
    assert "depth" in e.value.message


def test_hole_outside_the_envelope_is_rejected_rather_than_silently_cutting_nothing():
    led = ledger(
        feature("feat_b_1", "box", {"length": 40, "width": 40, "height": 10}),
        feature("feat_h_1", "hole", {"diameter": 6, "through": True}, origin=(500, 0, 10)),
    )
    with pytest.raises(GeometryError) as e:
        resolve(led)
    assert e.value.feature_id == "feat_h_1"
    assert "outside the part envelope" in e.value.message
    assert e.value.detail and "move the hole" in e.value.detail


def test_leading_subtract_is_rejected():
    led = ledger(feature("feat_h_1", "hole", {"diameter": 5, "through": True}))
    with pytest.raises(GeometryError):
        resolve(led)


def test_unknown_feature_kind_names_what_is_supported():
    with pytest.raises(GeometryError) as e:
        resolve(ledger(feature("feat_x_1", "torus", {"diameter": 5})))
    assert e.value.code is GeometryErrorCode.UNSUPPORTED_FEATURE


def test_empty_ledger_is_rejected():
    with pytest.raises(GeometryError) as e:
        resolve(ledger())
    assert e.value.code is GeometryErrorCode.EMPTY_RESULT


def test_cut_that_consumes_the_whole_part_is_reported_as_empty():
    led = ledger(
        feature("feat_b_1", "box", {"length": 10, "width": 10, "height": 10}),
        feature("feat_c_1", "box", {"length": 50, "width": 50, "height": 50},
                origin=(0, 0, -20), operation="subtract"),
    )
    with pytest.raises(GeometryError) as e:
        build_solid(resolve(led))
    assert e.value.code is GeometryErrorCode.EMPTY_RESULT
