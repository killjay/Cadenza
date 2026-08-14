"""Ledger fixtures. Shapes follow cadenza_contracts.ledger (mm, Z-up, ordered
features, `feat_*` ids), written as plain dicts because that package is not yet
importable — see cadenza_geometry/contracts.py."""

from __future__ import annotations

import math
from typing import Any

import pytest

from cadenza_geometry import Build123dGeometryService


def feature(fid: str, kind: str, params: dict, origin=(0, 0, 0), rotation=(0, 0, 0), **kw) -> dict:
    f: dict[str, Any] = {
        "id": fid,
        "kind": kind,
        "parameters": params,
        "placement": {"origin": list(origin), "rotation_deg": list(rotation)},
    }
    f.update(kw)
    return f


def ledger(*features: dict, name: str = "Test Part", revision: int = 1) -> dict:
    return {
        "schema_version": 1,
        "project_id": "prj_test",
        "revision": revision,
        "metadata": {"name": name, "workspace_mode": "3D", "global_units": "mm"},
        "features": list(features),
    }


# The blueprint's own worked example: 100x100x20 plate, Ø10 hole through the middle.
def plate_with_hole(diameter: float = 10.0, height: float = 20.0, hole_xy=(0.0, 0.0)) -> dict:
    return ledger(
        feature(
            "feat_base_plate_9f2a",
            "box",
            {"length": 100, "width": 100, "height": height},
            operation="add",
            name="Main Body",
        ),
        feature(
            "feat_bore_1a2b",
            "hole",
            {"diameter": diameter, "through": True},
            origin=(hole_xy[0], hole_xy[1], height),
            operation="subtract",
            name="Centre Bore",
        ),
    )


# A 20-tooth module-2 spur gear on a Ø8 axle: pitch Ø40, outer Ø44, 10 thick.
def gear_with_bore(
    module: float = 2.0,
    teeth: int = 20,
    height: float = 10.0,
    bore: float = 8.0,
    **gear_params,
) -> dict:
    return ledger(
        feature(
            "feat_spur_gear_7c3d",
            "gear",
            {"module": module, "teeth": teeth, "height": height, **gear_params},
            operation="add",
            name="Spur Gear",
        ),
        feature(
            "feat_axle_bore_4e1f",
            "hole",
            {"diameter": bore, "through": True},
            origin=(0.0, 0.0, height),
            operation="subtract",
            name="Axle Bore",
        ),
    )


BOX_VOLUME = 100 * 100 * 20


def hole_volume(d: float, h: float) -> float:
    return math.pi * (d / 2) ** 2 * h


@pytest.fixture
def svc() -> Build123dGeometryService:
    return Build123dGeometryService()
