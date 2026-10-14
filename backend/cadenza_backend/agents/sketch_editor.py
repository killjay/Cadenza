"""Bounded semantic sketch intent; the model never supplies executable patches."""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cadenza_contracts.parameters import ParameterName, Finite
from cadenza_contracts.profiles import Number, Size
from cadenza_contracts.sketch_draft import Alias, EntitySeed, Variable
from cadenza_backend.agents.core import AgentCard


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


Target = Annotated[str, Field(pattern=r"^(ent_[a-z0-9_]{1,48}|\$[a-z][a-z0-9_]{0,47})$")]


class Create(_Model):
    kind: Literal["create"]
    key: Alias
    entity: EntitySeed


class Dimension(_Model):
    kind: Literal["dimension"]
    entity: Target
    parameter: Variable
    value: Number


class Driver(_Model):
    kind: Literal["driver"]
    parameter: ParameterName
    value: Finite


class Centre(_Model):
    kind: Literal["centre"]
    entity: Target
    other: Target


class Remove(_Model):
    kind: Literal["remove"]
    entity: Target


class Margin(_Model):
    kind: Literal["margin"]
    entity: Target
    edge: Literal["left", "right", "top", "bottom"]
    distance: Size
    driver_name: ParameterName


class Extrusion(_Model):
    kind: Literal["extrusion"]
    amount: Size


IntentAction = Annotated[Union[Create, Dimension, Driver, Centre, Remove, Margin, Extrusion], Field(discriminator="kind")]


class SketchIntent(_Model):
    summary: str = Field(min_length=1, max_length=500)
    question: str | None = Field(default=None, min_length=1, max_length=500)
    assumptions: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=8)
    actions: list[IntentAction] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _answer_or_question(self):
        if bool(self.actions) == bool(self.question):
            raise ValueError("Return either actions or one clarification question")
        return self


CARD = AgentCard(name="sketch_editor", output_schema=SketchIntent.model_json_schema(), system_prompt="""
Interpret one text request for the supplied current sketch draft. Return semantic actions OR
one actionable clarification question. No patches, code, tools, accepted-design edits or invented IDs.
All distances are mm in the sketch's local plane (x right, y up). Angles/plane edits, images,
assemblies, arbitrary curves, fillets and rim-clearance recipes are unsupported: explain this in a question.
Use exact entity IDs from context; selection is only a hint. Named targets override selection.
Ask when 'it', 'bigger', counts, dimensions, positions or centre-versus-rim distances are ambiguous.
Never claim the request is fulfilled: the user must review the interpretation and actual changes.
create adds a fully dimensioned outer rectangle/circle or subtractive circular opening. Use a unique
key and refer to that new entity as $key in later actions. Only one outer region, at most 15 openings,
8 semantic actions and 32 compiled commands. Ask to split requests exceeding those limits.
For an explicitly centred new outer sketch use x=y=0; disclose this origin assumption. Specify sizes
and opening locations from the request, or ask. For a later centre/margin action, initial x/y may be
zero placeholders, disclosed in assumptions; centre/margin establishes the final position.
dimension changes a size or local x/y through its existing dimension/driver; never remove a binding.
driver edits an existing literal named driver. Derived dimensions require their driving parameters.
centre explicitly replaces the target's unbound x/y dimensions with shared centre to 'other';
bound position dimensions cannot be detached. remove only deletes unbound openings and their
referencing constraints, never an outer profile or bound entity. Report destructive intent clearly.
margin explicitly binds an opening centre to one rectangle edge, centring the other coordinate;
distance means centre-to-edge, NOT rim clearance. Use a NEW unique driver_name. Existing bound
locations/shared centres cannot be overwritten: ask the user to edit the current driver or repair
constraints manually. Reuse existing outer literal drivers. Compound outer bindings are unsupported
by the margin recipe. Preserve all other entities, bindings, named parameters and plane placement.
extrusion only sets the new draft's straight additive extrusion amount. Existing extrusion editing
must happen after finishing this sketch. Do not infer unsupported changes or silently drop requests.
""")
