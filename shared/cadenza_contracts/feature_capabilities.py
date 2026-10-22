"""Capabilities and supported fields for design commands."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator, model_serializer

# Which fields are editable via SetDimension for supported kinds
DIMENSION_FIELDS = {
    "box": {"length", "width", "height"},
    "cylinder": {"diameter", "height"},
    "hole": {"diameter", "depth"},
    "extrude": {"amount"},
}

# Which feature kinds support SetOrigin
ORIGIN_AXES = {"box", "cylinder", "hole"}

# Which feature kinds can be added via AddFeature, and what operations they support
ADDABLE_KINDS = {
    "box": {"add", "subtract"},
    "cylinder": {"add", "subtract"},
    "hole": {"subtract"},
    "extrude": {"add", "subtract"},
}


def is_supported_edit(kind: str, parameter: str) -> bool:
    return parameter in DIMENSION_FIELDS.get(kind, set())


def is_origin_editable(kind: str) -> bool:
    return kind in ORIGIN_AXES


def is_addable(kind: str, operation: str) -> bool:
    return operation in ADDABLE_KINDS.get(kind, set())


class FeatureCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["box", "cylinder", "hole", "extrude"]
    dimensions: list[Literal["length", "width", "height", "diameter", "depth", "amount"]] = Field(max_length=6)
    origin_axes: list[Literal["x", "y", "z"]] = Field(max_length=3)
    create_operations: list[Literal["add", "subtract"]] = Field(max_length=2)
    suppress: StrictBool
    remove: StrictBool


class CapabilitiesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["design.capabilities"] = "design.capabilities"
    message_id: str = Field(min_length=1, max_length=200)
    version: Literal[1, 2] = 1

    @model_validator(mode="before")
    @classmethod
    def strict_version(cls, data):
        if isinstance(data, dict) and "version" in data and type(data["version"]) is not int:
            raise ValueError("Capability version must be an integer.")
        return data


class RevolveCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operations: tuple[Literal["add_revolve"], Literal["update_revolve"]] = ("add_revolve", "update_revolve")
    profile_plane: Literal["XY"] = "XY"
    axis: Literal["+Y"] = "+Y"
    angle_unit: Literal["deg"] = "deg"
    max_vertices: Literal[64] = 64


class CapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["design.capabilities"] = "design.capabilities"
    message_id: str
    session_id: str
    version: Literal[1, 2] = 1
    scope: Literal["source"] = "source"
    length_unit: Literal["mm"] = "mm"
    features: list[FeatureCapability] = Field(max_length=4)
    revolve: RevolveCapability | None = None

    @model_validator(mode="after")
    def version_fields(self):
        if (self.version == 2) != (self.revolve is not None):
            raise ValueError("Revolve capability requires version 2.")
        return self

    @model_serializer(mode="wrap")
    def legacy_shape(self, handler):
        data = handler(self)
        if self.version == 1:
            data.pop("revolve", None)
        return data


def feature_capabilities() -> list[FeatureCapability]:
    """Promoted static subset; document/build validation still decides validity."""
    return [FeatureCapability.model_validate(dict(kind=kind,
        dimensions=sorted(DIMENSION_FIELDS[kind]),
        origin_axes=["x", "y", "z"] if is_origin_editable(kind) else [],
        create_operations=sorted(operations), suppress=True, remove=True))
        for kind, operations in ADDABLE_KINDS.items()]


class PatternCapabilitiesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["design.pattern.capabilities"] = "design.pattern.capabilities"
    message_id: str = Field(min_length=1, max_length=200)


class PatternCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["design.pattern.capabilities"] = "design.pattern.capabilities"
    message_id: str
    session_id: str
    version: Literal[1] = 1
    scope: Literal["source"] = "source"
    operations: tuple[Literal["add_linear_pattern"], Literal["add_circular_pattern"],
                      Literal["update_linear_pattern"], Literal["update_circular_pattern"]] = (
                          "add_linear_pattern", "add_circular_pattern", "update_linear_pattern", "update_circular_pattern")
    seed_kinds: tuple[Literal["box"], Literal["cylinder"], Literal["hole"]] = ("box", "cylinder", "hole")
    directions: tuple[Literal["+x"], Literal["-x"], Literal["+y"], Literal["-y"], Literal["+z"], Literal["-z"]] = ("+x", "-x", "+y", "-y", "+z", "-z")
    count_min: Literal[2] = 2
    count_max: Literal[32] = 32
    copy_budget: Literal[128] = 128
    radius_mode: Literal["seed_position"] = "seed_position"
