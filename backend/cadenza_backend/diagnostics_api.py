"""Local-only, bounded diagnostic downloads; no CAD state is mutated."""
import asyncio
import ipaddress
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cadenza_backend.diagnostics import IDENTIFIER, store

router = APIRouter(prefix="/api/diagnostics")


def local(request: Request) -> None:
    try:
        host = request.client.host if request.client else ""
        if not ipaddress.ip_address(host).is_loopback: raise ValueError()
        for value in (str(request.url), request.headers.get("origin", str(request.url))):
            parsed = urlsplit(value)
            if parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.username or parsed.password:
                raise ValueError()
    except ValueError as exc:
        raise HTTPException(403, "Diagnostics are restricted to local development access.") from exc


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    include_images: bool = False
    browser: list[dict] = Field(default_factory=list, max_length=200)
    browser_omitted: int = Field(default=0, ge=0, le=2**53-1)


@router.get("/status")
async def status(request: Request, response: Response):
    local(request)
    response.headers["Cache-Control"] = "no-store"
    try: return await asyncio.to_thread(store().status)
    except Exception as exc:
        store().failed(exc)
        raise HTTPException(503, "Diagnostic storage is unavailable.") from exc


@router.post("/sessions/{session_id}/export")
async def export(session_id: str, request: Request):
    local(request)
    if not IDENTIFIER.fullmatch(session_id): raise HTTPException(400, "Invalid session identifier.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 256*1024: raise HTTPException(413, "Browser evidence exceeds the export limit.")
    try: options = ExportRequest.model_validate_json(bytes(body))
    except (ValidationError, ValueError) as exc: raise HTTPException(422, "Invalid export options.") from exc
    try:
        data = await asyncio.to_thread(store().export, session_id, options.include_images, options.browser, options.browser_omitted)
    except KeyError as exc: raise HTTPException(404, "No retained diagnostics for this session.") from exc
    except Exception as exc:
        store().failed(exc)
        raise HTTPException(503, "Could not export diagnostics.") from exc
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="cadenza-{session_id}.zip"', "Cache-Control": "no-store"})
