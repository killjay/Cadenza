"""The ONE module to re-point when the architect's `cadenza_contracts.geometry` lands.

Everything else in this package imports its interface types from here, never from
`cadenza_contracts` directly. When `cadenza_contracts/geometry.py` exists, the
try-block below picks it up and the local mirrors go away; if the shapes differ,
this file is the only thing that changes.

Status at time of writing: `cadenza_contracts/__init__.py` names
`geometry.py` and `messages.py`, but NEITHER FILE EXISTS YET, so the package is
un-importable in its entirety (importing any submodule runs the failing
`__init__`). Hence the mirrors, and hence the ledger being read as plain JSON
dicts rather than as their pydantic `Ledger`. See NOTES.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# Optional upgrade path — light up automatically once the architect lands the
# real files. Never hard-fail: this package must build geometry with or without.
# --------------------------------------------------------------------------- #

HAVE_CONTRACTS = False
LedgerModel: Any = None

try:  # pragma: no cover - depends on another dev's working tree
    from cadenza_contracts.ledger import Ledger as LedgerModel  # type: ignore

    HAVE_CONTRACTS = True
except Exception:  # ModuleNotFoundError today, could be anything mid-edit
    HAVE_CONTRACTS = False
    LedgerModel = None


# --------------------------------------------------------------------------- #
# Mirrors of the names declared in cadenza_contracts/__init__.py
# --------------------------------------------------------------------------- #


class GeometryErrorCode(str, Enum):
    """Mirrors the GEOMETRY_* members of `cadenza_contracts.errors.ErrorCode`.

    Values are the wire strings, so re-emitting is `ErrorCode(hit.code.value)`
    with no mapping table.
    """

    UNSUPPORTED_FEATURE = "GEOMETRY_UNSUPPORTED_FEATURE"
    INVALID_PARAMETER = "GEOMETRY_INVALID_PARAMETER"
    KERNEL_FAILURE = "GEOMETRY_KERNEL_FAILURE"
    EMPTY_RESULT = "GEOMETRY_EMPTY_RESULT"
    EXPORT_FAILURE = "GEOMETRY_EXPORT_FAILURE"
    TIMEOUT = "GEOMETRY_TIMEOUT"
    NO_HIT = "NO_HIT"


@dataclass
class GeometryError(Exception):
    """Raised by everything in this package. Carries a wire-ready code.

    `feature_id` is set whenever the failure is attributable to one ledger
    feature, so the agent layer can tell the user *which* feature is wrong
    rather than "the build failed".
    """

    code: GeometryErrorCode
    message: str
    feature_id: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "feature_id": self.feature_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class BBox:
    """Axis-aligned bounds in mm, model space, Z-up."""

    min: tuple[float, float, float]
    max: tuple[float, float, float]

    @property
    def size(self) -> tuple[float, float, float]:
        return tuple(b - a for a, b in zip(self.min, self.max))  # type: ignore[return-value]

    @property
    def center(self) -> tuple[float, float, float]:
        return tuple((a + b) / 2 for a, b in zip(self.min, self.max))  # type: ignore[return-value]

    def to_dict(self) -> dict[str, Any]:
        return {"min": list(self.min), "max": list(self.max)}


@dataclass(frozen=True)
class EntityRef:
    """A resolved topological entity, named SEMANTICALLY rather than by index.

    `label` is a stable human/LLM-readable role ("top_face", "bore") derived
    from the feature that generated the entity — NOT an OCC face index. Face
    indices are deliberately never exposed past this boundary; see blueprint §7.
    """

    kind: str  # "face" for milestone 1
    feature_id: str
    label: str
    index: int | None = None  # OCC ordinal, debug only, never persisted

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "feature_id": self.feature_id, "label": self.label}


@dataclass(frozen=True)
class SpatialQuery:
    """A click, in model space.

    `normal` is the surface normal recorded AT CLICK TIME (the frontend already
    has it from the raycast, and `cadenza_contracts.ledger.TargetRef` already
    has the field). Supplying it is not cosmetic: it is what stops a stored
    coordinate from silently re-resolving onto an unrelated face after the part
    changes shape. See NOTES.md for the measured difference.
    """

    point: tuple[float, float, float]
    max_distance: float | None = None  # None => always return the nearest face
    normal: tuple[float, float, float] | None = None


@dataclass(frozen=True)
class SpatialHit:
    """The answer to "what did the user click?" — semantic, not topological."""

    feature_id: str
    label: str
    distance: float
    point_on_face: tuple[float, float, float]
    normal: tuple[float, float, float]
    kind: str = "face"
    # How confident the attribution is. Drops when the click is equidistant
    # between two candidate faces — the failure mode called out in NOTES.md.
    confidence: float = 1.0
    ambiguous_with: tuple[str, ...] = ()
    # Why confidence is what it is, in words the agent layer can put in front of
    # the user ("I think you meant the top face, but the part has changed shape
    # since — did you mean ...?").
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """The blueprint §5 wire shape, plus the extras the agent layer wants."""
        return {
            "node_id": self.feature_id,
            "feature": self.label,
            "distance": round(self.distance, 6),
            "point_on_face": [round(c, 6) for c in self.point_on_face],
            "normal": [round(c, 6) for c in self.normal],
            "kind": self.kind,
            "confidence": round(self.confidence, 4),
            "ambiguous_with": list(self.ambiguous_with),
            "notes": list(self.notes),
        }


@dataclass
class BuildResult:
    """Everything one rebuild produces. In-process type: it holds bytes."""

    glb: bytes
    step: bytes
    bbox: BBox
    volume: float
    # feature_id -> semantic labels that survived into the final solid. A
    # feature present here contributed at least one visible face; a feature
    # absent was fully consumed (e.g. a boss swallowed by a later cut).
    entities: dict[str, list[str]] = field(default_factory=dict)
    revision: int = 0
    build_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Metadata only — bytes go on the wire as binary frames, not JSON."""
        return {
            "bbox": self.bbox.to_dict(),
            "volume": round(self.volume, 6),
            "entities": self.entities,
            "revision": self.revision,
            "build_ms": round(self.build_ms, 2),
            "glb_bytes": len(self.glb),
            "step_bytes": len(self.step),
            "warnings": self.warnings,
        }


@runtime_checkable
class GeometryService(Protocol):
    """What the backend calls. Implemented by `cadenza_geometry.service.Build123dGeometryService`."""

    def build(self, ledger: Any) -> BuildResult: ...

    def probe(self, ledger: Any, query: SpatialQuery) -> SpatialHit | None: ...
