"""The WebSocket protocol — the boundary between Dev A and Dev C.

ONE connection at `/ws`, carrying two kinds of frame:

  TEXT frames    JSON control plane. Every frame is one of the models below,
                 discriminated on `type`.
  BINARY frames  Geometry payloads (GLB, STEP). Self-describing: a 4-byte
                 big-endian header length, a UTF-8 JSON header, then the bytes.
                 See `encode_blob_frame` / `decode_blob_frame` — both sides use
                 that exact framing, and the TS mirror in ts/messages.ts.

Rules that are not negotiable:

  * `message_id` is minted by the CLIENT and echoed by every server frame that
    belongs to that turn. It is how the UI attaches a status spinner to the
    right bubble.
  * A `geometry.ready` manifest is sent BEFORE its binary frames, but the
    client MUST key incoming blobs off the binary header's (revision, artifact),
    never off arrival order.
  * The server never sends a `ledger.updated` for a revision whose geometry
    failed. Failed build => ledger is rolled back => `geometry.failed`.
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from cadenza_contracts.errors import ErrorCode
from cadenza_contracts.geometry import BBox, BuildStats, Ray, SpatialHit
from cadenza_contracts.ledger import Ledger, Vec3
from cadenza_contracts.patch import PatchOp
from cadenza_contracts.version import CONTRACT_VERSION

PROTOCOL_VERSION = CONTRACT_VERSION


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# client -> server
# --------------------------------------------------------------------------- #


class Target(_Msg):
    """A Point & Speak click, attached to a prompt.

    `point` is the world-space intersection Three.js computed against the
    rendered GLB, in ledger coordinates (mm, Z-up). The frontend converts out of
    glTF's Y-up before sending — the server never guesses an axis convention.
    """

    point: Vec3
    normal: Vec3 | None = None
    ray: Ray | None = None


class ClientHello(_Msg):
    type: Literal["client.hello"] = "client.hello"
    contract_version: str = CONTRACT_VERSION
    session_id: str | None = None  # resume an existing session, else None


MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5_000_000
"""Cap on DECODED bytes — what actually reaches the provider."""

ImageMediaType = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]


class InlineImage(_Msg):
    """A sketch or drawing attached to a prompt. Base64 in the JSON control plane.

    Not a binary frame and not a separate upload endpoint: an image is
    meaningless without the prompt it belongs to, and a second transport would
    need upload ids, a lifetime, and a story for the orphan case where the
    upload lands but the prompt never does. One frame, one turn, no lifecycle.

    Presence of any image routes the turn to the Draftsman (ARCHITECTURE.md §4,
    "a different input, the same `AgentPatchOutput`").
    """

    media_type: ImageMediaType
    data: str
    """Bare base64. A `data:` URL prefix is stripped on the way in."""
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


class UserPrompt(_Msg):
    """The one message that drives everything. `target` present => Point & Speak."""

    type: Literal["user.prompt"] = "user.prompt"
    message_id: str
    prompt: str = Field(min_length=1, max_length=4000)
    target: Target | None = None
    base_revision: int | None = None  # stale => REVISION_CONFLICT, turn refused
    images: list[InlineImage] = Field(default_factory=list, max_length=MAX_IMAGES)

    def decoded_images(self) -> list[tuple[str, bytes]]:
        """`(media_type, bytes)` pairs — the shape the agent layer consumes."""
        return [(img.media_type, img.decoded) for img in self.images]


class SpatialProbe(_Msg):
    """Hover/click preview. Pure query — never mutates the ledger."""

    type: Literal["spatial.probe"] = "spatial.probe"
    message_id: str
    point: Vec3
    ray: Ray | None = None
    prefer: Literal["face", "edge", "vertex", "any"] = "face"


class LedgerRequest(_Msg):
    """Resync after a dropped frame. Server replies with `ledger.updated`."""

    type: Literal["ledger.request"] = "ledger.request"
    message_id: str


class SessionReset(_Msg):
    type: Literal["session.reset"] = "session.reset"
    message_id: str


class Ping(_Msg):
    type: Literal["ping"] = "ping"
    t: float


ClientMessage = Annotated[
    Union[ClientHello, UserPrompt, SpatialProbe, LedgerRequest, SessionReset, Ping],
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- #
# server -> client
# --------------------------------------------------------------------------- #


class Limits(_Msg):
    max_prompt_chars: int = 4000
    max_patch_ops: int = 32
    max_features: int = 64
    build_timeout_s: float = 20.0
    max_images: int = MAX_IMAGES
    max_image_bytes: int = MAX_IMAGE_BYTES


class SessionReady(_Msg):
    type: Literal["session.ready"] = "session.ready"
    session_id: str
    contract_version: str = CONTRACT_VERSION
    ledger: Ledger
    limits: Limits = Field(default_factory=Limits)
    agents_available: bool = True  # False => no API key; UI shows a banner


AgentPhase = Literal[
    "routing",     # coordinator picking an agent
    "locating",    # reverse spatial lookup (Point & Speak only)
    "thinking",    # LLM call in flight
    "patching",    # validating + applying the patch
    "building",    # build123d
    "exporting",   # GLB / STEP
    "done",
]


class AgentStatus(_Msg):
    """Progress ticker. Purely cosmetic — never carries state the UI needs."""

    type: Literal["agent.status"] = "agent.status"
    message_id: str
    phase: AgentPhase
    detail: str | None = None
    elapsed_ms: int | None = None


class AgentMessage(_Msg):
    """What the agent says to the user. One per turn, after the work."""

    type: Literal["agent.message"] = "agent.message"
    message_id: str
    text: str
    assumptions: list[dict[str, Any]] = Field(default_factory=list)
    needs_clarification: bool = False


class LedgerUpdated(_Msg):
    """The ledger changed AND the geometry rebuilt. Both, or neither."""

    type: Literal["ledger.updated"] = "ledger.updated"
    message_id: str | None = None
    revision: int
    ledger: Ledger
    patch: list[PatchOp] = Field(default_factory=list)  # what got applied, for the UI diff
    summary: str = ""


ArtifactKind = Literal["glb", "step"]


class BlobDescriptor(_Msg):
    artifact: ArtifactKind
    content_type: str            # "model/gltf-binary" | "application/step"
    bytes: int
    sha256: str
    url: str | None = None       # HTTP fallback, e.g. "/artifacts/{rev}/model.step"


class GeometryReady(_Msg):
    """Manifest declaring the binary frames that follow."""

    type: Literal["geometry.ready"] = "geometry.ready"
    message_id: str | None = None
    revision: int
    blobs: list[BlobDescriptor]
    stats: BuildStats
    warnings: list[str] = Field(default_factory=list)


class GeometryFailed(_Msg):
    """The patch was valid but the solid would not build. The ledger has been
    ROLLED BACK; `current_revision` is what the client is still showing."""

    type: Literal["geometry.failed"] = "geometry.failed"
    message_id: str | None = None
    current_revision: int
    code: ErrorCode
    message: str
    feature_id: str | None = None
    detail: dict[str, Any] | None = None


class SpatialResult(_Msg):
    type: Literal["spatial.result"] = "spatial.result"
    message_id: str
    hit: SpatialHit | None = None  # None => nothing within tolerance


class ErrorMessage(_Msg):
    type: Literal["error"] = "error"
    message_id: str | None = None
    code: ErrorCode
    message: str
    detail: str | None = None
    recoverable: bool = True


class Pong(_Msg):
    type: Literal["pong"] = "pong"
    t: float


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

_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
_server_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)


def parse_client_message(raw: str | bytes | dict) -> ClientMessage:
    """Raises pydantic.ValidationError -> caller emits ErrorCode.BAD_MESSAGE."""
    if isinstance(raw, dict):
        return _client_adapter.validate_python(raw)
    return _client_adapter.validate_json(raw)


def parse_server_message(raw: str | bytes | dict) -> ServerMessage:
    if isinstance(raw, dict):
        return _server_adapter.validate_python(raw)
    return _server_adapter.validate_json(raw)


def dump_message(msg: BaseModel) -> str:
    """The single serialisation point. `exclude_none=False` on purpose: an
    explicit `"normal": null` is easier to debug than a missing key."""
    return msg.model_dump_json(by_alias=True)


# --------------------------------------------------------------------------- #
# binary frames
# --------------------------------------------------------------------------- #


class BlobHeader(_Msg):
    type: Literal["geometry.blob"] = "geometry.blob"
    revision: int
    artifact: ArtifactKind
    bytes: int


_HEADER_LEN = struct.Struct(">I")
MAX_BLOB_HEADER_BYTES = 4096


def encode_blob_frame(header: BlobHeader, payload: bytes) -> bytes:
    """[4-byte BE header length][UTF-8 JSON header][payload]"""
    h = header.model_copy(update={"bytes": len(payload)}).model_dump_json().encode()
    return _HEADER_LEN.pack(len(h)) + h + payload


def decode_blob_frame(frame: bytes) -> tuple[BlobHeader, bytes]:
    """Inverse of `encode_blob_frame`. Raises ValueError on a malformed frame."""
    if len(frame) < _HEADER_LEN.size:
        raise ValueError("binary frame shorter than its length prefix")
    (n,) = _HEADER_LEN.unpack_from(frame, 0)
    if n == 0 or n > MAX_BLOB_HEADER_BYTES:
        raise ValueError(f"implausible blob header length: {n}")
    start = _HEADER_LEN.size
    end = start + n
    if len(frame) < end:
        raise ValueError("binary frame truncated inside its header")
    header = BlobHeader.model_validate(json.loads(frame[start:end]))
    payload = frame[end:]
    if len(payload) != header.bytes:
        raise ValueError(
            f"blob payload is {len(payload)} bytes, header declared {header.bytes}"
        )
    return header, payload


__all__ = [
    "PROTOCOL_VERSION",
    "Target",
    "ClientHello",
    "InlineImage",
    "ImageMediaType",
    "MAX_IMAGES",
    "MAX_IMAGE_BYTES",
    "UserPrompt",
    "SpatialProbe",
    "LedgerRequest",
    "SessionReset",
    "Ping",
    "ClientMessage",
    "Limits",
    "SessionReady",
    "AgentPhase",
    "AgentStatus",
    "AgentMessage",
    "LedgerUpdated",
    "ArtifactKind",
    "BlobDescriptor",
    "GeometryReady",
    "GeometryFailed",
    "SpatialResult",
    "ErrorMessage",
    "Pong",
    "ServerMessage",
    "BBox",
    "parse_client_message",
    "parse_server_message",
    "dump_message",
    "BlobHeader",
    "encode_blob_frame",
    "decode_blob_frame",
    "MAX_BLOB_HEADER_BYTES",
]
