"""Bounded spur-gear authoring; saved legacy gear definitions stay unchanged."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt

GearLength = Annotated[StrictFloat | StrictInt, Field(gt=1e-7, le=1e6, allow_inf_nan=False)]


class GearSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    module: Annotated[StrictFloat | StrictInt, Field(gt=1e-7, le=1000, allow_inf_nan=False)]
    teeth: Annotated[StrictInt, Field(ge=3, le=128)]
    height: GearLength
    pressure_angle_deg: Annotated[StrictFloat | StrictInt, Field(ge=5, le=45, allow_inf_nan=False)]
    shift: Annotated[StrictFloat | StrictInt, Field(ge=-1, le=1, allow_inf_nan=False)]


def gear_reason(feature: dict, source: dict | None = None) -> str | None:
    if feature.get("kind") != "gear" or feature.get("operation") != "add":
        return "These controls support additive external spur gears only."
    placement = feature.get("placement", {})
    if placement.get("relative_to") is not None or placement.get("rotation_deg", [0, 0, 0])[:2] != [0, 0]:
        return "Gear controls require absolute placement with rotation about source Z only."
    if source and sum(f["kind"] == "gear" and not f.get("suppressed", False) for f in source["features"]) > 1:
        return "These controls support one active gear per source."
    try:
        GearSettings.model_validate(feature.get("parameters", {}))
    except ValueError:
        return "This legacy gear is outside the supported settings; its geometry is preserved."
    return None


class GearCapabilitiesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["design.gear.capabilities"] = "design.gear.capabilities"
    message_id: str = Field(min_length=1, max_length=200)


class GearCapabilitiesResponse(GearCapabilitiesRequest):
    session_id: str
    version: Literal[1] = 1
    scope: Literal["source"] = "source"
    length_unit: Literal["mm"] = "mm"
    operations: tuple[Literal["add_gear"], Literal["update_gear"]] = ("add_gear", "update_gear")
    axis: Literal["+Z"] = "+Z"
    additive_only: Literal[True] = True
    teeth_min: Literal[3] = 3
    teeth_max: Literal[128] = 128
    max_active_gears: Literal[1] = 1
