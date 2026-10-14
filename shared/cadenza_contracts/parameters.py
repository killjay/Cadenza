"""Bounded, unit-checked arithmetic for persistent part-local relationships."""
from __future__ import annotations

import ast
import copy
import math
from typing import Annotated, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ParameterName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")]
Expression = Annotated[str, Field(min_length=1, max_length=256)]
Finite = Annotated[float, Field(allow_inf_nan=False, ge=-1e9, le=1e9)]
DIMENSIONS = {"box": {"length", "width", "height"}, "cylinder": {"diameter", "height"},
              "hole": {"diameter", "depth"}, "extrude": {"amount"}, "profile": set()}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Parameter(_Model):
    unit: Literal["mm", "scalar"] = "mm"
    value: Finite | None = None
    expression: Expression | None = None

    @model_validator(mode="after")
    def _one_source(self) -> Parameter:
        if (self.value is None) == (self.expression is None):
            raise ValueError("A parameter needs exactly one value or expression")
        return self


class Binding(_Model):
    feature_id: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")
    target: Literal["length", "width", "height", "diameter", "depth", "amount", "x", "y", "z", "value"]
    constraint_id: str | None = Field(default=None, pattern=r"^con_[a-z0-9_]{1,48}$")
    expression: Expression


class Constraint(_Model):
    left: Expression
    operator: Literal["<", "<=", ">", ">="]
    right: Expression
    message: str = Field(min_length=1, max_length=200)


class Relationships(_Model):
    parameters: dict[ParameterName, Parameter] = Field(default_factory=dict, max_length=64)
    bindings: list[Binding] = Field(default_factory=list, max_length=256)
    constraints: list[Constraint] = Field(default_factory=list, max_length=64)


# Quantity = (value, length exponent). Only scalar and mm quantities are allowed.
Quantity = tuple[float, int]


def _bounded(value: float, dimension: int) -> Quantity:
    if not math.isfinite(value) or abs(value) > 1e9:
        raise ValueError("Parameter result must be finite and within +/- 1e9")
    if dimension not in (0, 1):
        raise ValueError("Only mm and scalar quantities are supported")
    return value, dimension


