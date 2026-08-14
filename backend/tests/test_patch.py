"""RFC 6902 patching against the ledger — blueprint §2, spike 2.

The tests that matter here are the failure ones. Applying a valid patch is the
easy half; the reason this module exists is the guarantee that an *invalid*
patch changes nothing at all, because a half-applied patch would leave the
single source of truth in a state nobody wrote.
"""

from __future__ import annotations

import copy

import jsonpatch
import pytest

from cadenza_backend.contracts_bridge import (
    BoxFeature,
    BoxParameters,
    CadenzaError,
    ErrorCode,
    Ledger,
    PatchOp,
    Placement,
)
from cadenza_backend.ledger_store import LedgerStore, apply_patch
from tests.conftest import BASE_ID, HOLE_ID

# --------------------------------------------------------------------------- #
# The blueprint's own §2 shape
# --------------------------------------------------------------------------- #


def test_blueprint_history_tree_patch_mechanics():
    """The literal blueprint §2 example, applied to the blueprint's literal shape.

    The architect has since renamed `history_tree` -> `features` (and resolved
    the blueprint's own contradiction, where §2 showed an array but the patch
    example addressed `/objects/<id>/...`). This test is kept because the
    milestone was specified against the blueprint's wording: it pins the RFC 6902
    mechanics themselves, independent of the contract's field names.
    """
    blueprint_ledger = {
        "project_id": "proj_mvp_001",
        "metadata": {"workspace_mode": "3D", "global_units": "mm"},
        "history_tree": [
            {
                "id": "obj_base_01",
                "name": "Main Body",
                "type": "box",
                "boolean_operation": "add",
                "parameters": {"length": 100, "width": 100, "height": 20},
            }
        ],
    }
    patch = [{"op": "replace", "path": "/history_tree/0/parameters/height", "value": 40}]

    patched = jsonpatch.apply_patch(blueprint_ledger, patch, in_place=False)

    assert patched["history_tree"][0]["parameters"]["height"] == 40
    # The source document is untouched — in_place=False is the whole point.
    assert blueprint_ledger["history_tree"][0]["parameters"]["height"] == 20


# --------------------------------------------------------------------------- #
# Happy paths against the real contract
# --------------------------------------------------------------------------- #


def test_replace_by_index(plate_ledger: Ledger):
    ops = [PatchOp(op="replace", path="/features/0/parameters/height", value=40)]

    result = apply_patch(plate_ledger, ops)

    assert result.ledger.features[0].parameters.height == 40
    assert plate_ledger.features[0].parameters.height == 20  # original untouched


def test_replace_by_feature_id(plate_ledger: Ledger):
    """Id-addressed pointer sugar — the form agents are told to emit."""
    ops = [PatchOp(op="replace", path=f"/features/{BASE_ID}/parameters/height", value=40)]

    result = apply_patch(plate_ledger, ops)

    assert result.ledger.features[0].parameters.height == 40
    # normalize_patch rewrote the id to an index before jsonpatch saw it.
    assert result.ops[0]["path"] == "/features/0/parameters/height"


def test_multi_op_patch_applies_atomically(plate_ledger: Ledger):
    ops = [
        PatchOp(op="replace", path=f"/features/{BASE_ID}/parameters/height", value=40),
        PatchOp(op="replace", path=f"/features/{HOLE_ID}/parameters/diameter", value=12),
    ]

    result = apply_patch(plate_ledger, ops)

    assert result.ledger.features[0].parameters.height == 40
    assert result.ledger.features[1].parameters.diameter == 12


def test_append_feature(plate_ledger: Ledger):
    new_feature = BoxFeature(
        id="feat_boss_11aa",
        name="Boss",
        parameters=BoxParameters(length=20, width=20, height=5),
        placement=Placement(origin=[0.0, 0.0, 20.0]),
    )
    ops = [PatchOp(op="add", path="/features/-", value=new_feature.model_dump(mode="json"))]

    result = apply_patch(plate_ledger, ops)

    assert len(result.ledger.features) == 3
    assert result.ledger.features[-1].id == "feat_boss_11aa"


# --------------------------------------------------------------------------- #
# Failure paths — the reason this module exists
# --------------------------------------------------------------------------- #


def test_unknown_feature_id_fails_loudly(plate_ledger: Ledger):
    """An id that names nothing must raise, not silently no-op."""
    ops = [PatchOp(op="replace", path="/features/feat_does_not_exist_0000/parameters/height", value=40)]

    with pytest.raises(CadenzaError) as exc:
        apply_patch(plate_ledger, ops)

    assert exc.value.code is ErrorCode.PATCH_UNKNOWN_TARGET


