"""The geometry service interface — the boundary between Dev A and Dev B.

Dev B implements `GeometryService` in `cadenza/geometry/`. Dev A imports the
Protocol and nothing else from that package. In milestone 1 it is a plain
in-process call (same interpreter, same memory) — the blueprint's "sandboxed
container" is deferred, and safely so, because NO MODEL-AUTHORED CODE IS EVER
EXECUTED (see ARCHITECTURE.md).

Types that cross the wire are pydantic models. `BuildResult` is a dataclass
because it carries raw bytes and never gets JSON-serialised whole.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from cadenza_contracts.ledger import Ledger, Vec3

ExportFormat = Literal["glb", "step"]
EntityKind = Literal["face", "edge", "vertex"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #


class BBox(_Model):
    min: Vec3
    max: Vec3

    @property
    def size(self) -> list[float]:
        return [b - a for a, b in zip(self.min, self.max)]

    @property
    def center(self) -> list[float]:
        return [(a + b) / 2 for a, b in zip(self.min, self.max)]


class BuildStats(_Model):
    """Cheap facts about the built solid. Shown in the UI, logged, asserted on."""

    bbox: BBox
    volume_mm3: float
    face_count: int
    build_ms: int
    export_ms: int = 0


@dataclass(slots=True)
class BuildResult:
    """In-process only. `glb`/`step` are the raw file bytes, built in memory —
    no temp files, no disk I/O (blueprint section 4)."""

    ledger_revision: int
    stats: BuildStats
    glb: bytes | None = None
    step: bytes | None = None
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# reverse spatial lookup
# --------------------------------------------------------------------------- #


class Ray(_Model):
    origin: Vec3
    direction: Vec3  # need not be normalised


class SpatialQuery(_Model):
    """"Which bit of the part did the user click?"

    `point` is a world-space point in ledger coordinates (mm, Z-up) — the
    frontend's Three.js raycast hit against the tessellated GLB. Because that
    mesh is an approximation of the B-rep, the point can sit a few microns off
    the true surface; `max_distance_mm` is the tolerance for snapping it back.

    `ray` is optional extra precision (camera position + direction). When
    present the implementation SHOULD prefer the first entity the ray actually
    strikes over the merely nearest one; when absent, nearest-by-distance is
    correct behaviour.
    """

    point: Vec3
    ray: Ray | None = None
    prefer: Literal["face", "edge", "vertex", "any"] = "face"
    max_distance_mm: float = Field(default=2.0, gt=0)


class EntityRef(_Model):
    """A topological entity in THIS build only.

    `index` is valid for exactly one revision and MUST NEVER be written to the
    ledger, sent back as a selector, or cached across a rebuild. It exists for
    frontend highlighting within a single frame.
    """

    kind: EntityKind
    index: int
    revision: int


class SpatialHit(_Model):
    """The answer to a click — raw geometry plus the semantics an LLM needs.

    `descriptor` is the field that earns its keep: it is pasted verbatim into
    the Machinist prompt, so it must read like a person describing the part.

        "the top face of 'Main Body' (a box), facing +Z, 100.0 x 100.0 mm"
        "the cylindrical wall of 'Centre Hole' (a hole), 10.0 mm diameter"
    """

    entity: EntityRef
    point: Vec3                       # snapped onto the exact B-rep surface
    normal: Vec3 | None = None        # outward, for faces
    distance_mm: float                # query point -> snapped point
    feature_id: str | None = None     # which ledger feature produced it
    feature_name: str | None = None
    feature_kind: str | None = None   # one of ledger.FEATURE_KINDS
    descriptor: str
    area_mm2: float | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class GeometryErrorCode(str, Enum):
    UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"   # kind not in the vocabulary
    INVALID_PARAMETER = "INVALID_PARAMETER"       # geometrically impossible value
    KERNEL_FAILURE = "KERNEL_FAILURE"             # OCCT raised
    EMPTY_RESULT = "EMPTY_RESULT"                 # every solid was cut away
    EXPORT_FAILURE = "EXPORT_FAILURE"             # GLB/STEP writer failed
    TIMEOUT = "TIMEOUT"                           # exceeded BUILD_TIMEOUT_S


class GeometryError(Exception):
    """A build failure, in terms something downstream can act on.

    The prose is for the user and the repair prompt. The structured fields are
    the same facts in machine-readable form, so a deterministic clamp does not
    have to parse numbers out of an English sentence. (Lifted from draftsmith's
    `BuildError`, which learned this the hard way.)

    CONTRACT: setting `parameter` to `limit` makes this error go away.
    Not "the bound" — for a hole diameter the limit is a ceiling, for a plate
    thickness it is a floor, and a caller that had to know which would get it
    wrong. Expressing it as the nearest WORKING value makes the fix
    `setattr(feature.parameters, err.parameter, err.limit)` in every case.
    """

    def __init__(
        self,
        code: GeometryErrorCode,
        message: str,
        *,
        feature_id: str | None = None,
        parameter: str | None = None,
        actual: float | None = None,
        limit: float | None = None,
        alternatives: Sequence[tuple[str, float]] = (),
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.feature_id = feature_id
        self.parameter = parameter
        self.actual = actual
        self.limit = limit
        # Other remedies, most-preferred first, as (parameter, value) pairs — a
        # hole that does not fit can MOVE as well as SHRINK, and always taking
        # the first remedy is how three repair attempts get spent shrinking a
        # bore that needed moving.
        self.alternatives = list(alternatives)
        self.detail = detail

    def as_dict(self) -> dict:
        return {
            "code": self.code.value,
            "message": self.message,
            "feature_id": self.feature_id,
            "parameter": self.parameter,
            "actual": self.actual,
            "limit": self.limit,
            "alternatives": self.alternatives,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------- #
# the service
# --------------------------------------------------------------------------- #


@runtime_checkable
class GeometryService(Protocol):
    """Two methods. Both are pure functions of the ledger — no hidden state, no
    caching that survives a call, no session affinity. Given the same ledger
    they must produce byte-identical GLB and STEP.
    """

    def build(
        self,
        ledger: Ledger,
        *,
        exports: Sequence[ExportFormat] = ("glb", "step"),
        timeout_s: float = 20.0,
    ) -> BuildResult:
        """Translate the ledger into build123d calls, build the solid, export.

        Raises GeometryError on any failure. An empty `ledger.features` is NOT
        an error: it returns a BuildResult with zeroed stats and `glb=None`.
        """
        ...

    def probe(self, ledger: Ledger, query: SpatialQuery) -> SpatialHit | None:
        """Reverse spatial lookup: 3D point -> topological entity + the ledger
        feature responsible for it. Returns None when nothing lies within
        `query.max_distance_mm` (the caller turns that into NO_HIT).

        Rebuilds the solid internally. Milestone 1 accepts the double build;
        an LRU cache keyed on the ledger hash is the obvious later fix.
        """
        ...
