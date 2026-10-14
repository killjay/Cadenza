"""Read-only local observer endpoints; no access to evaluator sessions."""
import asyncio
import re
import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError

from cadenza_backend.config import get_settings
from cadenza_backend.diagnostics_api import local
from cadenza_backend.evaluation_observer import IDENTIFIER, MAX_GLB, MAX_JSON, Manifest

router = APIRouter(prefix="/api/evaluations")


def read(run_id: str, artifact: str | None = None) -> bytes:
    configured = get_settings().evaluation_view_root
    if configured is None: raise HTTPException(404, "Evaluation viewer is disabled")
    if not re.fullmatch(IDENTIFIER, run_id): raise HTTPException(400, "Invalid run identifier")
    root = Path(configured).resolve()
    directory = root / run_id / "observer"
    def bounded(name, limit):
        path = directory / name
        for candidate in (root / run_id, directory, path):
            if candidate.exists() and getattr(candidate.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or candidate.is_symlink():
                raise HTTPException(403, "Linked artifacts are not served")
        if not path.resolve().is_relative_to(root): raise HTTPException(403, "Artifact outside configured root")
        with path.open("rb") as stream: data = stream.read(limit + 1)
        if len(data) > limit: raise HTTPException(413, "Artifact exceeds viewer limit")
        return data
    try:
        payload = bounded("manifest.json", MAX_JSON)
        manifest = Manifest.model_validate_json(payload)
        if manifest.run_id != run_id: raise ValueError("Run mismatch")
        if artifact is None: return payload
        if not re.fullmatch(r"[0-9]+\.glb", artifact): raise HTTPException(400, "Invalid model identifier")
        names = {name for c in manifest.checkpoints for name in (c.before, c.proposal, c.accepted) if name}
        if artifact not in names: raise HTTPException(404, "Model is not published")
        return bounded(artifact, MAX_GLB)
    except FileNotFoundError as exc: raise HTTPException(404, "Run or model not published yet") from exc
    except (OSError, ValueError, ValidationError) as exc: raise HTTPException(503, "Observer artifact unavailable or invalid") from exc


@router.get("/{run_id}")
async def manifest(run_id: str, request: Request):
    local(request)
    return Response(await asyncio.to_thread(read, run_id), media_type="application/json", headers={"Cache-Control": "no-store"})


@router.get("/{run_id}/models/{artifact}")
async def model(run_id: str, artifact: str, request: Request):
    local(request)
    return Response(await asyncio.to_thread(read, run_id, artifact), media_type="model/gltf-binary", headers={"Cache-Control": "no-store"})
