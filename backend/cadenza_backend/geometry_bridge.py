"""The seam between the server and Dev B's geometry package.

`ARCHITECTURE.md` §2 says geometry is an **in-process function call, not a
service** — no queue, no subprocess, no serialising the ledger across a boundary
— and that it can become a service later precisely because the interface is a
Protocol. This module is that Protocol's one call site.

Why a bridge rather than importing `cadenza_geometry` directly:

  * build123d is a heavyweight, compiled dependency (OCCT). A venv without it
    must still be able to run the transport, the agents and the tests. So the
    import is attempted once, at module load, and its absence degrades to
    `geometry_stub` instead of taking the process down.
  * Dev B's package raises `GeometryError` with its own enum. The server speaks
    `cadenza_contracts.ErrorCode`. The values are deliberately identical
    strings, so the mapping is `ErrorCode(exc.code.value)` — but doing that
    conversion in one place means a future divergence is one edit.

`BuildOutcome` is uniform across both backends. Callers never branch on which
one answered; they read `stub` only to label the result for the user.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from cadenza_backend import geometry_stub
from cadenza_backend.contracts_bridge import ErrorCode, Ledger

try:  # pragma: no cover - exercised by whichever venv is running
    from cadenza_geometry.contracts import GeometryError
    from cadenza_geometry.service import Build123dGeometryService

    _service: Any = Build123dGeometryService()
    REAL_GEOMETRY = True
    GEOMETRY_DETAIL = "build123d"
except Exception as exc:  # noqa: BLE001 - any import failure means "no kernel"
    _service = None
    REAL_GEOMETRY = False
    GEOMETRY_DETAIL = f"stub ({type(exc).__name__}: {exc})"
    GeometryError = ()  # type: ignore[assignment,misc]


GLB_CONTENT_TYPE = "model/gltf-binary"
STEP_CONTENT_TYPE = "application/step"


@dataclass(frozen=True)
class BuildOutcome:
    """One build attempt, in the shape the transport needs to emit it.

    `glb`/`step` are None when the stub answered — it has no kernel and
    therefore no bytes. That is exactly why `GeometryReady.stub` exists: an
    empty `blobs` list must never be mistaken for a successful real build.
    """

    ok: bool
    detail: str
    stub: bool
    glb: bytes | None = None
    step: bytes | None = None
    bbox: dict[str, list[float]] | None = None
    volume_mm3: float | None = None
    face_count: int | None = None
    build_ms: float | None = None
    warnings: list[str] = field(default_factory=list)
    code: ErrorCode | None = None
    feature_id: str | None = None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(ledger: Ledger) -> BuildOutcome:
    """Build the ledger. Never raises — every failure is a coded `BuildOutcome`."""
    if not REAL_GEOMETRY:
        stubbed = geometry_stub.build(ledger)
        bbox = None
        if stubbed.bbox:
            bbox = {"min": stubbed.bbox[:3], "max": stubbed.bbox[3:]}
        return BuildOutcome(
            ok=stubbed.ok,
            detail=stubbed.detail,
            stub=True,
            bbox=bbox,
            code=None if stubbed.ok else ErrorCode.GEOMETRY_EMPTY_RESULT,
        )

    try:
        result = _service.build(ledger)
    except GeometryError as exc:  # type: ignore[misc]
        # Dev B's enum values are the contract's wire strings by design.
        try:
            code = ErrorCode(exc.code.value)
        except ValueError:
            code = ErrorCode.GEOMETRY_KERNEL_FAILURE
        return BuildOutcome(
            ok=False,
            detail=exc.message,
            stub=False,
            code=code,
            feature_id=exc.feature_id,
        )
    except Exception as exc:  # noqa: BLE001 - a kernel crash is not a server crash
        return BuildOutcome(
            ok=False,
            detail=f"{type(exc).__name__}: {exc}",
            stub=False,
            code=ErrorCode.GEOMETRY_KERNEL_FAILURE,
        )

    return BuildOutcome(
        ok=True,
        detail=(
            f"Built {len(result.entities) if result.entities else 0} entities, "
            f"volume {result.volume:.0f} mm³."
        ),
        stub=False,
        glb=result.glb,
        step=result.step,
        bbox={"min": list(result.bbox.min), "max": list(result.bbox.max)},
        volume_mm3=result.volume,
        face_count=len(result.entities) if result.entities else None,
        build_ms=result.build_ms,
        warnings=list(result.warnings),
    )


@dataclass(frozen=True)
class ProbeOutcome:
    """What a click resolved to, plus the sentence both the user and the model see.

    `descriptor` is contract, not decoration (ARCHITECTURE.md §5.7): the same
    string goes into the user's selection chip and into the Machinist's prompt,
    so what the user reads is what the model was told.
    """

    feature_id: str | None
    feature_name: str | None
    feature_kind: str | None
    descriptor: str
    point: list[float]
    normal: list[float] | None
    distance_mm: float
    confidence: float
    stub: bool


def probe(
    ledger: Ledger,
    point: list[float] | None,
    normal: list[float] | None = None,
) -> ProbeOutcome:
    """Resolve a click to the ledger feature that produced the nearest face."""
    from cadenza_backend.spatial_stub import resolve_target

    if point is None:
        resolution = resolve_target(ledger, None)
        return ProbeOutcome(
            feature_id=None,
            feature_name=None,
            feature_kind=None,
            descriptor=resolution.semantic_context,
            point=[0.0, 0.0, 0.0],
            normal=None,
            distance_mm=0.0,
            confidence=0.0,
            stub=not REAL_GEOMETRY,
        )

    if REAL_GEOMETRY:
        try:
            hit = _service.probe_point(
                ledger,
                tuple(point),
                normal=tuple(normal) if normal else None,
            )
        except Exception:  # noqa: BLE001 - a probe failure falls back, never 500s
            hit = None
        if hit:
            feature_id = hit.get("feature_id")
            label = hit.get("label") or "face"
            kind = _kind_of(ledger, feature_id)
            name = _name_of(ledger, feature_id)
            # The descriptor goes verbatim into the model's prompt AND the user's
            # selection chip, so an unresolved kind must read as a word, not "None".
            described = name or kind or "that feature"
            of_kind = f", a {kind}" if kind else ""
            return ProbeOutcome(
                feature_id=feature_id,
                feature_name=name,
                feature_kind=kind,
                descriptor=(
                    f"The user clicked the {label.replace('_', ' ')} of feature "
                    f'`{feature_id}` ("{described}"{of_kind}). Treat that feature as '
                    "the subject of the instruction unless the wording clearly points elsewhere."
                ),
                point=list(hit.get("point_on_face") or point),
                normal=list(hit.get("normal")) if hit.get("normal") else normal,
                distance_mm=float(hit.get("distance") or 0.0),
                confidence=float(hit.get("confidence") or 1.0),
                stub=False,
            )

    resolution = resolve_target(ledger, point)
    return ProbeOutcome(
        feature_id=resolution.feature_id,
        feature_name=resolution.feature_name,
        feature_kind=_kind_of(ledger, resolution.feature_id),
        descriptor=resolution.semantic_context,
        point=list(point),
        normal=normal,
        distance_mm=resolution.distance_mm or 0.0,
        confidence=0.5 if resolution.feature_id else 0.0,
        stub=True,
    )


def _feature(ledger: Ledger, feature_id: str | None):
    if not feature_id:
        return None
    return next((f for f in ledger.features if f.id == feature_id), None)


def _kind_of(ledger: Ledger, feature_id: str | None) -> str | None:
    feature = _feature(ledger, feature_id)
    return feature.kind if feature else None


def _name_of(ledger: Ledger, feature_id: str | None) -> str | None:
    feature = _feature(ledger, feature_id)
    return (feature.name or None) if feature else None