def test_vanished_path_fails_loudly(plate_ledger: Ledger):
    """A path that no longer exists — the stale-patch case from the spike brief."""
    ops = [PatchOp(op="replace", path="/features/0/parameters/radius", value=5)]

    with pytest.raises(CadenzaError) as exc:
        apply_patch(plate_ledger, ops)

    assert exc.value.code is ErrorCode.PATCH_UNKNOWN_TARGET
    assert plate_ledger.features[0].parameters.height == 20


def test_out_of_range_index_fails_loudly(plate_ledger: Ledger):
    ops = [PatchOp(op="replace", path="/features/7/parameters/height", value=40)]

    with pytest.raises(CadenzaError) as exc:
        apply_patch(plate_ledger, ops)

    assert exc.value.code is ErrorCode.PATCH_UNKNOWN_TARGET


def test_failed_op_leaves_ledger_completely_unchanged(plate_ledger: Ledger):
    """The anti-corruption guarantee: op 1 is valid, op 2 is not, nothing applies.

    With `in_place=True` this is exactly where a ledger gets silently corrupted —
    the height would be 40 while the caller sees an exception and assumes nothing
    happened.
    """
    before = copy.deepcopy(plate_ledger.model_dump(mode="json"))
    ops = [
        PatchOp(op="replace", path=f"/features/{BASE_ID}/parameters/height", value=40),
        PatchOp(op="replace", path="/features/0/parameters/nonexistent", value=1),
    ]

    with pytest.raises(CadenzaError):
        apply_patch(plate_ledger, ops)

    assert plate_ledger.model_dump(mode="json") == before


def test_negative_dimension_rejected_by_revalidation(plate_ledger: Ledger):
    """jsonpatch will happily write -5; the Ledger model is what catches it."""
    ops = [PatchOp(op="replace", path=f"/features/{BASE_ID}/parameters/height", value=-5)]

    with pytest.raises(CadenzaError) as exc:
        apply_patch(plate_ledger, ops)

    assert exc.value.code is ErrorCode.LEDGER_INVALID
    assert plate_ledger.features[0].parameters.height == 20


def test_removing_required_parameter_rejected(plate_ledger: Ledger):
    ops = [PatchOp(op="remove", path=f"/features/{BASE_ID}/parameters/height")]

    with pytest.raises(CadenzaError) as exc:
        apply_patch(plate_ledger, ops)

    assert exc.value.code is ErrorCode.LEDGER_INVALID


def test_first_feature_must_stay_additive(plate_ledger: Ledger):
    """A ledger-level invariant, not a field-level one — removing the base plate
    would leave a subtractive hole as the first live feature."""
    ops = [PatchOp(op="remove", path=f"/features/{BASE_ID}")]

    with pytest.raises(CadenzaError) as exc:
        apply_patch(plate_ledger, ops)

    assert exc.value.code is ErrorCode.LEDGER_INVALID


@pytest.mark.parametrize("field,value", [("revision", 99), ("project_id", "prj_hijacked"), ("schema_version", 2)])
def test_server_owned_fields_are_rejected(plate_ledger: Ledger, field: str, value):
    ops = [PatchOp(op="replace", path=f"/{field}", value=value)]

    with pytest.raises(CadenzaError) as exc:
        apply_patch(plate_ledger, ops)

    # The contract has a dedicated code for this: a patch aimed at a
    # server-owned path is forbidden, not malformed.
    assert exc.value.code is ErrorCode.PATCH_FORBIDDEN_PATH


def test_empty_patch_rejected(plate_ledger: Ledger):
    with pytest.raises(CadenzaError) as exc:
        apply_patch(plate_ledger, [])

    assert exc.value.code is ErrorCode.PATCH_INVALID


# --------------------------------------------------------------------------- #
# Store bookkeeping
# --------------------------------------------------------------------------- #


def test_apply_does_not_bump_revision_but_build_does(plate_ledger: Ledger):
    """Contract invariant: a revision that exists has been BUILT."""
    store = LedgerStore(ledger=plate_ledger)
    assert store.ledger.revision == 0

    store.apply([PatchOp(op="replace", path=f"/features/{BASE_ID}/parameters/height", value=40)])
    assert store.ledger.revision == 0, "patching alone must not imply a built revision"

    assert store.mark_built() == 1
    assert store.ledger.revision == 1


def test_failed_apply_leaves_store_untouched(plate_ledger: Ledger):
    store = LedgerStore(ledger=plate_ledger)

    with pytest.raises(CadenzaError):
        store.apply([PatchOp(op="replace", path="/features/0/parameters/bogus", value=1)])

    assert store.ledger.features[0].parameters.height == 20
    assert store.history == [], "a failed patch must not push an undo entry"


def test_rollback_restores_previous_ledger(plate_ledger: Ledger):
    store = LedgerStore(ledger=plate_ledger)
    store.apply([PatchOp(op="replace", path=f"/features/{BASE_ID}/parameters/height", value=40)])

    store.rollback()

    assert store.ledger.features[0].parameters.height == 20
