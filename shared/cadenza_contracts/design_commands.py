"""Typed design command operations for Stage B1."""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, model_validator
from cadenza_contracts.revolve import RevolveSettings
from cadenza_contracts.gear_authoring import GearSettings

StrictNum = Union[StrictFloat, StrictInt]
PositiveNum = Annotated[StrictNum, Field(gt=0, allow_inf_nan=False)]
FiniteNum = Annotated[StrictNum, Field(allow_inf_nan=False, ge=-1e9, le=1e9)]


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetDimension(_Op):
    op: Literal["set_dimension"] = "set_dimension"
    feature_id: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")
    parameter: Literal["length", "width", "height", "diameter", "depth", "amount"]
    value: PositiveNum


class SetOrigin(_Op):
    op: Literal["set_origin"] = "set_origin"
    feature_id: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")
    axis: Literal["x", "y", "z"]
    value: FiniteNum


class BoxCreationParams(_Op):
    length: PositiveNum
    width: PositiveNum
    height: PositiveNum


class CylinderCreationParams(_Op):
    diameter: PositiveNum
    height: PositiveNum


class HoleCreationParams(_Op):
    diameter: PositiveNum
    depth: PositiveNum | None = None
    through: StrictBool = False

    @model_validator(mode="after")
    def _depth_rules(self) -> "HoleCreationParams":
        if not self.through and self.depth is None:
            raise ValueError("a blind hole requires depth")
        return self


class ExtrudeCreationParams(_Op):
    amount: PositiveNum
    target_feature: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")


class CommandPlacement(_Op):
    """Finite source-local input; the saved-ledger Placement stays compatible."""
    origin: list[FiniteNum] = Field(default_factory=lambda: [0, 0, 0], min_length=3, max_length=3)
    rotation_deg: list[FiniteNum] = Field(default_factory=lambda: [0, 0, 0], min_length=3, max_length=3)
    relative_to: None = None


class AddFeatureBase(_Op):
    op: Literal["add_feature"] = "add_feature"
    name: str = Field(min_length=1, max_length=100)
    operation: Literal["add", "subtract"]
    placement: CommandPlacement


class AddRevolve(RevolveSettings):
    op: Literal["add_revolve"] = "add_revolve"
    name: str = Field(min_length=1, max_length=100)
    operation: Literal["add", "subtract"]
    placement: CommandPlacement


class UpdateRevolve(RevolveSettings):
    op: Literal["update_revolve"] = "update_revolve"
    feature_id: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")


class AddBox(AddFeatureBase):
    kind: Literal["box"] = "box"
    parameters: BoxCreationParams


class AddGear(GearSettings):
    op: Literal["add_gear"] = "add_gear"
    name: str = Field(min_length=1, max_length=100)
    operation: Literal["add"] = "add"
    placement: CommandPlacement

    @model_validator(mode="after")
    def source_axis(self):
        if self.placement.rotation_deg[:2] != [0, 0]:
            raise ValueError("Gear creation supports rotation about source Z only")
        return self


class UpdateGear(GearSettings):
    op: Literal["update_gear"] = "update_gear"
    feature_id: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")


class AddCylinder(AddFeatureBase):
    kind: Literal["cylinder"] = "cylinder"
    parameters: CylinderCreationParams


class AddHole(AddFeatureBase):
    kind: Literal["hole"] = "hole"
    parameters: HoleCreationParams


class AddExtrude(AddFeatureBase):
    kind: Literal["extrude"] = "extrude"
    parameters: ExtrudeCreationParams


AddFeature = Annotated[
    Union[AddBox, AddCylinder, AddHole, AddExtrude],
    Field(discriminator="kind"),
]


class SetSuppressed(_Op):
    op: Literal["set_suppressed"] = "set_suppressed"
    feature_id: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")
    suppressed: StrictBool


class RemoveFeature(_Op):
    op: Literal["remove_feature"] = "remove_feature"
    feature_id: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")


PatternCount = Annotated[StrictInt, Field(ge=2, le=32)]
PatternSpacing = Annotated[StrictNum, Field(gt=0, le=1e9, allow_inf_nan=False)]
PatternDirection = Literal["+x", "-x", "+y", "-y", "+z", "-z"]


class LinearSettings(_Op):
    count: PatternCount
    spacing: PatternSpacing
    direction: PatternDirection


class CircularSettings(_Op):
    count: PatternCount
    center_xy: list[FiniteNum] = Field(min_length=2, max_length=2)


class AddLinearPattern(LinearSettings):
    op: Literal["add_linear_pattern"] = "add_linear_pattern"
    name: str = Field(min_length=1, max_length=100)
    target_feature: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")


class AddCircularPattern(CircularSettings):
    op: Literal["add_circular_pattern"] = "add_circular_pattern"
    name: str = Field(min_length=1, max_length=100)
    target_feature: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")


class UpdateLinearPattern(LinearSettings):
    op: Literal["update_linear_pattern"] = "update_linear_pattern"
    feature_id: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")


class UpdateCircularPattern(CircularSettings):
    op: Literal["update_circular_pattern"] = "update_circular_pattern"
    feature_id: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")


CommandOperation = Annotated[
    Union[SetDimension, SetOrigin, AddFeature, SetSuppressed, RemoveFeature,
          AddLinearPattern, AddCircularPattern, UpdateLinearPattern, UpdateCircularPattern, AddRevolve, UpdateRevolve, AddGear, UpdateGear],
    Field(discriminator="op"),
]


class DesignCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["design.command"] = "design.command"
    message_id: str
    base_revision: int = Field(ge=0)
    ledger_id: str = Field(min_length=1, max_length=200)
    scope: Literal["source"] = "source"
    operations: list[CommandOperation] = Field(min_length=1, max_length=32)
