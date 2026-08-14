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

SUPPORTED_KINDS = {"box", "cylinder", "hole", "gear"}

# Positive-dimension fields, by feature kind. A gear's `teeth`, `pressure_angle`
# and `shift` are NOT here: teeth is an integer count, and shift is legitimately
# zero or negative. They are resolved separately, below.
_SCALARS: dict[str, tuple[str, ...]] = {
    "box": ("length", "width", "height"),
    "cylinder": ("diameter", "height"),
    "hole": ("diameter",),
    "gear": ("module", "height"),
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
    p: dict[str, float] = field(default_factory=dict)
    through: bool = True
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
        r = self.p["diameter"] / 2
        d = self.depth if not self.through else 0.0
        return (-r, -r, -(d or 0.0)), (r, r, 0.0)


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
        p: dict[str, float] = {}
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

        placement = raw.get("placement") or {}
        origin = _vec3(placement.get("origin"), (0.0, 0.0, 0.0), fid, "placement.origin")
        rot = _vec3(placement.get("rotation_deg"), (0.0, 0.0, 0.0), fid, "placement.rotation_deg")

        through, depth = True, None
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

        gear: GearGeometry | None = None
        if kind == "gear":
            gear = _resolve_gear(params, p, fid)

        op = raw.get("operation") or ("subtract" if kind == "hole" else "add")
        if kind == "hole" and op != "subtract":
            raise _err(
                GeometryErrorCode.INVALID_PARAMETER, f"a hole cannot have operation={op!r}", fid
            )
        if op not in ("add", "subtract"):
            raise _err(GeometryErrorCode.INVALID_PARAMETER, f"unknown operation {op!r}", fid)

        feats.append(
            RFeature(
                id=fid,
                kind=kind,
                operation=op,
                name=raw.get("name") or "",
                origin=origin,
                rotation_deg=rot,
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
