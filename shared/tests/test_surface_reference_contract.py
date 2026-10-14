import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cadenza_contracts.fixtures import plate_with_hole
from cadenza_contracts.ledger import Ledger
from cadenza_contracts.surface_reference import SurfaceReference


def fixture():
    return json.loads((Path(__file__).parents[1] / "contract_fixtures/surface_reference.json").read_text())


def test_reference_contract_round_trip_and_old_ledger_default():
    reference = SurfaceReference.model_validate(fixture())
    assert reference.model_dump(mode="json", exclude={"profile_id", "entity_id"}) == fixture()
    assert reference.profile_id is None and reference.entity_id is None
    doc = plate_with_hole().model_dump(mode="json")
    del doc["surface_references"]
    assert Ledger.model_validate(doc).surface_references == {}
    doc["surface_references"] = {"ref_plate_top": fixture()}
    ledger = Ledger.model_validate(doc)
    assert Ledger.model_validate_json(ledger.model_dump_json()) == ledger
    # A saved annotation remains valid data after deletion; geometry resolution
    # reports its status instead of making the whole project impossible to open.
    doc["features"] = []
    assert Ledger.model_validate(doc).surface_references["ref_plate_top"] == reference


@pytest.mark.parametrize("changes", [{"version": 2}, {"role": "face_7"}, {"role": "bore"},
    {"component_path": ["invalid"]}, {"operation": "subtract"}, {"index": 7},
    {"source_ledger_id": "another_ledger"}, {"component_path": ["comp_a"] * 33}])
def test_invalid_surface_identity_is_rejected(changes):
    with pytest.raises(ValidationError):
        SurfaceReference.model_validate({**fixture(), **changes})


@pytest.mark.parametrize("references", [{"bad/key": fixture()}, {f"ref_{i}": fixture() for i in range(65)}])
def test_saved_reference_names_and_count_are_bounded(references):
    with pytest.raises(ValidationError):
        Ledger.model_validate({"surface_references": references})
