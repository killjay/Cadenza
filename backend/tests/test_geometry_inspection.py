import pytest

from cadenza_backend import geometry_bridge
from cadenza_backend.tools import registry


def test_probe_requires_explicit_ledger_when_ambiguous(plate_ledger):
    tool = registry.get("geometry_probe")
    assert "error" in tool.execute(context={"ledgers": {"a": plate_ledger, "b": plate_ledger}}, query_type="get_mass_properties")
    assert "error" in tool.execute(context={"ledgers": {"a": plate_ledger}}, ledger_id="missing", query_type="get_mass_properties")
    assert "error" in tool.execute(context={"ledgers": {"a": plate_ledger}}, query_type="get_edge_length", edge_index=-1)


@pytest.mark.skipif(not geometry_bridge.REAL_GEOMETRY, reason="requires OCCT")
def test_registered_tool_measures_plate_and_hole(plate_ledger):
    tool = registry.get("geometry_probe")
    context = {"ledgers": {plate_ledger.project_id: plate_ledger}}
    def query(kind, **kwargs):
        result = tool.execute(context=context, ledger_id=plate_ledger.project_id, query_type=kind, **kwargs)
        assert "error" not in result, result
        return result
    bbox = query("get_bounding_box")["final_assembly"]
    assert bbox["max"][2] == pytest.approx(20)
    hole = plate_ledger.features[1].id
    props = query("get_face_properties", feature_1_id=hole, direction="cylindrical")
    assert props["surviving_face_in_assembly"]["radius"] == pytest.approx(5)
    axis = query("get_cylinder_axis", feature_1_id=hole)
    assert abs(axis["direction"]["z"]) == pytest.approx(1)
    assert query("get_edge_length", feature_1_id=hole, edge_index=0)["length"] > 0
    assert query("get_face_center", feature_1_id=plate_ledger.features[0].id, direction="top")["surviving_face_in_assembly"]["center"][2] == pytest.approx(20)


@pytest.mark.skipif(not geometry_bridge.REAL_GEOMETRY, reason="requires OCCT")
@pytest.mark.parametrize("offset, collides", [(100, False), (90, True)])
def test_inspection_distinguishes_contact_from_overlap(plate_ledger, offset, collides):
    doc = plate_ledger.model_dump(mode="json")
    base = doc["features"][0]
    doc["features"] = [base, dict(base, id="feat_second", placement={"origin": [offset, 0, 0]})]
    ledger = type(plate_ledger).model_validate(doc)
    result = registry.get("geometry_probe").execute(context={"ledgers": {ledger.project_id: ledger}},
        query_type="check_collision", feature_1_id=base["id"], feature_2_id="feat_second")
    assert "error" not in result, result
    assert result["collides"] is collides
    assert result["touching"] is not collides


@pytest.mark.skipif(not geometry_bridge.REAL_GEOMETRY, reason="requires OCCT")
def test_selected_assembly_receives_unselected_dependencies(plate_ledger):
    from cadenza_contracts.ledger import Ledger
    assembly = Ledger.model_validate({"assembly": {"components": [{
        "id": "comp_plate", "sub_assembly_id": plate_ledger.project_id,
        "placement": {"origin": [150, 0, 0]},
    }]}})
    context = {"ledgers": {assembly.project_id: assembly},
               "all_ledgers": {assembly.project_id: assembly, plate_ledger.project_id: plate_ledger}}
    tool = registry.get("geometry_probe")
    result = tool.execute(context=context, query_type="get_bounding_box")
    assert "error" not in result, result
    assert result["final_assembly"]["min"][0] == pytest.approx(100)
    result = tool.execute(context=context, query_type="get_face_center",
                          feature_1_id=plate_ledger.features[0].id, direction="top")
    assert "source part ledger_id" in result["error"]
    result = tool.execute(context=context, query_type="get_face_center", component_1_id="comp_plate",
                          feature_1_id=plate_ledger.features[0].id, direction="top")
    assert result["surviving_face_in_assembly"]["center"] == pytest.approx([150, 0, 20])


@pytest.mark.skipif(not geometry_bridge.REAL_GEOMETRY, reason="requires OCCT")
def test_async_tool_measures_instances_and_refreshes_dependency_cache(plate_ledger, monkeypatch):
    import asyncio
    from cadenza_backend import geometry_jobs as jobs
    from cadenza_contracts.ledger import Ledger
    assembly = Ledger.model_validate({"assembly": {"components": [
        {"id": "comp_a", "sub_assembly_id": plate_ledger.project_id},
        {"id": "comp_b", "sub_assembly_id": plate_ledger.project_id, "placement": {"origin": [150, 0, 0]}}]}})
    documents = {assembly.project_id: assembly, plate_ledger.project_id: plate_ledger}
    context = {"ledgers": {assembly.project_id: assembly}, "all_ledgers": documents}
    runner = jobs.GeometryJobRunner(1, 20)
    monkeypatch.setattr(jobs, "runner", runner)
    tool = registry.get("geometry_probe")

    async def scenario():
        result = await tool.execute_async(context=context, query_type="measure_distance",
            component_1_id="comp_a", component_2_id="comp_b",
            feature_1_id=plate_ledger.features[0].id, feature_2_id=plate_ledger.features[0].id)
        assert result["distance_mm"] == pytest.approx(50)
        assert "_timings" not in result
        for height in (20, 40):
            edited = plate_ledger.model_dump(mode="json")
            edited["features"][0]["parameters"]["height"] = height
            documents[plate_ledger.project_id] = Ledger.model_validate(edited)
            result = await tool.execute_async(context=context, query_type="get_face_center",
                component_1_id="comp_b", feature_1_id=plate_ledger.features[0].id, direction="top")
            assert result["surviving_face_in_assembly"]["center"] == pytest.approx([150, 0, height])
        result = await tool.execute_async(context=context, query_type="get_mass_properties", component_1_id="comp_missing")
        assert "Unknown leaf instance path" in result["error"]

    try:
        asyncio.run(scenario())
    finally:
        runner.close()


def test_part_query_rejects_instance_and_empty_feature_selectors(plate_ledger):
    tool = registry.get("geometry_probe")
    context = {"ledgers": {plate_ledger.project_id: plate_ledger}}
    for kwargs in ({"component_1_id": "comp_a"}, {"feature_1_id": ""}, {"component_1_id": ""}):
        assert "error" in tool.execute(context=context, query_type="get_bounding_box", **kwargs)
