"""Spike 3, end to end, against a REAL model. Costs money; not part of pytest.

    .venv/bin/python scripts/machinist_smoke.py

Builds the milestone-1 part, simulates a click on the plate, sends
"make this twice as thick", and applies whatever patch comes back.
"""

from __future__ import annotations

import asyncio
import json

from cadenza_backend.agents.machinist import run_machinist
from cadenza_backend.config import get_settings
from cadenza_backend.contracts_bridge import (
    BoxFeature,
    BoxParameters,
    HoleFeature,
    HoleParameters,
    Ledger,
    Metadata,
    Placement,
)
from cadenza_backend.ledger_store import LedgerStore
from cadenza_backend.spatial_stub import resolve_target

BASE_ID = "feat_base_plate_9f2a"


def milestone_ledger() -> Ledger:
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
                id="feat_centre_hole_3c71",
                name="Centre Hole",
                parameters=HoleParameters(diameter=10, through=True),
                placement=Placement(origin=[0.0, 0.0, 20.0]),
            ),
        ],
    )


async def main() -> None:
    settings = get_settings()
    print(f"machinist routed to: {settings.stage_model('machinist')}  effort={settings.machinist_effort}")

    store = LedgerStore(ledger=milestone_ledger())
    click = [10.5, 0.0, 20.0]  # the blueprint §5 example coordinate

    resolution = resolve_target(store.ledger, click)
    print(f"\nclick {click} resolved -> {resolution.feature_id}")
    print(f"context: {resolution.semantic_context}\n")

    output = await run_machinist(
        store.ledger, "Make this twice as thick.", resolution.semantic_context
    )

    print("summary:", output.summary)
    print("patch:  ", json.dumps([op.to_jsonpatch() for op in output.patch], indent=2))
    if output.assumptions:
        print("assumptions:", json.dumps(output.assumptions, indent=2))
    if output.needs_clarification:
        print("needs_clarification:", output.needs_clarification)
        return

    applied = store.apply(output.patch)
    height = applied.ledger.features[0].parameters.height
    print(f"\nheight 20 -> {height}")
    print("RESULT:", "PASS" if height == 40 else f"UNEXPECTED (got {height})")


if __name__ == "__main__":
    asyncio.run(main())
