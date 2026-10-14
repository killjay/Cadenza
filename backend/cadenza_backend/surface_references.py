"""Read-only saved-surface checks and authoritative prompt target resolution."""
from fastapi import APIRouter, HTTPException
from pydantic import Field

from cadenza_contracts.ledger import Ledger
from cadenza_contracts.surface_reference import SavedSurfaceTarget, SurfaceReference
from cadenza_backend.profile_selection import profile_context
from cadenza_backend.contracts_bridge import CadenzaError, ErrorCode
from cadenza_backend.geometry_bridge import ProbeOutcome
from cadenza_backend.geometry_jobs import GeometryJobError, inspect
from cadenza_backend.session import registry


router = APIRouter()


class CheckSurface(SavedSurfaceTarget):
    base_revision: int = Field(ge=0)


async def check_saved_surface(ledgers: dict[str, Ledger], target: SavedSurfaceTarget) -> dict:
    ledger = ledgers.get(target.ledger_id)
    if ledger is None or target.reference_id not in ledger.surface_references:
        raise CadenzaError(ErrorCode.NO_HIT, "Saved surface no longer exists in this design.")
    result = await inspect(ledger, {"query_type": "resolve_surface_reference", "reference_id": target.reference_id}, ledgers)
    if "error" in result:
        raise CadenzaError(ErrorCode.GEOMETRY_KERNEL_FAILURE, "Could not check the saved surface.", detail=result["error"])
    return result


async def resolve_saved_target(ledgers: dict[str, Ledger], target: SavedSurfaceTarget) -> ProbeOutcome:
    result = await check_saved_surface(ledgers, target)
    if result["status"] != "resolved":
        raise CadenzaError(ErrorCode.NO_HIT, "Saved surface is " + result["status"] + ". Check or repair it before editing.")
    ref = ledgers[target.ledger_id].surface_references[target.reference_id]
    return _resolved_target(ledgers, ref, result, f"Saved surface {target.reference_id}")


async def resolve_surface_target(ledgers: dict[str, Ledger], ref: SurfaceReference) -> ProbeOutcome:
    ledger = ledgers.get(ref.ledger_id)
    if ledger is None:
        raise CadenzaError(ErrorCode.NO_HIT, "Selected surface owner no longer exists.")
    result = await inspect(ledger, {"query_type": "resolve_surface_reference", "reference": ref.model_dump(mode="json")}, ledgers)
    if result.get("status") != "resolved":
        raise CadenzaError(ErrorCode.NO_HIT, "Selected surface is " + result.get("status", "unavailable") + ". Select it again or repair its reference.")
    return _resolved_target(ledgers, ref, result, "Selected surface")


def _resolved_target(ledgers: dict[str, Ledger], ref: SurfaceReference, result: dict, label: str) -> ProbeOutcome:
    face = result["face"]
    component = "/".join(ref.component_path) or None
    return ProbeOutcome(feature_id=ref.feature_id, feature_name=None, feature_kind=ref.feature_kind,
        descriptor=f"{label}: {ref.role} of feature {ref.feature_id}. "
            f"Owner ledger: {ref.ledger_id}; source ledger: {ref.source_ledger_id}; source part: {ref.source_part_id}; "
            f"instance path: {component}. Editing source geometry affects all instances of that source. "
            "Use the source feature for geometry edits and the assembly component for placement edits." + profile_context(ref, ledgers),
        point=face["point"], normal=face["normal"], distance_mm=0, confidence=1, stub=False,
        component_id=component, ledger_id=ref.ledger_id)


@router.post("/api/sessions/{session_id}/surface-references/resolve")
async def check_surface(session_id: str, request: CheckSurface) -> dict:
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(404, "Session no longer exists.")
    if session.lock.locked():
        raise HTTPException(409, "Wait for the current design action to finish.")
    async with session.lock:
        if session.revision != request.base_revision:
            raise HTTPException(409, "Design changed. Check the surface again at the current revision.")
        try:
            result = await check_saved_surface(session.snapshot(), request)
        except GeometryJobError as exc:
            raise HTTPException(504 if exc.code == ErrorCode.GEOMETRY_TIMEOUT else 503, exc.message) from exc
        except CadenzaError as exc:
            raise HTTPException(404 if exc.code == ErrorCode.NO_HIT else 422, exc.message) from exc
        return {"session_id": session.id, "revision": session.revision, "result": result}
