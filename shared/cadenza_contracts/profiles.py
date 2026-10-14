"""Exact, bounded constraints for axis-aligned rectangle/circle profiles.

The supported equations are variable=fixed value and centre A=centre B.
Equality components give exact rank/remaining freedom without an iterative solver.
"""
from __future__ import annotations

import math
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

Number = Annotated[float, Field(allow_inf_nan=False, ge=-1e6, le=1e6)]
Size = Annotated[float, Field(allow_inf_nan=False, gt=1e-6, le=1e6)]
EntityId = Annotated[str, Field(pattern=r"^ent_[a-z0-9_]{1,48}$")]
ConstraintId = Annotated[str, Field(pattern=r"^con_[a-z0-9_]{1,48}$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileEntity(_Model):
    id: EntityId
    kind: Literal["rectangle", "circle"]
    operation: Literal["add", "subtract"] = "add"
    x: Number = 0
    y: Number = 0
    width: Size | None = None
    height: Size | None = None
    diameter: Size | None = None

    @model_validator(mode="after")
    def _dimensions(self) -> ProfileEntity:
        if self.kind == "rectangle":
            if self.width is None or self.height is None or self.diameter is not None:
                raise ValueError("Rectangle requires width/height and no diameter")
        elif self.diameter is None or self.width is not None or self.height is not None:
            raise ValueError("Circle requires diameter and no width/height")
        return self


class DimensionConstraint(_Model):
    id: ConstraintId
    kind: Literal["dimension"] = "dimension"
    entity_id: EntityId
    parameter: Literal["x", "y", "width", "height", "diameter"]
    value: Number


class CentreConstraint(_Model):
    id: ConstraintId
    kind: Literal["coincident"] = "coincident"
    entity_a: EntityId
    entity_b: EntityId


ProfileConstraint = Annotated[Union[DimensionConstraint, CentreConstraint], Field(discriminator="kind")]


class ProfileDefinition(_Model):
    entities: list[ProfileEntity] = Field(min_length=1, max_length=16)
    constraints: list[ProfileConstraint] = Field(default_factory=list, max_length=96)

    @model_validator(mode="after")
    def _identities(self) -> ProfileDefinition:
        if len({e.id for e in self.entities}) != len(self.entities):
            raise ValueError("Duplicate sketch entity ID")
        if len({c.id for c in self.constraints}) != len(self.constraints):
            raise ValueError("Duplicate sketch constraint ID")
        if self.entities[0].operation != "add" or any(e.operation != "subtract" or e.kind != "circle" for e in self.entities[1:]):
            raise ValueError("Profile needs one outer rectangle/circle followed by circular openings")
        return self


class ProfileSolution(_Model):
    status: Literal["underconstrained", "fully_constrained", "conflicting"]
    degrees_of_freedom: int
    conflicts: list[str]
    redundant: list[str]
    free_parameters: list[str]
    entities: list[ProfileEntity]


def solve_profile(definition: ProfileDefinition) -> ProfileSolution:
    variables = {f"{e.id}.{p}": float(getattr(e, p)) for e in definition.entities
                 for p in (("x", "y", "width", "height") if e.kind == "rectangle" else ("x", "y", "diameter"))}
    parent = {key: key for key in variables}
    redundant: list[str] = []
    links: list[tuple[str, str, str]] = []

    def root(key: str) -> str:
        if key not in parent:
            raise ValueError(f"Sketch constraint references missing variable {key}")
        while parent[key] != key:
            key = parent[key]
        return key

    for constraint in definition.constraints:
        if constraint.kind == "coincident":
            changed = False
            for axis in ("x", "y"):
                a, b = f"{constraint.entity_a}.{axis}", f"{constraint.entity_b}.{axis}"
                ra, rb = root(a), root(b)
                if ra != rb:
                    parent[rb] = ra
                    changed = True
                links.append((a, b, constraint.id))
            if not changed:
                redundant.append(constraint.id)

    fixed: dict[str, tuple[float, str]] = {}
    conflicts: set[str] = set()
    bad_roots: set[str] = set()
    for constraint in definition.constraints:
        if constraint.kind != "dimension":
            continue
        key = root(f"{constraint.entity_id}.{constraint.parameter}")
        if constraint.parameter not in ("x", "y") and constraint.value <= 1e-6:
            conflicts.add(constraint.id)
        if key in fixed:
            previous, cid = fixed[key]
            if not math.isclose(previous, constraint.value, rel_tol=0, abs_tol=1e-8):
                conflicts.update((cid, constraint.id))
                bad_roots.add(key)
            else:
                redundant.append(constraint.id)
        else:
            fixed[key] = constraint.value, constraint.id
    for a, _, cid in links:
        if root(a) in bad_roots:
            conflicts.add(cid)
    free = sorted({root(key) for key in variables} - fixed.keys())
    seeds = {}
    for key, value in variables.items():
        seeds.setdefault(root(key), value)
    solved = []
    for entity in definition.entities:
        data = entity.model_dump()
        for parameter in ("x", "y", "width", "height", "diameter"):
            key = f"{entity.id}.{parameter}"
            if key in variables:
                representative = root(key)
                value = fixed[representative][0] if representative in fixed else seeds[representative]
                # Preserve valid seeds for the diagnostic display of a failed solve.
                if parameter in ("x", "y") or value > 1e-6:
                    data[parameter] = value
        solved.append(ProfileEntity.model_validate(data))
    return ProfileSolution(status="conflicting" if conflicts else "underconstrained" if free else "fully_constrained",
        degrees_of_freedom=len(free), conflicts=sorted(conflicts), redundant=sorted(set(redundant)),
        free_parameters=free, entities=solved)


class ProfileParameters(ProfileDefinition):
    # Derived cache for UI diagnostics; always recomputed, never trusted on input.
    solution: ProfileSolution | None = None

    @model_validator(mode="before")
    @classmethod
    def _solve(cls, value: Any) -> Any:
        if isinstance(value, dict):
            definition = ProfileDefinition.model_validate({key: value[key] for key in ("entities", "constraints") if key in value})
            return {**value, "solution": solve_profile(definition)}
        return value


def validate_profile_region(solution: ProfileSolution) -> None:
    """Reject tangent, escaping or overlapping openings before boolean operations."""
    if solution.status != "fully_constrained":
        detail = ", ".join(solution.conflicts or solution.free_parameters)
        raise ValueError(f"Sketch is {solution.status}: {detail}")
    outer, *holes = solution.entities
    for index, hole in enumerate(holes):
        assert hole.diameter is not None
        radius = hole.diameter / 2
        dx, dy = abs(hole.x - outer.x), abs(hole.y - outer.y)
        if outer.kind == "rectangle":
            assert outer.width is not None and outer.height is not None
            inside = dx + radius < outer.width / 2 - 1e-7 and dy + radius < outer.height / 2 - 1e-7
        else:
            assert outer.diameter is not None
            inside = math.hypot(dx, dy) + radius < outer.diameter / 2 - 1e-7
        if not inside:
            raise ValueError(f"Opening {hole.id} must remain strictly inside the outer profile")
        for previous in holes[:index]:
            assert previous.diameter is not None
            if math.hypot(hole.x - previous.x, hole.y - previous.y) <= radius + previous.diameter / 2 + 1e-7:
                raise ValueError(f"Openings {hole.id} and {previous.id} overlap or touch")
