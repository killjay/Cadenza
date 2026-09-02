"""Stage 1: ledger JSON -> BuildPlan. No kernel, no OCC import, fully unit-testable.

Lifted in spirit from draftsmith's `compiler/plan.py`: resolve every field to a
concrete number, then run invariant gates, so a large class of nonsense is
rejected before build123d is touched. Two reasons that separation earns its
keep here:

  * OCC reports bad input as `StdFail_NotDone` with no indication of which
    feature caused it. An LLM cannot repair that. "hole feat_bore_1 Ø30 at
    (60, 0) falls outside the 100x100 envelope" it can.
  * These checks run in ~30us. A failed OCC boolean costs milliseconds and can
    leave an invalid shape that only fails later, during export.

Accepts EITHER the architect's pydantic `Ledger` or a plain dict. See the note
in contracts.py — `cadenza_contracts` is currently un-importable, so dict is
the path that actually runs today.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from cadenza_geometry.contracts import GeometryError, GeometryErrorCode
from cadenza_geometry.gear import (
    DEFAULT_PRESSURE_ANGLE,
    GearGeometry,
    gear_geometry,
    undercut_warning,
)

SUPPORTED_KINDS = {"box", "cylinder", "hole", "gear", "cone", "sphere", "torus", "edge_fillet", "edge_chamfer", "sketch", "linear_pattern", "circular_pattern", "shell", "revolve", "loft", "extrude"}

# Positive-dimension fields, by feature kind. A gear's `teeth`, `pressure_angle`
# and `shift` are NOT here: teeth is an integer count, and shift is legitimately
# zero or negative. They are resolved separately, below.
_SCALARS: dict[str, tuple[str, ...]] = {
    "box": ("length", "width", "height"),
    "cylinder": ("diameter", "height"),
    "hole": ("diameter",),
    "gear": ("module", "height"),
    "cone": ("bottom_diameter", "top_diameter", "height"),
    "sphere": ("diameter",),
    "torus": ("major_diameter", "minor_diameter"),
    "edge_fillet": ("radius",),
    "edge_chamfer": ("width",),
    "sketch": (),
    "linear_pattern": ("spacing",),
    "circular_pattern": (),
    "shell": (),  # thickness can be negative
    "revolve": (), # angle can be optional (default 360)
    "loft": (),
    "extrude": ("amount",),
}

TOL = 1e-7


def _err(code: GeometryErrorCode, msg: str, fid: str | None = None, detail: str | None = None):
    return GeometryError(code=code, message=msg, feature_id=fid, detail=detail)


@dataclass
class RFeature:
    """One ledger feature with every field resolved to concrete numbers.

    Geometry conventions, fixed by `cadenza_contracts.ledger.Placement`:
      * box / cylinder: `origin` is the CENTRE of the footprint at the BASE, so
        origin.z is the bottom face and the solid runs to origin.z + height.
      * hole: `origin` is where the axis meets the face being cut, and the hole
        is cut along -Z of its own (rotated) frame.
    """

    id: str
    kind: str
    operation: str
    name: str
    origin: tuple[float, float, float]
    rotation_deg: tuple[float, float, float]
    relative_to: str | None = None
    p: dict[str, Any] = field(default_factory=dict)
    through: bool = False
    depth: float | None = None
    index: int = 0
    #: Derived gear radii and angles, resolved once at plan time. Set for
    #: `kind == "gear"` and None otherwise, so the builder and the attributor
    #: work from identical numbers rather than each re-deriving them.
    gear: GearGeometry | None = None

    @property
    def is_rotated(self) -> bool:
        return any(abs(a) > 1e-9 for a in self.rotation_deg)

    def local_extent(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """AABB of this feature IN ITS OWN LOCAL FRAME. Used for containment
        tests during face attribution, and for the envelope gate."""
        if self.kind == "box":
            hl, hw = self.p["length"] / 2, self.p["width"] / 2
            return (-hl, -hw, 0.0), (hl, hw, self.p["height"])
        if self.kind == "cylinder":
            r = self.p["diameter"] / 2
            return (-r, -r, 0.0), (r, r, self.p["height"])
        if self.kind == "gear":
            assert self.gear is not None  # set by resolve() for every gear
            r = self.gear.tip_radius
            return (-r, -r, 0.0), (r, r, self.p["height"])
        if self.kind == "cone":
            r = max(self.p["bottom_diameter"], self.p["top_diameter"]) / 2
            return (-r, -r, 0.0), (r, r, self.p["height"])
        if self.kind == "sphere":
            r = self.p["diameter"] / 2
            return (-r, -r, -r), (r, r, r)
        if self.kind == "torus":
            r = (self.p["major_diameter"] + self.p["minor_diameter"]) / 2
            h = self.p["minor_diameter"] / 2
            return (-r, -r, -h), (r, r, h)
        if self.kind == "edge_fillet":
            r = self.p["radius"]
            hl = self.p["length"] / 2
            return (0.0, 0.0, -hl), (r, r, hl)
        if self.kind == "edge_chamfer":
            w = self.p["width"]
            hl = self.p["length"] / 2
            return (0.0, 0.0, -hl), (w, w, hl)
        if self.kind == "hole":
            r = self.p["diameter"] / 2
            d = self.depth if not self.through else 0.0
            return (-r, -r, -(d or 0.0)), (r, r, 0.0)
        if self.kind == "sketch":
            xs, ys = [], []
            verts = self.p.get("vertices", [])
            edges = self.p.get("edges", [])
            for v in verts:
                xs.append(v[0])
                ys.append(v[1])
            for e in edges:
                if "p1" in e:
                    xs.append(e["p1"][0])
                    ys.append(e["p1"][1])
                if "p2" in e:
                    xs.append(e["p2"][0])
                    ys.append(e["p2"][1])
                if "p3" in e:
                    xs.append(e["p3"][0])
                    ys.append(e["p3"][1])
            if not xs or not ys:
                return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
            return (min(xs), min(ys), 0.0), (max(xs), max(ys), self.p["height"])
        if self.kind in ("linear_pattern", "circular_pattern", "extrude"):
            return (-1000.0, -1000.0, -1000.0), (1000.0, 1000.0, 1000.0)
        if self.kind == "revolve":
            verts = self.p.get("vertices", [])
            if not verts:
                return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
            xs = [v[0] for v in verts]
            ys = [v[1] for v in verts]
            rmax = max([abs(x) for x in xs] + [abs(y) for y in ys]) # approx bounds
            zmin = min(ys)
            zmax = max(ys)
            return (-rmax, -rmax, zmin), (rmax, rmax, zmax)
        if self.kind == "loft":
            xs, ys, zs = [], [], []
            sections = self.p.get("sections", [])
            for s in sections:
                loc = s.get("location", {})
                origin = loc.get("origin", [0,0,0])
                xs.append(origin[0])
                ys.append(origin[1])
                zs.append(origin[2])
            if not xs:
                return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
            # Rough bounds based on section origins + some padding
            rmax = 50.0
            return (min(xs)-rmax, min(ys)-rmax, min(zs)-rmax), (max(xs)+rmax, max(ys)+rmax, max(zs)+rmax)
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)


@dataclass
class BuildPlan:
    project_id: str
    name: str
    revision: int
    units: str
    features: list[RFeature]
    warnings: list[str] = field(default_factory=list)

    @property
    def base(self) -> RFeature:
        return self.features[0]

    def by_id(self, fid: str) -> RFeature | None:
        return next((f for f in self.features if f.id == fid), None)


def _as_dict(ledger: Any) -> dict[str, Any]:
    """Normalise pydantic-or-dict to a plain dict. Keeps this package usable
    before `cadenza_contracts` becomes importable, and after."""
    if hasattr(ledger, "model_dump"):
        return ledger.model_dump(mode="json")
    if isinstance(ledger, dict):
        return ledger
    raise _err(
        GeometryErrorCode.INVALID_PARAMETER,
        f"ledger must be a dict or a pydantic model, got {type(ledger).__name__}",
    )


def _vec3(raw: Any, fallback: tuple[float, float, float], fid: str, fname: str) -> tuple[float, float, float]:
    if raw is None:
        return fallback
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise _err(GeometryErrorCode.INVALID_PARAMETER, f"{fname} must be 3 numbers", fid)
    try:
        out = tuple(float(c) for c in raw)
    except (TypeError, ValueError) as exc:
        raise _err(GeometryErrorCode.INVALID_PARAMETER, f"{fname} must be numeric", fid) from exc
    if any(math.isnan(c) or math.isinf(c) for c in out):
        raise _err(GeometryErrorCode.INVALID_PARAMETER, f"{fname} contains NaN/inf", fid)
    return out  # type: ignore[return-value]


def _resolve_gear(params: dict[str, Any], p: dict[str, float], fid: str) -> GearGeometry:
    """Gear parameters -> derived geometry, with every bad input named.

    `module` and `height` have already been through the positive-scalar gate.
    What is left is the integer tooth count and the two angles, which have their
    own rules — a tooth count must be a whole number, and a profile shift is
    legitimately zero or negative, so neither belongs in that gate.
    """
    raw_teeth = params.get("teeth")
    if raw_teeth is None:
        raise _err(GeometryErrorCode.INVALID_PARAMETER, "a gear requires `teeth`", fid)
    try:
        teeth = int(raw_teeth)
    except (TypeError, ValueError) as exc:
        raise _err(
            GeometryErrorCode.INVALID_PARAMETER,
            f"`teeth` must be a whole number, got {raw_teeth!r}",
            fid,
        ) from exc
    if teeth != raw_teeth:
        raise _err(
            GeometryErrorCode.INVALID_PARAMETER,
            f"`teeth` must be a whole number, got {raw_teeth!r}",
            fid,
            detail="a gear cannot have a fractional tooth",
        )

    def number(name: str, default: float) -> float:
        v = params.get(name)
        if v is None:
            return default
        try:
            fv = float(v)
        except (TypeError, ValueError) as exc:
            raise _err(
                GeometryErrorCode.INVALID_PARAMETER, f"`{name}` must be numeric, got {v!r}", fid
            ) from exc
        if math.isnan(fv) or math.isinf(fv):
            raise _err(GeometryErrorCode.INVALID_PARAMETER, f"`{name}` is NaN/inf", fid)
        return fv

    # gear_geometry raises GeometryError with this feature id for anything it
    # cannot build — too few teeth, pointed teeth, a collapsed root circle.
    return gear_geometry(
        module=p["module"],
        teeth=teeth,
        pressure_angle_deg=number("pressure_angle_deg", DEFAULT_PRESSURE_ANGLE),
        shift=number("shift", 0.0),
        feature_id=fid,
    )


def resolve(ledger: Any) -> BuildPlan:
    """Ledger -> BuildPlan, or raise GeometryError. Runs the invariant gates."""
    doc = _as_dict(ledger)
    raw_features = doc.get("features", [])
    if not isinstance(raw_features, list):
        raise _err(GeometryErrorCode.INVALID_PARAMETER, "`features` must be an array")

    feats: list[RFeature] = []
    seen: set[str] = set()

    for raw in raw_features:
        if not isinstance(raw, dict):
            raise _err(GeometryErrorCode.INVALID_PARAMETER, "each feature must be an object")
        fid = raw.get("id") or "<missing id>"
        if raw.get("suppressed"):
            continue  # suppressed features are invisible to geometry entirely
        if fid in seen:
            raise _err(GeometryErrorCode.INVALID_PARAMETER, f"duplicate feature id: {fid}", fid)
        seen.add(fid)

        kind = raw.get("kind")
        if kind not in SUPPORTED_KINDS:
            raise _err(
                GeometryErrorCode.UNSUPPORTED_FEATURE,
                f"feature kind {kind!r} is not supported (milestone 1 supports "
                f"{sorted(SUPPORTED_KINDS)})",
                fid,
            )

        params = raw.get("parameters") or {}
        p: dict[str, Any] = {}
        for nm in _SCALARS[kind]:
            v = params.get(nm)
            if v is None:
                raise _err(GeometryErrorCode.INVALID_PARAMETER, f"{kind} requires `{nm}`", fid)
            try:
                fv = float(v)
            except (TypeError, ValueError) as exc:
                raise _err(
                    GeometryErrorCode.INVALID_PARAMETER, f"`{nm}` must be numeric, got {v!r}", fid
                ) from exc
            if math.isnan(fv) or math.isinf(fv):
                raise _err(GeometryErrorCode.INVALID_PARAMETER, f"`{nm}` is NaN/inf", fid)
            if fv <= TOL:
                raise _err(
                    GeometryErrorCode.INVALID_PARAMETER,
                    f"`{nm}` must be > 0, got {fv:g}",
                    fid,
                )
            p[nm] = fv

        # Extract non-scalar parameters safely
        if kind in ("edge_fillet", "edge_chamfer"):
            p["select_all"] = params.get("select_all", False)
            p["edge_selectors"] = params.get("edge_selectors", [])
            # Also extract length for backward compatibility of cutter primitive
            if "length" in params:
                p["length"] = float(params["length"])
        elif kind == "shell":
            p["thickness"] = float(params.get("thickness", 0.0))
            p["open_faces"] = params.get("open_faces", [])
        elif kind == "sketch":
            p["vertices"] = params.get("vertices", [])
            p["edges"] = params.get("edges", [])
            p["draft_angle"] = float(params.get("draft_angle", 0.0))
        elif kind == "revolve":
            p["vertices"] = params.get("vertices", [])
            p["angle"] = float(params.get("angle", 360.0))
        elif kind == "loft":
            p["sections"] = params.get("sections", [])


        placement = raw.get("placement") or {}
        origin = _vec3(placement.get("origin"), (0.0, 0.0, 0.0), fid, "placement.origin")
        
        if kind == "hole" and "direction" in placement:
            direction = _vec3(placement.get("direction"), (0.0, 0.0, -1.0), fid, "placement.direction")
            if direction == (0.0, 0.0, 0.0):
                raise _err(GeometryErrorCode.INVALID_PARAMETER, "`direction` cannot be zero", fid)
            from build123d import Plane
            # The hole cutter drills into local -Z. To drill along `direction`,
            # we must orient local +Z to point to `-direction`.
            inv_dir = [-d for d in direction]
            try:
                loc = Plane(origin=(0,0,0), z_dir=inv_dir).location
                rot = (loc.orientation.X, loc.orientation.Y, loc.orientation.Z)
            except Exception as e:
                raise _err(GeometryErrorCode.INVALID_PARAMETER, f"Invalid direction vector {direction}: {e}", fid)
        else:
            rot = _vec3(placement.get("rotation_deg"), (0.0, 0.0, 0.0), fid, "placement.rotation_deg")
            
        relative_to = placement.get("relative_to")

        through, depth = False, None
        if kind == "hole":
            through = bool(params.get("through", True))
            if not through:
                d = params.get("depth")
                if d is None:
                    raise _err(
                        GeometryErrorCode.INVALID_PARAMETER,
                        "blind hole requires `depth`",
                        fid,
                        detail="set parameters.through=true, or supply parameters.depth",
                    )
                depth = float(d)
                if depth <= TOL:
                    raise _err(
                        GeometryErrorCode.INVALID_PARAMETER, f"`depth` must be > 0, got {depth:g}", fid
                    )
        elif kind in ("box", "cylinder", "cone", "sphere", "torus"):
            through = bool(params.get("through", False))
            p["draft_angle"] = float(params.get("draft_angle", 0.0))
            p["align"] = params.get("align")
        elif kind == "sketch":
            p["draft_angle"] = float(params.get("draft_angle", 0.0))
            p["height"] = float(params.get("height", 0.0))
            if not p["vertices"] and not p["edges"]:
                raise _err(GeometryErrorCode.INVALID_PARAMETER, "sketch requires vertices or edges", fid)
            if not p["edges"] and (not p["vertices"] or len(p["vertices"]) < 3):
                raise _err(GeometryErrorCode.INVALID_PARAMETER, "sketch requires at least 3 vertices", fid)
            p["vertices"] = [[float(pt[0]), float(pt[1])] for pt in p["vertices"]]
            for e in p["edges"]:
                if not isinstance(e, dict) or "type" not in e:
                    raise _err(GeometryErrorCode.INVALID_PARAMETER, f"sketch edges must be dicts with 'type', got {e}", fid)
        elif kind == "extrude":
            target = params.get("target_feature")
            if not target:
                raise _err(GeometryErrorCode.INVALID_PARAMETER, "extrude requires target_feature", fid)
            p["target_feature"] = str(target)
            p["twist"] = float(params.get("twist", 0.0))
            p["taper"] = float(params.get("taper", 0.0))
        elif kind == "revolve":
            p["angle"] = float(params.get("angle", 360.0))
            p["axis"] = _vec3(params.get("axis"), (0.0, 1.0, 0.0), fid, "axis")
            verts = params.get("vertices")
            if not isinstance(verts, list) or len(verts) < 3:
                raise _err(GeometryErrorCode.INVALID_PARAMETER, "revolve requires at least 3 vertices", fid)
            p["vertices"] = [[float(pt[0]), float(pt[1])] for pt in verts]
        elif kind in ("linear_pattern", "circular_pattern"):
            target = params.get("target_feature")
            if not target:
                raise _err(GeometryErrorCode.INVALID_PARAMETER, "pattern requires target_feature", fid)
            p["target_feature"] = str(target)
            count = int(params.get("count", 2))
            if count < 2:
                raise _err(GeometryErrorCode.INVALID_PARAMETER, "pattern count must be >= 2", fid)
            p["count"] = count
            if kind == "linear_pattern":
                p["axis"] = _vec3(params.get("axis"), (1.0, 0.0, 0.0), fid, "axis")
                p["count_2"] = int(params.get("count_2", 1))
                p["spacing_2"] = float(params.get("spacing_2", 0.0))
                p["axis_2"] = _vec3(params.get("axis_2"), (0.0, 1.0, 0.0), fid, "axis_2")
            else:
                p["sweep_angle"] = float(params.get("sweep_angle", 360.0))
        elif kind == "shell":
            p["thickness"] = float(params.get("thickness", -1.0))
            if abs(p["thickness"]) < TOL:
                raise _err(GeometryErrorCode.INVALID_PARAMETER, "shell thickness cannot be zero", fid)

        gear: GearGeometry | None = None
        if kind == "gear":
            gear = _resolve_gear(params, p, fid)

        op = raw.get("operation") or ("modify" if kind == "shell" else "subtract" if kind == "hole" else "add")
        if kind == "hole" and op != "subtract":
            raise _err(
                GeometryErrorCode.INVALID_PARAMETER, f"a hole cannot have operation={op!r}", fid
            )
        if kind in ("shell", "edge_fillet", "edge_chamfer") and op != "modify":
            # Topological fillet/chamfer operations modify the existing solid
            # If length is present, it's a legacy subtractive box cutter.
            if kind == "shell" or (kind in ("edge_fillet", "edge_chamfer") and "length" not in p):
                raise _err(
                    GeometryErrorCode.INVALID_PARAMETER, f"{kind} must have operation='modify' when used topologically", fid
                )
        if kind not in ("shell", "edge_fillet", "edge_chamfer") and op == "modify":
            raise _err(
                GeometryErrorCode.INVALID_PARAMETER, f"{kind} cannot have operation='modify'", fid
            )
        if op not in ("add", "subtract", "modify"):
            raise _err(GeometryErrorCode.INVALID_PARAMETER, f"unknown operation {op!r}", fid)

        feats.append(
            RFeature(
                id=fid,
                kind=kind,
                operation=op,
                name=raw.get("name") or "",
                origin=origin,
                rotation_deg=rot,
                relative_to=relative_to,
                p=p,
                through=through,
                depth=depth,
                index=len(feats),
                gear=gear,
            )
        )

    meta = doc.get("metadata") or {}
    plan = BuildPlan(
        project_id=doc.get("project_id") or "prj_unknown",
        name=meta.get("name") or "Untitled Part",
        revision=int(doc.get("revision") or 0),
        units=meta.get("global_units") or "mm",
        features=feats,
    )
    _validate(plan)
    return plan


def _validate(plan: BuildPlan) -> None:
    """Invariant gates. Everything here is cheaper than a failed OCC boolean."""
    if not plan.features:
        raise _err(
            GeometryErrorCode.EMPTY_RESULT,
            "ledger has no live features — nothing to build",
            detail="every feature is suppressed, or `features` is empty",
        )

    if plan.units != "mm":
        raise _err(
            GeometryErrorCode.INVALID_PARAMETER,
            f"units must be mm, got {plan.units!r}",
            detail="the ledger contract fixes millimetres; no conversion happens here",
        )

    base = plan.base
    if base.operation != "add":
        raise _err(
            GeometryErrorCode.INVALID_PARAMETER,
            f"the first live feature must be additive, but {base.id!r} is {base.operation!r}",
            base.id,
            detail="reorder features so a solid exists before anything cuts into it",
        )
    if base.kind == "hole":
        raise _err(
            GeometryErrorCode.INVALID_PARAMETER,
            f"the first live feature is a hole ({base.id!r}) — there is nothing to cut",
            base.id,
        )

    # Running envelope of ADDITIVE material. A cut can only shrink the solid,
    # so the union of what is added is an exact outer bound — which makes
    # "this hole misses the part" decidable without touching the kernel.
    env: list[float] | None = None
    for f in plan.features:
        if f.operation != "add":
            continue
        lo, hi = f.local_extent()
        corners = _world_corners(lo, hi, f)
        if env is None:
            env = [
                min(c[0] for c in corners), min(c[1] for c in corners), min(c[2] for c in corners),
                max(c[0] for c in corners), max(c[1] for c in corners), max(c[2] for c in corners),
            ]
        else:
            for i in range(3):
                env[i] = min(env[i], min(c[i] for c in corners))
                env[i + 3] = max(env[i + 3], max(c[i] for c in corners))

    assert env is not None  # base is additive, checked above

    for f in plan.features:
        if f.kind == "gear" and f.gear is not None:
            # Buildable, but not what a hob would cut. Say so here rather than
            # letting it be discovered on a test print.
            note = undercut_warning(f.gear, f.id)
            if note:
                plan.warnings.append(note)

    for f in plan.features:
        if f.kind != "hole":
            continue
        # A hole is located by its axis. If the axis passes nowhere near the
        # additive envelope the cut is a no-op and the user gets a silently
        # unchanged part -- the exact failure that reads as "the AI ignored me".
        r = f.p["diameter"] / 2
        ox, oy, oz = f.origin
        if not f.is_rotated:
            inside_xy = (
                env[0] - r - TOL <= ox <= env[3] + r + TOL
                and env[1] - r - TOL <= oy <= env[4] + r + TOL
            )
            if not inside_xy:
                raise _err(
                    GeometryErrorCode.INVALID_PARAMETER,
                    f"hole {f.id!r} at ({ox:g}, {oy:g}) lies outside the part envelope "
                    f"x[{env[0]:g}, {env[3]:g}] y[{env[1]:g}, {env[4]:g}] — it would cut nothing",
                    f.id,
                    suggest_move(env, ox, oy, r),
                )
            if oz < env[2] - TOL:
                plan.warnings.append(
                    f"hole {f.id!r} starts at z={oz:g}, below the part (z>={env[2]:g}); "
                    "cutting along -Z from there removes no material"
                )
            if not f.through and f.depth is not None and f.depth > (env[5] - env[2]) + TOL:
                plan.warnings.append(
                    f"hole {f.id!r} depth {f.depth:g} exceeds the part height "
                    f"{env[5] - env[2]:g} — it is effectively a through hole"
                )


def suggest_move(env: list[float], ox: float, oy: float, r: float) -> str:
    cx = min(max(ox, env[0] + r), env[3] - r)
    cy = min(max(oy, env[1] + r), env[4] - r)
    return f"move the hole to ({cx:g}, {cy:g}), or enlarge the part"


def _world_corners(lo, hi, f: RFeature) -> list[tuple[float, float, float]]:
    """The 8 local AABB corners, placed into world space.

    Uses the same rotation convention build123d's `Location` uses (intrinsic
    XYZ), reimplemented here only so this stage stays free of OCC. `builder.py`
    asserts the two agree.
    """
    rx, ry, rz = (math.radians(a) for a in f.rotation_deg)
    out = []
    for x in (lo[0], hi[0]):
        for y in (lo[1], hi[1]):
            for z in (lo[2], hi[2]):
                px, py, pz = rotate_xyz((x, y, z), rx, ry, rz)
                out.append((px + f.origin[0], py + f.origin[1], pz + f.origin[2]))
    return out


def rotate_xyz(p: tuple[float, float, float], rx: float, ry: float, rz: float):
    """Intrinsic X-then-Y-then-Z rotation, radians. Matches build123d Location."""
    x, y, z = p
    cx, sx = math.cos(rx), math.sin(rx)
    y, z = y * cx - z * sx, y * sx + z * cx
    cy, sy = math.cos(ry), math.sin(ry)
    x, z = x * cy + z * sy, -x * sy + z * cy
    cz, sz = math.cos(rz), math.sin(rz)
    x, y = x * cz - y * sz, x * sz + y * cz
    return x, y, z
