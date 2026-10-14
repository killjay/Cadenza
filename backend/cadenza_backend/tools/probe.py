from typing import Any
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from cadenza_contracts.surface_reference import ReferenceId, SurfaceReference

from .base import Tool
from . import registry
from cadenza_backend import geometry_bridge

class ProbeQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ledger_id: str | None = None
    query_type: Literal["get_bounding_box", "get_face_center", "check_collision", "measure_distance", "get_mass_properties", "get_face_properties", "get_edge_length", "get_cylinder_axis", "get_minimum_clearance", "capture_surface_reference", "resolve_surface_reference"]
    feature_1_id: str | None = Field(default=None, min_length=1)
    feature_2_id: str | None = Field(default=None, min_length=1)
    component_1_id: str | None = Field(default=None, min_length=1, description="Full leaf instance path for target 1, e.g. comp_left/comp_plate. Required for assembly feature measurements. Omit feature_1_id for whole-instance bounds, mass, distance, clearance or collision.")
    component_2_id: str | None = Field(default=None, min_length=1, description="Full leaf instance path for target 2 in distance, clearance or collision queries. Required independently of component_1_id for assembly target 2.")
    direction: Literal["+X", "-X", "+Y", "-Y", "+Z", "-Z", "top", "bottom", "left", "right", "front", "back", "cylindrical"] | None = None
    edge_index: int | None = Field(default=None, strict=True, ge=0)
    point: list[Annotated[float, Field(allow_inf_nan=False)]] | None = Field(default=None, min_length=3, max_length=3)
    normal: list[Annotated[float, Field(allow_inf_nan=False)]] | None = Field(default=None, min_length=3, max_length=3)
    reference: SurfaceReference | None = None
    reference_id: ReferenceId | None = None

    @model_validator(mode="after")
    def reference_arguments(self):
        if self.query_type in {"capture_surface_reference", "resolve_surface_reference"}:
            if any(value is not None for value in (self.feature_1_id, self.feature_2_id, self.component_1_id,
                                                   self.component_2_id, self.direction, self.edge_index)):
                raise ValueError("Reference queries use a capture point or a reference, not measurement selectors.")
            if self.query_type == "capture_surface_reference":
                if self.point is None or self.reference is not None or self.reference_id is not None:
                    raise ValueError("Capture requires point and optional normal only.")
            elif self.point is not None or self.normal is not None or (self.reference is None) == (self.reference_id is None):
                raise ValueError("Resolve requires exactly one of reference or reference_id, without coordinates.")
        elif any(value is not None for value in (self.point, self.normal, self.reference, self.reference_id)):
            raise ValueError("Reference arguments require a reference query type.")
        return self

class GeometryProbeTool(Tool):
    name = "geometry_probe"
    description = "Query the CAD engine for measurements or capture/resolve stable primitive surface references. Capture uses a point and optional normal on an actual surface. Resolve uses a reference object or a saved reference_id. Only status=resolved is usable; other statuses require repair. Save captured selectors using ledger surface_references patches when requested."
    schema = {
        "name": name,
        "description": description + " Select ledger_id explicitly when multiple ledgers are available. Coordinates and directions use that ledger's frame, including transformed assembly instances; lengths use millimetres. Select component_1_id/component_2_id using full leaf paths from spatial selection. Feature IDs belong to those source parts. Omit features to measure whole instances; omit all targets for whole-ledger bounds/mass. Feature collision measures independent primitives/cutters; whole-instance collision measures final solids. Edge indices are build-local.",
        "input_schema": ProbeQuery.model_json_schema(),
    }

    @staticmethod
    def _prepare(context: dict | None, kwargs: dict):
        if not context or "ledgers" not in context:
            raise ValueError("No ledger available in context. Cannot run geometry probe.")
        query = ProbeQuery.model_validate(kwargs)
        ledgers = context["ledgers"]
        lid = query.ledger_id
        if lid is None and len(ledgers) == 1:
            lid = next(iter(ledgers))
        if lid not in ledgers:
            raise ValueError("Choose a valid ledger_id; it is required when multiple ledgers are available.")
        return (ledgers[lid], query.model_dump(exclude={"ledger_id"}, exclude_none=True),
                context.get("all_ledgers", ledgers))

    async def execute_async(self, context: dict | None = None, **kwargs) -> Any:
        from cadenza_backend.geometry_jobs import inspect
        try:
            return await inspect(*self._prepare(context, kwargs))
        except Exception as exc:
            return {"error": f"Geometry probe failed: {exc}"}

    def execute(self, context: dict | None = None, **kwargs) -> Any:
        """Direct synchronous library/test entry point; providers use execute_async."""
        try:
            return geometry_bridge.run_geometry_probe(*self._prepare(context, kwargs))
        except Exception as exc:
            return {"error": f"Geometry probe failed: {exc}"}

# Register the tool
registry.register(GeometryProbeTool())