def evaluate(expression: str, lookup: Callable[[str], Quantity]) -> Quantity:
    """Interpret an allowlisted AST; never eval/compile or execute user code."""
    if not 0 < len(expression) <= 256:
        raise ValueError("Expression length must be 1..256")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("Invalid parameter expression") from exc
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ValueError("Expression exceeds 64 syntax nodes")

    def visit(node: ast.AST) -> Quantity:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return _bounded(float(node.value), 0)
        if isinstance(node, ast.Name):
            return lookup(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value, unit = visit(node.operand)
            return _bounded(-value if isinstance(node.op, ast.USub) else value, unit)
        if isinstance(node, ast.BinOp):
            left, lu = visit(node.left)
            right, ru = visit(node.right)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                if lu != ru:
                    raise ValueError("Addition/subtraction requires matching units")
                return _bounded(left + right if isinstance(node.op, ast.Add) else left - right, lu)
            if isinstance(node.op, ast.Mult):
                return _bounded(left * right, lu + ru)
            if isinstance(node.op, ast.Div):
                if right == 0:
                    raise ValueError("Division by zero in parameter expression")
                return _bounded(left / right, lu - ru)
        raise ValueError("Expressions support only names, numbers, parentheses and + - * /")

    return visit(tree.body)


def resolve_parameters(relationships: Relationships) -> dict[str, Quantity]:
    resolved: dict[str, Quantity] = {}
    depths: dict[str, int] = {}
    visiting: set[str] = set()

    def lookup(name: str) -> Quantity:
        if name in resolved:
            return resolved[name]
        if name in visiting:
            raise ValueError(f"Cyclic parameter dependency at {name}")
        if len(visiting) >= 16:
            raise ValueError("Parameter dependencies exceed 16 levels")
        parameter = relationships.parameters.get(name)
        if parameter is None:
            raise ValueError(f"Unknown parameter: {name}")
        visiting.add(name)
        unit = 1 if parameter.unit == "mm" else 0
        dependency_depth = 0

        def reference(dependency: str) -> Quantity:
            nonlocal dependency_depth
            quantity = lookup(dependency)
            dependency_depth = max(dependency_depth, depths[dependency])
            return quantity

        if parameter.expression is not None:
            quantity = evaluate(parameter.expression, reference)
        else:
            assert parameter.value is not None
            quantity = parameter.value, unit
        if quantity[1] != unit:
            raise ValueError(f"Parameter {name} expression does not have unit {parameter.unit}")
        depths[name] = dependency_depth + 1
        if depths[name] > 16:
            raise ValueError("Parameter dependencies exceed 16 levels")
        resolved[name] = _bounded(*quantity)
        visiting.remove(name)
        return resolved[name]

    for name in relationships.parameters:
        lookup(name)
    for constraint in relationships.constraints:
        left, lu = evaluate(constraint.left, lookup)
        right, ru = evaluate(constraint.right, lookup)
        if lu != ru:
            raise ValueError("Constraint requires matching units")
        valid = {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[constraint.operator]
        if not valid:
            raise ValueError(f"Relationship constraint failed: {constraint.message}")
    return resolved


def binding_value(feature: dict, target: str, constraint_id: str | None = None) -> Any:
    if constraint_id is not None:
        return next((c.get("value") for c in feature.get("parameters", {}).get("constraints", []) if c.get("id") == constraint_id), None)
    if target in ("x", "y", "z"):
        return feature.get("placement", {}).get("origin", [0, 0, 0])["xyz".index(target)]
    return feature.get("parameters", {}).get(target)


def materialize(document: Any) -> Any:
    """Return a private numeric document; validate relationships before geometry."""
    if not isinstance(document, dict) or not document.get("relationships"):
        return document
    relationships = Relationships.model_validate(document["relationships"])
    if not relationships.parameters and not relationships.bindings and not relationships.constraints:
        return document
    values = resolve_parameters(relationships)
    result = copy.deepcopy(document)
    if not isinstance(result.get("features", []), list):
        raise ValueError("Relationship features must be an array")
    # Ledger constructors may receive model objects as well as raw JSON.
    result["features"] = [f.model_dump(mode="json") if isinstance(f, BaseModel) else f
                          for f in result.get("features", [])]
    if any(not isinstance(f, dict) or not isinstance(f.get("id"), str) for f in result["features"]):
        raise ValueError("Relationship features require valid feature IDs")
    features = {f["id"]: f for f in result["features"]}
    seen = set()

    def lookup(name: str) -> Quantity:
        if name not in values:
            raise ValueError(f"Unknown parameter: {name}")
        return values[name]

    for binding in relationships.bindings:
        key = binding.feature_id, binding.target, binding.constraint_id
        if key in seen:
            raise ValueError(f"Duplicate binding: {key}")
        seen.add(key)
        feature = features.get(binding.feature_id)
        if feature is None:
            raise ValueError(f"Binding targets missing top-level feature: {binding.feature_id}")
        if binding.constraint_id is not None:
            if feature.get("kind") != "profile" or binding.target != "value":
                raise ValueError("Constraint binding must target a profile dimension value")
            constraint = next((c for c in feature.get("parameters", {}).get("constraints", []) if c.get("id") == binding.constraint_id), None)
            if constraint is None or constraint.get("kind") != "dimension":
                raise ValueError(f"Missing sketch dimension constraint: {binding.constraint_id}")
            value, unit = evaluate(binding.expression, lookup)
            if unit != 1:
                raise ValueError("Sketch dimensions require mm")
            constraint["value"] = value
            continue
        if feature.get("kind") not in DIMENSIONS or (binding.target not in ("x", "y", "z") and binding.target not in DIMENSIONS[feature["kind"]]):
            raise ValueError(f"Unsupported binding target: {binding.target}")
        if not isinstance(feature.get("parameters"), dict):
            raise ValueError("Bound features require numeric parameters")
        if binding.target == "depth" and feature["parameters"].get("through", True):
            raise ValueError("Cannot bind inactive through-hole depth")
        value, unit = evaluate(binding.expression, lookup)
        if unit != 1:
            raise ValueError(f"Binding {key} requires mm")
        if binding.target in ("x", "y", "z"):
            placement = feature.setdefault("placement", {})
            if not isinstance(placement, dict):
                raise ValueError("Bound placement must be an object")
            origin = placement.setdefault("origin", [0.0, 0.0, 0.0])
            if not isinstance(origin, list) or len(origin) != 3:
                raise ValueError("Bound origin requires three coordinates")
            origin["xyz".index(binding.target)] = value
        else:
            feature["parameters"][binding.target] = value
    return result


def reject_bound_overrides(before: dict, after: dict) -> None:
    """A patch must edit a driver/unbind, never silently overwrite a result."""
    if not after.get("relationships"):
        return
    resolved = materialize(after)
    relationships = Relationships.model_validate(after["relationships"])
    previous = {f["id"]: f for f in before.get("features", [])}
    proposed = {f["id"]: f for f in after.get("features", [])}
    actual = {f["id"]: f for f in resolved["features"]}
    for binding in relationships.bindings:
        fid, target = binding.feature_id, binding.target
        if fid in previous and binding_value(previous[fid], target, binding.constraint_id) != binding_value(proposed[fid], target, binding.constraint_id):
            if binding_value(proposed[fid], target, binding.constraint_id) != binding_value(actual[fid], target, binding.constraint_id):
                raise ValueError(f"{fid}.{target} is driven by {binding.expression}; edit its named parameter or explicitly remove the binding")


def dimension_edit_path(ledger: Any, feature_id: str, parameter: str, constraint_id: str | None = None) -> str:
    """Simple bindings edit their driver; compound relationships are read-only."""
    for binding in ledger.relationships.bindings:
        if binding.feature_id == feature_id and binding.target == parameter and binding.constraint_id == constraint_id:
            name = binding.expression.strip()
            driver = ledger.relationships.parameters.get(name)
            if driver is None or driver.expression is not None:
                raise ValueError(f"Derived dimension: {binding.expression}. Edit its driving parameters.")
            return f"/relationships/parameters/{name}/value"
    if constraint_id is not None:
        return f"/features/{feature_id}/parameters/constraints/{constraint_id}/value"
    if parameter in ("x", "y", "z"):
        return f"/features/{feature_id}/placement/origin/{'xyz'.index(parameter)}"
    return f"/features/{feature_id}/parameters/{parameter}"
