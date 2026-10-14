"""Bounded assembly authoring; existing ledger and transaction formats remain authoritative."""
from typing import Annotated, Literal, Union
from math import hypot

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator, model_serializer, field_validator

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
ComponentId = Annotated[str, Field(pattern=r"^comp_[a-z0-9_]{1,60}$")]
Vector = Annotated[list[float], Field(min_length=3, max_length=3)]
OPERATIONS = ("initialize_assembly", "add_instance", "duplicate_instance", "set_instance_placement",
              "rename_instance", "remove_instance")
MAX_COMPONENTS, MAX_LEAVES, MAX_DEPTH = 32, 128, 8
E2_OPERATIONS = (*OPERATIONS, "add_rigid_joint", "remove_joint", "create_subassembly")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @field_validator("version", mode="before", check_fields=False)
    @classmethod
    def integer_version(cls, value):
        # Literal[1, 2] alone accepts True as 1, even under strict model validation.
        if type(value) is not int:
            raise ValueError("Protocol version must be an integer.")
        return value


class AbsolutePlacement(StrictModel):
    origin: Vector
    rotation_deg: Vector


class InitializeAssembly(StrictModel):
    op: Literal["initialize_assembly"]
    name: Name


class AddInstance(StrictModel):
    op: Literal["add_instance"]
    source_ledger_id: str = Field(min_length=1, max_length=200)
    name: Name
    placement: AbsolutePlacement


class DuplicateInstance(StrictModel):
    op: Literal["duplicate_instance"]
    component_id: ComponentId
    name: Name
    placement: AbsolutePlacement


class SetInstancePlacement(StrictModel):
    op: Literal["set_instance_placement"]
    component_id: ComponentId
    placement: AbsolutePlacement


class RenameInstance(StrictModel):
    op: Literal["rename_instance"]
    component_id: ComponentId
    name: Name


class RemoveInstance(StrictModel):
    op: Literal["remove_instance"]
    component_id: ComponentId


class JointAnchor(StrictModel):
    point: Vector
    normal: Vector | None = None

    @model_validator(mode="after")
    def usable_normal(self):
        if self.normal is not None and (not 1e-12 <= hypot(*self.normal) < float("inf")):
            raise ValueError("Use a finite, nonzero normal.")
        return self


class AddRigidJoint(StrictModel):
    op: Literal["add_rigid_joint"]
    component_a: ComponentId
    component_b: ComponentId
    target_a: JointAnchor
    target_b: JointAnchor

    @model_validator(mode="after")
    def paired_normals(self):
        if (self.target_a.normal is None) != (self.target_b.normal is None):
            raise ValueError("Supply both normals, or neither.")
        return self


class RemoveJoint(StrictModel):
    op: Literal["remove_joint"]
    joint_id: str = Field(pattern=r"^joint_[a-z0-9_]{1,60}$")


class SubassemblyChild(StrictModel):
    source_ledger_id: str = Field(min_length=1, max_length=200)
    name: Name
    placement: AbsolutePlacement


class CreateSubassembly(StrictModel):
    op: Literal["create_subassembly"]
    name: Name
    instance_name: Name
    placement: AbsolutePlacement
    children: list[SubassemblyChild] = Field(min_length=1, max_length=16)


AssemblyOperation = Annotated[Union[InitializeAssembly, AddInstance, DuplicateInstance,
    SetInstancePlacement, RenameInstance, RemoveInstance, AddRigidJoint, RemoveJoint,
    CreateSubassembly], Field(discriminator="op")]


class AssemblyCommand(StrictModel):
    type: Literal["design.assembly.command"] = "design.assembly.command"
    message_id: str = Field(min_length=1, max_length=200)
    base_revision: int = Field(ge=0)
    ledger_id: str = Field(min_length=1, max_length=200)
    scope: Literal["assembly_definition"]
    owner_path: list[ComponentId] = Field(default_factory=list, max_length=8)
    operations: list[AssemblyOperation] = Field(min_length=1, max_length=16)


class DefinitionTarget(StrictModel):
    kind: Literal["definition"]


