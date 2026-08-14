"""Shared fixtures. Deliberately no model calls anywhere in the test suite."""

from __future__ import annotations

import pytest

from cadenza_backend.contracts_bridge import (
    BoxFeature,
    BoxParameters,
    HoleFeature,
    HoleParameters,
    Ledger,
    Metadata,
    Placement,
)

BASE_ID = "feat_base_plate_9f2a"
HOLE_ID = "feat_centre_hole_3c71"


@pytest.fixture
def plate_ledger() -> Ledger:
    """The milestone-1 part: a 100 x 100 x 20 plate with a 10 mm hole in the middle."""
    return Ledger(
        metadata=Metadata(name="Base Plate"),
        features=[
            BoxFeature(
                id=BASE_ID,
                name="Base Plate",
                parameters=BoxParameters(length=100, width=100, height=20),
                placement=Placement(origin=[0.0, 0.0, 0.0]),
            ),
            HoleFeature(
                id=HOLE_ID,
                name="Centre Hole",
                parameters=HoleParameters(diameter=10, through=True),
                # Top face of the plate; holes cut along -Z from their origin.
                placement=Placement(origin=[0.0, 0.0, 20.0]),
            ),
        ],
    )
