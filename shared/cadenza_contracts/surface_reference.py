"""Serializable primitive-surface identity; no coordinates or kernel ordinals."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SURFACE_ROLES = {
    "box": {"top_face", "bottom_face", "side_x_pos", "side_x_neg", "side_y_pos", "side_y_neg"},
    "cylinder": {"top_face", "bottom_face", "lateral_face"},
    "hole": {"bore"},
    "extrude": {"top_face", "bottom_face", "side_x_pos", "side_x_neg", "side_y_pos", "side_y_neg", "lateral_face", "bore"},
}
ReferenceId = Annotated[str, Field(pattern=r"^ref_[a-z0-9_]{1,60}$")]
ComponentId = Annotated[str, Field(pattern=r"^comp_[a-z0-9_]{1,60}$")]


class SavedSurfaceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ledger_id: str = Field(min_length=1, max_length=200)
    reference_id: ReferenceId


class SurfaceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    ledger_id: str = Field(min_length=1, max_length=200)
    component_path: tuple[ComponentId, ...] = Field(default=(), max_length=32)
    source_ledger_id: str = Field(min_length=1, max_length=200)
    source_part_id: str | None = Field(default=None, pattern=r"^part_[a-z0-9_]{1,60}$")
    feature_id: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")
    feature_kind: Literal["box", "cylinder", "hole", "extrude"]
    profile_id: str | None = Field(default=None, pattern=r"^feat_[a-z0-9_]{1,60}$")
    entity_id: str | None = Field(default=None, pattern=r"^ent_[a-z0-9_]{1,48}$")
    operation: Literal["add", "subtract"]
    role: Literal["top_face", "bottom_face", "side_x_pos", "side_x_neg", "side_y_pos", "side_y_neg", "lateral_face", "bore"]

    @model_validator(mode="after")
    def supported_identity(self) -> SurfaceReference:
        if self.feature_kind == "extrude":
            if self.profile_id is None or (self.entity_id is None) != (self.role in {"top_face", "bottom_face"}):
                raise ValueError("Extrusion references require a profile and wall entity; caps must not specify an entity")
        elif self.profile_id is not None or self.entity_id is not None:
            raise ValueError("Primitive references cannot specify sketch identity")
        if self.role not in SURFACE_ROLES[self.feature_kind]:
            raise ValueError("Surface role is not supported for this feature kind")
        if self.operation != ("subtract" if self.feature_kind == "hole" else "add"):
            raise ValueError("References support additive boxes/cylinders/extrusions and subtractive holes")
        if not self.component_path and (self.source_ledger_id != self.ledger_id or self.source_part_id is not None):
            raise ValueError("A part-local reference must identify its own ledger with no source part")
        return self
