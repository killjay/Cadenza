"""Bounded source creation and read-only viewing; no document format changes."""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cadenza_contracts.design_commands import AddBox, AddCylinder, AddHole

MAX_SOURCE_PARTS = 32
PartOperation = Annotated[Union[AddBox, AddCylinder, AddHole], Field(discriminator="kind")]


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
            raise ValueError("Name the new part")
        return value


class PartCreate(PartRequest):
    type: Literal["design.part.create"] = "design.part.create"
    operations: list[PartOperation] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def additive_start(self):
        first = self.operations[0]
        if first.kind not in ("box", "cylinder") or first.operation != "add":
            raise ValueError("Start a new part with an additive box or cylinder")
        return self


class PartViewRequest(PartMessage):
    type: Literal["design.part.view"] = "design.part.view"
    base_revision: int = Field(strict=True, ge=0)
    ledger_id: str = Field(min_length=1, max_length=200)


class PartViewReady(PartMessage):
    type: Literal["design.part.view_ready"] = "design.part.view_ready"
    session_id: str
    revision: int
    ledger_id: str
    view_id: str
    glb_url: str | None = None
    step_url: str | None = None
    empty: bool = False


class PartCapabilitiesRequest(PartMessage):
    type: Literal["design.part.capabilities"] = "design.part.capabilities"


class PartCapabilitiesResponse(PartMessage):
    type: Literal["design.part.capabilities"] = "design.part.capabilities"
    session_id: str
    version: Literal[1] = 1
    starter_kinds: tuple[Literal["box"], Literal["cylinder"]] = ("box", "cylinder")
    max_operations: Literal[32] = 32
    max_sources: Literal[32] = 32
    source_view: Literal[True] = True
