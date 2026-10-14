import math
import json
from typing import Annotated, Literal
from pydantic import ConfigDict, Field, ValidationError, create_model
from typing import List, Dict, Any, Union
from .base import Tool
from . import registry

def polar_to_cartesian(radius: float, angle_deg: float) -> Dict[str, float]:
    angle_rad = math.radians(angle_deg)
    return {"x": radius * math.cos(angle_rad), "y": radius * math.sin(angle_rad)}

def bolt_circle(radius: float, count: int, start_angle_deg: float = 0, center_x: float = 0, center_y: float = 0) -> List[Dict[str, float]]:
    coords = []
    angle_step = 360.0 / count
    for i in range(count):
        pt = polar_to_cartesian(radius, start_angle_deg + i * angle_step)
        coords.append({"x": pt["x"] + center_x, "y": pt["y"] + center_y})
    return coords

def linear_array(start_val: float, step_val: float, count: int) -> List[float]:
    return [start_val + i * step_val for i in range(count)]

def grid_array(start_x: float, start_y: float, step_x: float, step_y: float, count_x: int, count_y: int) -> List[Dict[str, float]]:
    coords = []
    for i in range(count_x):
        for j in range(count_y):
            coords.append({
                "x": start_x + i * step_x,
                "y": start_y + j * step_y
            })
    return coords

def regular_polygon(sides: int, radius: float, center_x: float = 0, center_y: float = 0) -> List[Dict[str, float]]:
    return bolt_circle(radius, sides, 0, center_x, center_y)

def mirror_2d(point: Dict[str, float], axis: str = "x") -> Dict[str, float]:
    if axis.lower() == "x":
        return {"x": point["x"], "y": -point["y"]}
    elif axis.lower() == "y":
        return {"x": -point["x"], "y": point["y"]}
    return point

def gear_center_distance(module: float, teeth1: int, teeth2: int) -> float:
    return (module * teeth1 + module * teeth2) / 2.0

def planetary_gear_layout(module: float, sun_teeth: int, planet_teeth: int, num_planets: int = 3, center_x: float = 0, center_y: float = 0) -> Dict[str, Any]:
    ring_teeth = sun_teeth + 2 * planet_teeth
    orbital_radius = gear_center_distance(module, sun_teeth, planet_teeth)
    planet_coords = bolt_circle(orbital_radius, num_planets, 0, center_x, center_y)
    return {
        "ring_gear_teeth": ring_teeth,
        "orbital_radius": orbital_radius,
        "planet_centers": planet_coords
    }

def gear_ratio(driving_teeth: int, driven_teeth: int) -> float:
    return driven_teeth / driving_teeth

def bearing_bore(bearing_od: float, fit: str = "press") -> float:
    if fit.lower() == "press":
        return bearing_od - 0.05
    elif fit.lower() == "slip":
        return bearing_od + 0.05
    return bearing_od

class MathSandboxTool(Tool):
    name = "math_sandbox"
    description = "Calculate geometry using a named operation and numeric arguments. No scripts or expressions. Maximum 1024 points per result. Gear layout computes pitch geometry only, not manufacturability."

    def execute(self, context=None, **kwargs) -> Dict[str, Any]:
        if set(kwargs) != {"operation", "arguments"}:
            return {"error": "Provide only operation and arguments; scripts are not supported."}
        operation = kwargs["operation"]
        if not isinstance(operation, str) or operation not in OPERATIONS:
            return {"error": "Unknown geometry math operation."}
        function, model = OPERATIONS[operation]
        try:
            args = model.model_validate(kwargs["arguments"]).model_dump()
            if operation == "grid_array" and args["count_x"] * args["count_y"] > 1024:
                return {"error": "Grid exceeds the 1024 point limit."}
            result = function(**args)
            json.dumps(result, allow_nan=False)
            response = {"result": result}
            if operation == "bearing_bore":
                response["warning"] = "Illustrative 0.05 mm allowance only; this is not a toleranced engineering fit."
            return response
        except (ValidationError, ValueError, TypeError, OverflowError) as exc:
            return {"error": f"Invalid geometry math arguments: {exc}"}


Number = Annotated[float, Field(strict=True, ge=-1e6, le=1e6, allow_inf_nan=False)]
Positive = Annotated[float, Field(strict=True, gt=0, le=1e6, allow_inf_nan=False)]
Count = Annotated[int, Field(strict=True, ge=1, le=1024)]
Sides = Annotated[int, Field(strict=True, ge=3, le=1024)]
Point = create_model("MathPoint", __config__=ConfigDict(extra="forbid"), x=(Number, ...), y=(Number, ...))
_centers = {"center_x": (Number, 0), "center_y": (Number, 0)}
_specs = {
    polar_to_cartesian: {"radius": (Positive, ...), "angle_deg": (Number, ...)},
    bolt_circle: {"radius": (Positive, ...), "count": (Count, ...), "start_angle_deg": (Number, 0), **_centers},
    linear_array: {"start_val": (Number, ...), "step_val": (Number, ...), "count": (Count, ...)},
    grid_array: {"start_x": (Number, ...), "start_y": (Number, ...), "step_x": (Number, ...), "step_y": (Number, ...), "count_x": (Count, ...), "count_y": (Count, ...)},
    regular_polygon: {"sides": (Sides, ...), "radius": (Positive, ...), **_centers},
    mirror_2d: {"point": (Point, ...), "axis": (Literal["x", "y"], "x")},
    gear_center_distance: {"module": (Positive, ...), "teeth1": (Count, ...), "teeth2": (Count, ...)},
    planetary_gear_layout: {"module": (Positive, ...), "sun_teeth": (Count, ...), "planet_teeth": (Count, ...), "num_planets": (Count, 3), **_centers},
    gear_ratio: {"driving_teeth": (Count, ...), "driven_teeth": (Count, ...)},
    bearing_bore: {"bearing_od": (Annotated[float, Field(strict=True, gt=0.05, le=1e6, allow_inf_nan=False)], ...), "fit": (Literal["press", "slip"], "press")},
}
OPERATIONS = {fn.__name__: (fn, create_model(fn.__name__ + "Args", __config__=ConfigDict(extra="forbid"), **fields)) for fn, fields in _specs.items()}
# Inline model references so each operation's schema remains self-contained.
def _inline_schema(model):
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})
    def expand(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return expand(definitions[value["$ref"].split("/")[-1]])
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        return value
    return expand(schema)

MathSandboxTool.schema = {
    "name": MathSandboxTool.name, "description": MathSandboxTool.description,
    "input_schema": {
        "type": "object", "additionalProperties": False,
        "properties": {"operation": {"type": "string", "enum": list(OPERATIONS)}, "arguments": {"type": "object"}},
        "required": ["operation", "arguments"],
        "oneOf": [{"properties": {"operation": {"const": name}, "arguments": _inline_schema(model)}} for name, (_, model) in OPERATIONS.items()],
    },
}

registry.register(MathSandboxTool())
