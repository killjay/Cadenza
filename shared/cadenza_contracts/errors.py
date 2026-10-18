"""Shared error codes. Every `error` frame on the wire carries one of these."""

from __future__ import annotations

from enum import Enum
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field


class Interference(BaseModel):
    """Checked component pair, relative to the definition that failed to build."""
    model_config = ConfigDict(extra="forbid", strict=True)
    definition_id: str = Field(min_length=1, max_length=200)
    component_a: list[Annotated[str, Field(pattern=r"^comp_[a-z0-9_]{1,60}$")]] = Field(min_length=1, max_length=8)
    component_b: list[Annotated[str, Field(pattern=r"^comp_[a-z0-9_]{1,60}$")]] = Field(min_length=1, max_length=8)
    volume_mm3: float = Field(gt=0, allow_inf_nan=False)


class ErrorCode(str, Enum):
    # ---- transport / protocol -------------------------------------------
    BAD_MESSAGE = "BAD_MESSAGE"                     # unparseable, or unknown `type`
    PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"         # client contract major != server
    UNKNOWN_SESSION = "UNKNOWN_SESSION"
    BUSY = "BUSY"                                   # a turn is already running for this session

    # ---- agent layer -----------------------------------------------------
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"         # no API key / provider down
    AGENT_INVALID_OUTPUT = "AGENT_INVALID_OUTPUT"   # failed schema after the repair retry
    AGENT_REFUSED = "AGENT_REFUSED"                 # model declined
    AGENT_NEEDS_CLARIFICATION = "AGENT_NEEDS_CLARIFICATION"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"

    # ---- ledger / patch layer -------------------------------------------
    PATCH_INVALID = "PATCH_INVALID"                 # not RFC 6902 shaped, or over the op cap
    PATCH_FORBIDDEN_PATH = "PATCH_FORBIDDEN_PATH"   # touches a server-owned field
    PATCH_UNKNOWN_TARGET = "PATCH_UNKNOWN_TARGET"   # pointer resolves to nothing
    PATCH_APPLY_FAILED = "PATCH_APPLY_FAILED"       # jsonpatch raised
    LEDGER_INVALID = "LEDGER_INVALID"               # post-patch document fails validation
    REVISION_CONFLICT = "REVISION_CONFLICT"         # client's base_revision is stale

    # ---- geometry layer (mirror of GeometryErrorCode, re-emitted on wire) -
    GEOMETRY_UNSUPPORTED_FEATURE = "GEOMETRY_UNSUPPORTED_FEATURE"
    GEOMETRY_INVALID_PARAMETER = "GEOMETRY_INVALID_PARAMETER"
    GEOMETRY_KERNEL_FAILURE = "GEOMETRY_KERNEL_FAILURE"
    GEOMETRY_EMPTY_RESULT = "GEOMETRY_EMPTY_RESULT"
    GEOMETRY_EXPORT_FAILURE = "GEOMETRY_EXPORT_FAILURE"
    GEOMETRY_TIMEOUT = "GEOMETRY_TIMEOUT"

    # ---- spatial ---------------------------------------------------------
    NO_HIT = "NO_HIT"                               # nothing within tolerance of the click

    INTERNAL = "INTERNAL"


class CadenzaError(Exception):
    """Anything the backend surfaces to the client as an `error` frame.

    `recoverable=True` means the session is intact and the user can try again.
    `recoverable=False` means the client should reconnect / reset the session.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        detail: str | None = None,
        recoverable: bool = True,
        message_id: str | None = None,
        interference: Interference | dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.recoverable = recoverable
        self.message_id = message_id
        self.interference = Interference.model_validate(interference) if interference is not None else None
