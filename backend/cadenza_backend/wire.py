"""AG-UI wire envelope — built to CONTRACTS.md §2.

STILL A STAND-IN, but no longer a guess: every frame, field and phase name below
is transcribed from the architect's CONTRACTS.md §2. It exists only because the
module CONTRACTS.md points at does not exist on disk yet.

Two naming mismatches to resolve with the architect (see the report):
  * CONTRACTS.md says the package is `cadenza_shared`; the code on disk is
    `cadenza_contracts`.
  * It references `cadenza_shared.messages`, `.fixtures`, `.geometry` — none of
    which are written yet.

When the real module lands, delete this file and re-point `app.py` and
`orchestrator.py` at it. They touch the envelope only through these
constructors and the three functions at the bottom.

NOT implemented here: the §2.4 binary blob framing. The contract says
`encode_blob_frame` / `decode_blob_frame` are shared, tested codecs and must not
be hand-rolled — and geometry is stubbed, so there are no blobs to send yet.
"""

from __future__ import annotations

import base64
import binascii
import time
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from cadenza_backend.contracts_bridge import Assumption, ErrorCode, Ledger

PROTOCOL_VERSION = "1.0.0"

Vec3 = Annotated[list[float], Field(min_length=3, max_length=3)]

# CONTRACTS.md §2.2 `session.ready.limits`
MAX_PROMPT_CHARS = 4000
MAX_PATCH_OPS = 32
MAX_FEATURES = 64
BUILD_TIMEOUT_S = 20.0

# Sketch upload. The cap is on DECODED bytes, because that is what actually
# reaches the provider and what a malicious client would try to inflate. The
# client downscales before sending (see web/src/lib/image.ts); this bound is
# the server refusing to be the place where that discipline is enforced.
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5_000_000

ImageMediaType = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]


class _Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# shared shapes
# --------------------------------------------------------------------------- #


class Ray(_Frame):
    origin: Vec3
    direction: Vec3


class Target(_Frame):
    """The click, already converted to ledger coordinates (mm, Z-up) by the client."""

    point: Vec3
    normal: Vec3 | None = None
    ray: Ray | None = None


class EntityRef(_Frame):
    """`index` is valid for `revision` only — never persist it, never send it back."""

    kind: str
    index: int
    revision: int


class SpatialHit(_Frame):
    entity: EntityRef | None = None
    point: Vec3
    normal: Vec3 | None = None
    distance_mm: float
    feature_id: str | None = None
    feature_name: str | None = None
    feature_kind: str | None = None
    descriptor: str = ""
    """Goes into the Machinist prompt verbatim AND into the user's selection chip."""
    area_mm2: float | None = None
    confidence: float = 0.0


class Limits(_Frame):
    max_prompt_chars: int = MAX_PROMPT_CHARS
    max_patch_ops: int = MAX_PATCH_OPS
    max_features: int = MAX_FEATURES
    build_timeout_s: float = BUILD_TIMEOUT_S
    max_images: int = MAX_IMAGES
    max_image_bytes: int = MAX_IMAGE_BYTES


class InlineImage(_Frame):
    """A sketch or drawing riding along with the prompt.

    Base64 in the JSON control plane rather than a binary frame or a separate
    upload endpoint. The reasoning: an image is meaningless without the prompt
    it belongs to, and a second transport would need upload ids, a lifetime, and
    a story for the orphan case where the upload lands but the prompt never
    does. One frame, one turn, no lifecycle — and at the sizes the client sends
    after downscaling (~200-400 KB) the encoding overhead is not the bottleneck.

    `data` is bare base64. A `data:` URL prefix is stripped on the way in so a
    client that pastes one straight from a `FileReader` is not punished for it.
    """

    media_type: ImageMediaType
    data: str
    name: str | None = None

    @field_validator("data")
    @classmethod
    def _validate_payload(cls, value: str) -> str:
        if value.startswith("data:"):
            _, _, value = value.partition(",")
        value = "".join(value.split())
        if not value:
            raise ValueError("image data is empty")
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"image data is not valid base64: {exc}") from exc
        if not decoded:
            raise ValueError("image data decoded to zero bytes")
        if len(decoded) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"image is {len(decoded)} bytes, over the {MAX_IMAGE_BYTES} byte limit"
            )
        return value

    @property
    def decoded(self) -> bytes:
        return base64.b64decode(self.data, validate=True)


class AgentPhase(str, Enum):
    """CONTRACTS.md §2.2, in order. `locating` appears on Point & Speak turns only."""

    ROUTING = "routing"
    LOCATING = "locating"
    THINKING = "thinking"
    PATCHING = "patching"
    BUILDING = "building"
    EXPORTING = "exporting"
    DONE = "done"


class BlobRef(_Frame):
    artifact: str
    content_type: str
    bytes: int
    sha256: str
    url: str


class BuildStats(_Frame):
    bbox: dict[str, list[float]] | None = None
    volume_mm3: float | None = None
    face_count: int | None = None
    build_ms: float | None = None
    export_ms: float | None = None


