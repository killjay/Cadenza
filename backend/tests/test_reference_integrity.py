import pytest

from cadenza_backend.contracts_bridge import CadenzaError, PatchOp
from cadenza_backend.ledger_store import apply_patch as backend_apply
from cadenza_contracts.patch import apply_patch as shared_apply
from cadenza_backend.design import build_design
from cadenza_backend.session import Session


@pytest.mark.parametrize("apply", [backend_apply, shared_apply])
def test_remove_then_edit_by_id_uses_remaining_feature(apply, plate_ledger):
    doc = plate_ledger.model_dump(mode="json")
    extra = dict(doc["features"][1], id="feat_extra")
    doc["features"].append(extra)
    ledger = type(plate_ledger).model_validate(doc)
    result = apply(ledger, [
        PatchOp(op="remove", path=f"/features/{doc['features'][1]['id']}"),
        PatchOp(op="replace", path="/features/feat_extra/parameters/diameter", value=7),
    ])
    result = result.ledger if hasattr(result, "ledger") else result
    assert result.features[1].model_dump()["parameters"]["diameter"] == 7
    assert len(ledger.features) == 3


@pytest.mark.parametrize("apply", [backend_apply, shared_apply])
def test_add_then_edit_id_is_resolved_without_partial_model_validation(apply, plate_ledger):
    extra = dict(plate_ledger.features[1].model_dump(mode="json"), id="feat_extra")
    result = apply(plate_ledger, [PatchOp(op="add", path="/features/-", value=extra),
        PatchOp(op="replace", path="/features/feat_extra/parameters/diameter", value=6)])
    result = result.ledger if hasattr(result, "ledger") else result
    assert result.features[-1].model_dump()["parameters"]["diameter"] == 6


@pytest.mark.parametrize("apply", [backend_apply, shared_apply])
def test_parent_replacement_cannot_rename_or_switch_units(apply, plate_ledger):
    before = plate_ledger.model_dump(mode="json")
    for path, value in [("/features/0", dict(before["features"][0], id="feat_renamed")),
                        ("/metadata", dict(before["metadata"], global_units="inch"))]:
        with pytest.raises(CadenzaError):
            apply(plate_ledger, [PatchOp(op="replace", path=path, value=value)])
        assert plate_ledger.model_dump(mode="json") == before


@pytest.mark.parametrize("apply", [backend_apply, shared_apply])
def test_missing_reference_and_cycle_are_rejected_atomically(apply, plate_ledger):
    before = plate_ledger.model_dump(mode="json")
    for ops in ([PatchOp(op="add", path="/features/0/placement/relative_to", value="feat_missing")],
                [PatchOp(op="add", path="/features/0/placement/relative_to", value=plate_ledger.features[1].id),
                 PatchOp(op="add", path="/features/1/placement/relative_to", value=plate_ledger.features[0].id)]):
        with pytest.raises(CadenzaError):
            apply(plate_ledger, ops)
        assert plate_ledger.model_dump(mode="json") == before


def test_external_cycle_is_rejected_before_kernel(monkeypatch):
    from cadenza_backend import geometry_bridge
    session = Session()
    from cadenza_contracts.ledger import Ledger
    child = Ledger()
    primary = session.store.ledger.model_dump(mode="json")
    secondary = child.model_dump(mode="json")
    primary["assembly"] = {"components": [{"id": "comp_child", "sub_assembly_id": child.project_id}]}
    secondary["assembly"] = {"components": [{"id": "comp_parent", "sub_assembly_id": session.primary_ledger_id}]}
    ledgers = session.stage({session.primary_ledger_id: Ledger.model_validate(primary), child.project_id: Ledger.model_validate(secondary)})
    def unexpected(*a, **kw):
        pytest.fail("invalid references reached the kernel")
    monkeypatch.setattr(geometry_bridge, "build", unexpected)
    outcome = build_design(ledgers, session.primary_ledger_id)
    assert not outcome.ok and "cycle" in outcome.detail
    assert session.revision == 0 and not session.history


@pytest.mark.parametrize("apply", [backend_apply, shared_apply])
def test_move_by_id_resolves_destination_after_removal(apply, plate_ledger):
    doc = plate_ledger.model_dump(mode="json")
    base = doc["features"][0]
    doc["features"] = [dict(base, id=f"feat_box_{n}") for n in range(3)]
    ledger = type(plate_ledger).model_validate(doc)
    result = apply(ledger, [PatchOp.model_validate({"op": "move", "path": "/features/feat_box_2", "from": "/features/feat_box_0"}),
        PatchOp(op="replace", path="/features/feat_box_2/parameters/height", value=8)])
    result = result.ledger if hasattr(result, "ledger") else result
    assert [f.id for f in result.features] == ["feat_box_1", "feat_box_0", "feat_box_2"]
    assert result.features[2].model_dump()["parameters"]["height"] == 8


@pytest.mark.parametrize("apply", [backend_apply, shared_apply])
def test_invalid_move_source_is_a_domain_error(apply, plate_ledger):
    with pytest.raises(CadenzaError):
        apply(plate_ledger, [PatchOp.model_validate({"op": "move", "path": "/features/0", "from": "/features/99"})])
