"""Bounded authoring policy; historic revolve documents keep their geometry."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator

Coordinate = Annotated[StrictFloat | StrictInt, Field(ge=-1e6, le=1e6, allow_inf_nan=False)]
Angle = Annotated[StrictFloat | StrictInt, Field(gt=0, le=360, allow_inf_nan=False)]


class RevolveSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vertices: list[list[Coordinate]] = Field(min_length=3, max_length=64)
    angle: Angle

    @model_validator(mode="after")
    def simple_radial_profile(self):
        points = self.vertices
        if any(len(p) != 2 or p[0] < 0 for p in points):
            raise ValueError("Each vertex needs radius X >= 0 and axial Y, in mm.")
        if len({tuple(p) for p in points}) != len(points):
            raise ValueError("Use distinct corners; closure is automatic.")
        scale = max(1.0, max(max(p[i] for p in points) - min(p[i] for p in points) for i in (0, 1)))
        eps = 1e-10 * scale * scale

        def cross(a, b, c):
            return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])

        n = len(points)
        if any(abs(cross(points[i-1], points[i], points[(i+1) % n])) <= eps for i in range(n)):
            raise ValueError("Remove degenerate or collinear corners.")
        if abs(sum(points[i][0]*points[(i+1) % n][1] - points[(i+1) % n][0]*points[i][1] for i in range(n))) <= eps:
            raise ValueError("The profile must enclose a nonzero area.")
        for i in range(n):
            a, b = points[i], points[(i+1) % n]
            for j in range(i+1, n):
                if j == i+1 or (i == 0 and j == n-1):
                    continue
                c, d = points[j], points[(j+1) % n]
                if any(max(min(a[k], b[k]), min(c[k], d[k])) > min(max(a[k], b[k]), max(c[k], d[k])) for k in (0, 1)):
                    continue
                ab = cross(a, b, c), cross(a, b, d)
                cd = cross(c, d, a), cross(c, d, b)
                if min(ab) <= eps and max(ab) >= -eps and min(cd) <= eps and max(cd) >= -eps:
                    raise ValueError("The profile must not cross or touch itself.")
        return self


def revolve_reason(feature: dict) -> str | None:
    if feature.get("kind") != "revolve":
        return "Select a revolve feature."
    if feature.get("placement", {}).get("relative_to") is not None:
        return "Relative placement is not supported by revolve controls."
    params = feature.get("parameters", {})
    if params.get("axis", [0, 1, 0]) != [0, 1, 0]:
        return "This legacy revolve uses another axis. Its geometry is preserved; these controls use local +Y."
    try:
        RevolveSettings.model_validate(dict(vertices=params.get("vertices"), angle=params.get("angle", 360)))
    except ValueError:
        return "This legacy profile is outside the supported simple, nonnegative-radius subset."
    return None