# --------------------------------------------------------------------------- #
# client -> server   (CONTRACTS.md §2.1)
# --------------------------------------------------------------------------- #
#
# `message_id` is minted by the CLIENT and echoed by every server frame in that
# turn. It is therefore required on every mutating/queryable frame.


class ClientHello(_Frame):
    type: Literal["client.hello"] = "client.hello"
    contract_version: str
    session_id: str | None = None


class UserPrompt(_Frame):
    type: Literal["user.prompt"] = "user.prompt"
    message_id: str
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    target: Target | None = None
    base_revision: int | None = None
    images: list[InlineImage] = Field(default_factory=list, max_length=MAX_IMAGES)
    """Sketches to read. Present => the Coordinator routes to the Draftsman."""

    def decoded_images(self) -> list[tuple[str, bytes]]:
        """`(media_type, bytes)` pairs, the shape `ModelClient` already takes."""
        return [(img.media_type, img.decoded) for img in self.images]


class SpatialProbe(_Frame):
    type: Literal["spatial.probe"] = "spatial.probe"
    message_id: str
    point: Vec3
    ray: Ray | None = None
    prefer: Literal["face", "edge", "vertex", "any"] = "face"


class LedgerRequest(_Frame):
    type: Literal["ledger.request"] = "ledger.request"
    message_id: str


class SessionReset(_Frame):
    type: Literal["session.reset"] = "session.reset"
    message_id: str
    name: str = "Untitled Part"


class Ping(_Frame):
    type: Literal["ping"] = "ping"
    t: float = Field(default_factory=time.time)


ClientMessage = Annotated[
    Union[ClientHello, UserPrompt, SpatialProbe, LedgerRequest, SessionReset, Ping],
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- #
# server -> client   (CONTRACTS.md §2.2)
# --------------------------------------------------------------------------- #


class SessionReady(_Frame):
    type: Literal["session.ready"] = "session.ready"
    session_id: str
    contract_version: str
    ledger: Ledger
    limits: Limits = Field(default_factory=Limits)
    agents_available: bool = True
    """False means no API key: the client shows a banner and keeps the viewport alive."""


class AgentStatus(_Frame):
    type: Literal["agent.status"] = "agent.status"
    message_id: str
    phase: AgentPhase
    detail: str = ""
    elapsed_ms: int = 0


class AgentMessage(_Frame):
    type: Literal["agent.message"] = "agent.message"
    message_id: str
    text: str
    assumptions: list[Assumption] = Field(default_factory=list)
    needs_clarification: bool = False
    """Per §2.2 this is a BOOL on the wire; the question itself rides in `text`."""


class LedgerUpdated(_Frame):
    """Sent only when the ledger changed AND geometry rebuilt. Both, or neither."""

    type: Literal["ledger.updated"] = "ledger.updated"
    message_id: str
    revision: int
    ledger: Ledger
    patch: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""


class GeometryReady(_Frame):
    type: Literal["geometry.ready"] = "geometry.ready"
    message_id: str
    revision: int
    blobs: list[BlobRef] = Field(default_factory=list)
    stats: BuildStats = Field(default_factory=BuildStats)
    warnings: list[str] = Field(default_factory=list)
    stub: bool = False
    """NOT in the contract. Set while geometry is stubbed so no client mistakes
    an empty `blobs` list for a real build. Remove when Dev B's service lands."""


class GeometryFailed(_Frame):
    type: Literal["geometry.failed"] = "geometry.failed"
    message_id: str
    current_revision: int
    """What the client is STILL showing. Do not clear the viewport."""
    code: ErrorCode
    message: str
    feature_id: str | None = None
    detail: dict[str, Any] | None = None


class SpatialResult(_Frame):
    type: Literal["spatial.result"] = "spatial.result"
    message_id: str
    hit: SpatialHit | None = None
    """null means nothing within tolerance — "nothing selected", not an error."""


class ErrorMessage(_Frame):
    type: Literal["error"] = "error"
    message_id: str | None = None
    code: ErrorCode
    message: str
    detail: str | None = None
    recoverable: bool = True


class Pong(_Frame):
    type: Literal["pong"] = "pong"
    t: float = Field(default_factory=time.time)


ServerMessage = Annotated[
    Union[
        SessionReady,
        AgentStatus,
        AgentMessage,
        LedgerUpdated,
        GeometryReady,
        GeometryFailed,
        SpatialResult,
        ErrorMessage,
        Pong,
    ],
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- #
# parse / dump
# --------------------------------------------------------------------------- #

_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
_server_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)


def parse_client_message(raw: str | bytes | dict[str, Any]) -> ClientMessage:
    if isinstance(raw, (str, bytes)):
        return _client_adapter.validate_json(raw)
    return _client_adapter.validate_python(raw)


def parse_server_message(raw: str | bytes | dict[str, Any]) -> ServerMessage:
    if isinstance(raw, (str, bytes)):
        return _server_adapter.validate_json(raw)
    return _server_adapter.validate_python(raw)


def dump_message(message: BaseModel) -> str:
    return message.model_dump_json()