class InstanceTarget(StrictModel):
    kind: Literal["instance"]
    component_id: ComponentId
    detach_joint_ids: list[Annotated[str, Field(pattern=r"^joint_[a-z0-9_]{1,60}$")]] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def unique_detach(self):
        if len(set(self.detach_joint_ids)) != len(self.detach_joint_ids):
            raise ValueError("Choose each attachment once.")
        return self


class AttachmentTarget(StrictModel):
    kind: Literal["attachment"]
    component_a: ComponentId
    component_b: ComponentId
    joint_id: str | None = Field(default=None, pattern=r"^joint_[a-z0-9_]{1,60}$")

    @model_validator(mode="after")
    def distinct_pair(self):
        if self.component_a == self.component_b:
            raise ValueError("Choose two different instances.")
        return self


AssemblyPromptTarget = Annotated[Union[DefinitionTarget, InstanceTarget, AttachmentTarget], Field(discriminator="kind")]


class AssemblyPromptCapabilitiesRequest(StrictModel):
    type: Literal["design.assembly.prompt.capabilities"] = "design.assembly.prompt.capabilities"
    message_id: str = Field(min_length=1, max_length=200)
    base_revision: int = Field(ge=0)
    ledger_id: str = Field(min_length=1, max_length=200)
    owner_path: list[ComponentId] = Field(max_length=8)


class AssemblyPromptCapabilitiesResponse(StrictModel):
    type: Literal["design.assembly.prompt.capabilities"] = "design.assembly.prompt.capabilities"
    message_id: str
    session_id: str
    revision: int
    ledger_id: str
    owner_path: list[ComponentId] = Field(max_length=8)
    version: Literal[1] = 1
    target_modes: list[Literal["definition", "instance", "attachment"]] = Field(default_factory=lambda: ["definition", "instance", "attachment"])
    max_operations: Literal[16] = 16
    available: bool
    vision_available: bool
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def supported_modes(self):
        if self.target_modes != ["definition", "instance", "attachment"]:
            raise ValueError("Unsupported target modes")
        return self


class AssemblyCapabilitiesRequest(StrictModel):
    type: Literal["design.assembly.capabilities"] = "design.assembly.capabilities"
    message_id: str = Field(min_length=1, max_length=200)
    version: Literal[1, 2] = 1
    ledger_id: str | None = Field(default=None, min_length=1, max_length=200)
    owner_path: list[ComponentId] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def version_scope(self):
        if self.version == 1 and (self.ledger_id is not None or self.owner_path):
            raise ValueError("Version 1 discovery supports only the primary assembly.")
        if self.version == 2 and self.ledger_id is None:
            raise ValueError("Version 2 discovery requires an explicit owner ledger.")
        return self

    @model_serializer(mode="wrap")
    def versioned_shape(self, handler):
        result = handler(self)
        if self.version == 1:
            for key in ("version", "ledger_id", "owner_path"):
                result.pop(key, None)
        return result


class AssemblyCapabilitiesResponse(StrictModel):
    type: Literal["design.assembly.capabilities"] = "design.assembly.capabilities"
    message_id: str
    session_id: str
    revision: int
    ledger_id: str
    version: Literal[1, 2] = 1
    owner_path: list[ComponentId] = Field(default_factory=list, max_length=8)
    can_initialize: bool
    can_edit: bool
    reason: str | None
    operations: list[str] = Field(default_factory=lambda: list(OPERATIONS))
    max_operations: Literal[16] = 16
    max_components: Literal[32] = MAX_COMPONENTS
    max_leaves: Literal[128] = MAX_LEAVES
    max_depth: Literal[8] = MAX_DEPTH
    max_subassemblies: Literal[8] = 8
    max_initial_children: Literal[16] = 16

    @model_validator(mode="after")
    def version_scope(self):
        expected = OPERATIONS if self.version == 1 else E2_OPERATIONS
        if self.operations != list(expected) or (self.version == 1 and self.owner_path):
            raise ValueError("Capability operations and owner context must match the declared version.")
        if self.version == 2 and "owner_path" not in self.model_fields_set:
            raise ValueError("Version 2 discovery requires the resolved owner path.")
        return self

    @model_serializer(mode="wrap")
    def versioned_shape(self, handler):
        result = handler(self)
        if self.version == 1:
            for key in ("owner_path", "max_subassemblies", "max_initial_children"):
                result.pop(key, None)
        return result
