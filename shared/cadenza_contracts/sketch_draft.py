"""Transient sketch commands; unfinished work is not an accepted profile."""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cadenza_contracts.parameters import Relationships, ParameterName, Finite, Expression
from cadenza_contracts.profiles import (
    ProfileEntity, ProfileConstraint, ProfileDefinition, ProfileSolution,
    EntityId, ConstraintId, Number, Size,
)

FeatureId = Annotated[str, Field(pattern=r"^feat_[a-z0-9_]{1,60}$")]
Variable = Literal["x", "y", "width", "height", "diameter"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


Alias = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")]


class EntityAlias(_Model):
    entity_alias: Alias


class ConstraintAlias(_Model):
    constraint_alias: Alias


EntityReference = EntityId | EntityAlias
ConstraintReference = ConstraintId | ConstraintAlias


class EntitySeed(_Model):
    kind: Literal["rectangle", "circle"]
    operation: Literal["add", "subtract"] = "add"
    x: Number = 0
    y: Number = 0
    width: Size | None = None
    height: Size | None = None
    diameter: Size | None = None

    @model_validator(mode="after")
    def _shape(self):
        ProfileEntity(id="ent_seed", **self.model_dump())
        return self


class AddEntity(_Model):
    op: Literal["add_entity"]
    entity: EntitySeed
    alias: Alias | None = None


class RemoveEntity(_Model):
    op: Literal["remove_entity"]
    entity_id: EntityReference


class SetSeed(_Model):
    op: Literal["set_seed"]
    entity_id: EntityReference
    parameter: Variable
    value: Number


class AddDimension(_Model):
    op: Literal["add_dimension"]
    entity_id: EntityReference
    parameter: Variable
    value: Number
    alias: Alias | None = None


class AddCentre(_Model):
    op: Literal["add_centre"]
    entity_a: EntityReference
    entity_b: EntityReference


class RemoveConstraint(_Model):
    op: Literal["remove_constraint"]
    constraint_id: ConstraintReference


class SetDimension(_Model):
    op: Literal["set_dimension"]
    constraint_id: ConstraintReference
    value: Number


class SetDriver(_Model):
    op: Literal["set_driver"]
    parameter: ParameterName
    value: Finite


class SetExtrusion(_Model):
    op: Literal["set_extrusion"]
    amount: Size


class AddDriver(_Model):
    op: Literal["add_driver"]
    parameter: ParameterName
    value: Finite
    unit: Literal["mm", "scalar"] = "mm"


class BindDimension(_Model):
    op: Literal["bind_dimension"]
    constraint_id: ConstraintReference
    expression: Expression


DraftOperation = Annotated[Union[AddEntity, RemoveEntity, SetSeed, AddDimension, AddCentre,
    RemoveConstraint, SetDimension, SetDriver, SetExtrusion, AddDriver, BindDimension], Field(discriminator="op")]


class DraftDefinition(_Model):
    entities: list[ProfileEntity] = Field(default_factory=list, max_length=16)
    constraints: list[ProfileConstraint] = Field(default_factory=list, max_length=96)

    @model_validator(mode="after")
    def _structure(self):
        if self.entities:
            ProfileDefinition.model_validate(self.model_dump())
        elif self.constraints:
            raise ValueError("An empty draft cannot have constraints")
        return self


class SketchDraft(_Model):
    id: str
    base_revision: int = Field(ge=0)
    revision: int = Field(ge=0)
    ledger_id: str
    profile_id: FeatureId
    is_new: bool
    definition: DraftDefinition
    relationships: Relationships
    solution: ProfileSolution | None = None
    diagnostics: list[str] = Field(default_factory=list)
    extrusion_amount: Size | None = None
    extrusion_id: FeatureId | None = None


class DraftStart(_Model):
    type: Literal["sketch.draft_start"] = "sketch.draft_start"
    message_id: str
    base_revision: int = Field(ge=0)
    profile_id: FeatureId | None = None


class _DraftRequest(_Model):
    message_id: str
    base_revision: int = Field(ge=0)
    draft_id: str = Field(min_length=1, max_length=100)
    draft_revision: int = Field(ge=0)


class DraftUpdate(_DraftRequest):
    type: Literal["sketch.draft_update"] = "sketch.draft_update"
    operations: list[DraftOperation] = Field(min_length=1, max_length=32)


class DraftAction(_DraftRequest):
    type: Literal["sketch.draft_action"] = "sketch.draft_action"
    action: Literal["finish", "discard"]


class DraftState(_Model):
    type: Literal["sketch.draft_state"] = "sketch.draft_state"
    message_id: str
    session_id: str
    draft: SketchDraft


class DraftClosed(_Model):
    type: Literal["sketch.draft_closed"] = "sketch.draft_closed"
    message_id: str
    session_id: str
    draft_id: str


class DraftPrompt(_DraftRequest):
    type: Literal["sketch.prompt"] = "sketch.prompt"
    prompt: str = Field(min_length=1, max_length=4000)
    selected_entity_id: EntityId | None = None
    reply_to: str | None = Field(default=None, min_length=1, max_length=100)


class DraftProposalDecision(_DraftRequest):
    type: Literal["sketch.proposal_decision"] = "sketch.proposal_decision"
    proposal_id: str = Field(min_length=1, max_length=100)
    selected_entity_id: EntityId | None = None
    action: Literal["update", "cancel"]


class DraftChange(_Model):
    label: str
    before: str
    after: str


class DraftProposal(_Model):
    type: Literal["sketch.proposal"] = "sketch.proposal"
    message_id: str
    session_id: str
    draft_id: str
    draft_revision: int
    base_revision: int
    selected_entity_id: str | None = None
    proposal_id: str
    summary: str
    question: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    interpretation: list[str] = Field(default_factory=list)
    changes: list[DraftChange] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
