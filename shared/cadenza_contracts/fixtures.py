"""The milestone-1 golden fixtures. Import these instead of hand-typing JSON.

`MILESTONE_1_PROMPT` -> `plate_with_hole()` -> `THICKEN_PATCH` ->
`plate_with_hole(height=40)`. All three devs test against the same objects, so
"it works on my side" becomes checkable.
"""

from __future__ import annotations

from cadenza_contracts.ledger import (
    BoxFeature,
    BoxParameters,
    HoleFeature,
    HoleParameters,
    Ledger,
    Metadata,
    Placement,
)
from cadenza_contracts.patch import PatchOp

MILESTONE_1_PROMPT = "a 100x100x20 plate with a 10 mm hole in the middle"
MILESTONE_1_EDIT_PROMPT = "make this twice as thick"

PLATE_ID = "feat_main_body_0001"
HOLE_ID = "feat_centre_hole_0002"


def plate_with_hole(
    *,
    length: float = 100.0,
    width: float = 100.0,
    height: float = 20.0,
    hole_diameter: float = 10.0,
    revision: int = 1,
) -> Ledger:
    """The ledger the Draftsman must produce from `MILESTONE_1_PROMPT`.

    Box spans x,y in [-50, +50], z in [0, height]. The hole sits on the axis at
    x=y=0; its origin.z is irrelevant because `through=True` (see
    Placement's docstring) — which is exactly why thickening the plate is a
    single-field patch.
    """
    return Ledger(
        project_id="prj_milestone1",
        revision=revision,
        metadata=Metadata(name="Base Plate"),
        features=[
            BoxFeature(
                id=PLATE_ID,
                name="Main Body",
                operation="add",
                placement=Placement(origin=[0.0, 0.0, 0.0]),
                parameters=BoxParameters(length=length, width=width, height=height),
            ),
            HoleFeature(
                id=HOLE_ID,
                name="Centre Hole",
                operation="subtract",
                placement=Placement(origin=[0.0, 0.0, height]),
                parameters=HoleParameters(diameter=hole_diameter, through=True),
            ),
        ],
    )


THICKEN_PATCH: list[PatchOp] = [
    PatchOp(op="replace", path=f"/features/{PLATE_ID}/parameters/height", value=40.0)
]
"""What the Machinist must emit for "make this twice as thick" after the user
clicks the top face. Note the id-addressed pointer — see patch.normalize_pointer."""

DRAFTSMAN_PATCH: list[PatchOp] = [
    PatchOp(op="replace", path="/metadata/name", value="Base Plate"),
    PatchOp(
        op="add",
        path="/features/-",
        value=plate_with_hole().features[0].model_dump(mode="json"),
    ),
    PatchOp(
        op="add",
        path="/features/-",
        value=plate_with_hole().features[1].model_dump(mode="json"),
    ),
]
"""What the Draftsman must emit from an empty ledger for `MILESTONE_1_PROMPT`.
First build and edit go through the SAME patch pipeline — there is no second
code path for "initial generation"."""
