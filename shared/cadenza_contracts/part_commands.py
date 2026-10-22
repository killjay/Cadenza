"""Bounded source creation and read-only viewing; no document format changes."""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from cadenza_contracts.design_commands import AddBox, AddCylinder, AddHole, AddRevolve, AddGear
from cadenza_contracts.assembly_commands import ComponentId, MAX_DEPTH

MAX_SOURCE_PARTS = 32
PrimitivePartOperation = Annotated[Union[AddBox, AddCylinder, AddHole], Field(discriminator="kind")]
PartOperation = Annotated[Union[PrimitivePartOperation, AddRevolve, AddGear], Field(discriminator="op")]
CreationVersion = Annotated[StrictInt, Field(ge=1, le=2)]
OccurrencePath = Annotated[list[ComponentId], Field(min_length=1, max_length=MAX_DEPTH)]
RigidMatrix = Annotated[list[Annotated[float, Field(strict=True, allow_inf_nan=False)]], Field(min_length=16, max_length=16)]


class PartMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str = Field(min_length=1, max_length=200)


class PartRequest(PartMessage):
    base_revision: int = Field(strict=True, ge=0)
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def trimmed_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a part name")
        return value


class PartCreate(PartRequest):
    creation_version: CreationVersion = 1
    type: Literal["design.part.create"] = "design.part.create"
    operations: list[PartOperation] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def additive_start(self):
        first = self.operations[0]
        if self.creation_version == 1 and any(not isinstance(op, (AddBox, AddCylinder, AddHole)) for op in self.operations):
            raise ValueError("Version 1 supports primitive creation only")
        if not isinstance(first, (AddBox, AddCylinder, AddRevolve, AddGear)) or first.operation != "add":
            raise ValueError("Start a new part with supported additive stock")
        return self


class PartRename(PartRequest):
    type: Literal["design.part.rename"] = "design.part.rename"
    ledger_id: str = Field(min_length=1, max_length=200)


class PartViewRequest(PartMessage):
    type: Literal["design.part.view"] = "design.part.view"
    base_revision: int = Field(strict=True, ge=0)
    ledger_id: str = Field(min_length=1, max_length=200)
    component_path: OccurrencePath | None = None


class PartViewReady(PartMessage):
    type: Literal["design.part.view_ready"] = "design.part.view_ready"
    session_id: str
    revision: int
    ledger_id: str
    view_id: str
    glb_url: str | None = None
    step_url: str | None = None
    empty: bool = False
    component_path: OccurrencePath | None = None
    # Row-major rigid 4x4: source-local mm/Z-up -> project mm/Z-up.
    source_to_project: RigidMatrix | None = None
    context_glb_url: str | None = None
    context_warning: str | None = None


class PartCapabilitiesRequest(PartMessage):
    version: CreationVersion = 1
    type: Literal["design.part.capabilities"] = "design.part.capabilities"


class _PartCapabilities(PartMessage):
    type: Literal["design.part.capabilities"] = "design.part.capabilities"
    session_id: str
    max_operations: Literal[32] = 32
    max_sources: Literal[32] = 32
    source_view: Literal[True] = True
    source_context: Literal[True] = True
    rename_source: Literal[True] = True


class PartCapabilitiesResponse(_PartCapabilities):
    version: Literal[1] = 1
    starter_kinds: tuple[Literal["box"], Literal["cylinder"]] = ("box", "cylinder")


class PartCapabilitiesV2Response(_PartCapabilities):
    version: Literal[2] = 2
    starter_kinds: tuple[Literal["box"], Literal["cylinder"], Literal["revolve"], Literal["gear"]] = ("box", "cylinder", "revolve", "gear")
