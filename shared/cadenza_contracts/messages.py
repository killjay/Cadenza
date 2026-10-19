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
import time
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from cadenza_contracts.errors import ErrorCode, Interference
from cadenza_contracts.geometry import BBox
from cadenza_contracts.ledger import Assumption, Ledger, Vec3
from cadenza_contracts.version import CONTRACT_VERSION
from cadenza_contracts.surface_reference import SavedSurfaceTarget, SurfaceReference
from cadenza_contracts.sketch_draft import DraftStart, DraftUpdate, DraftAction, DraftState, DraftClosed, DraftPrompt, DraftProposalDecision, DraftProposal
from cadenza_contracts.assembly_commands import AssemblyCommand, AssemblyCapabilitiesRequest, AssemblyCapabilitiesResponse
from cadenza_contracts.assembly_commands import (AssemblyPromptTarget, ComponentId, StrictModel,
    AssemblyPromptCapabilitiesRequest, AssemblyPromptCapabilitiesResponse)
from cadenza_contracts.part_commands import (PartRequest, PartCreate, PartRename, PartViewRequest, PartViewReady,
    PartCapabilitiesRequest, PartCapabilitiesResponse, OccurrencePath, RigidMatrix)
from cadenza_contracts.design_commands import DesignCommand
from cadenza_contracts.feature_capabilities import CapabilitiesRequest, CapabilitiesResponse
from cadenza_contracts.feature_capabilities import PatternCapabilitiesRequest, PatternCapabilitiesResponse

PROTOCOL_VERSION = CONTRACT_VERSION

MAX_PROMPT_CHARS = 4000
MAX_PATCH_OPS = 32
MAX_FEATURES = 64
BUILD_TIMEOUT_S = 20.0


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
    saved_reference: SavedSurfaceTarget | None = None
    surface_reference: SurfaceReference | None = None

    @model_validator(mode="after")
    def _one_identity(self) -> Target:
        if self.saved_reference is not None and self.surface_reference is not None:
            raise ValueError("Select either a saved reference or a surface identity")
        return self


class Ray(_Msg):
    origin: Vec3
    direction: Vec3


class EntityRef(_Msg):
    kind: str
    index: int
    revision: int


class SpatialHit(_Msg):
    entity: EntityRef | None = None
    point: Vec3
    normal: Vec3 | None = None
    distance_mm: float
    component_id: str | None = None
    feature_id: str | None = None
    feature_name: str | None = None
    feature_kind: str | None = None
    descriptor: str = ""
    area_mm2: float | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ClientHello(_Msg):
    type: Literal["client.hello"] = "client.hello"
    contract_version: str
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
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    targets: list[Target] = Field(default_factory=list)
    base_revision: int | None = Field(default=None, ge=0)
    active_ledger_ids: list[str] = Field(default_factory=list)
    images: list[InlineImage] = Field(default_factory=list, max_length=MAX_IMAGES)
    preview: bool = False

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


class SourceFaceContext(StrictModel):
    mode: Literal["edit_feature", "add_on_face"]
    reference: SurfaceReference
    point: list[Annotated[float, Field(allow_inf_nan=False)]] = Field(min_length=3, max_length=3)
    normal: list[Annotated[float, Field(allow_inf_nan=False)]] = Field(min_length=3, max_length=3)
    component_path: OccurrencePath | None = None

    @model_validator(mode="after")
    def local_face(self):
        if self.reference.component_path or self.reference.source_part_id is not None:
            raise ValueError("Choose a source-local face")
        if sum(v * v for v in self.normal) < 1e-12:
            raise ValueError("A nonzero surface normal is required")
        return self


class DesignPrompt(_Msg):
    type: Literal["design.prompt"] = "design.prompt"
    message_id: str = Field(min_length=1, max_length=200)
    base_revision: int = Field(strict=True, ge=0)
    ledger_id: str = Field(min_length=1, max_length=200)
    feature_id: str | None = Field(default=None, pattern=r"^feat_[a-z0-9_]{1,60}$")
    scope: Literal["source"]
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    images: list[InlineImage] = Field(default_factory=list, max_length=MAX_IMAGES)
    face: SourceFaceContext | None = None

    @model_validator(mode="after")
    def face_target(self):
        if self.face:
            ref = self.face.reference
            if ref.ledger_id != self.ledger_id or ref.source_ledger_id != self.ledger_id:
                raise ValueError("Face belongs to another source")
            if self.face.mode == "edit_feature" and self.feature_id != ref.feature_id:
                raise ValueError("Select the feature that owns this face")
            if self.face.mode == "add_on_face" and self.feature_id is not None:
                raise ValueError("Face additions cannot modify existing features")
        return self

    @field_validator("prompt")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Describe the intended addition or edit")
        return value


class AssemblyPrompt(StrictModel):
    type: Literal["design.assembly.prompt"] = "design.assembly.prompt"
    message_id: str = Field(min_length=1, max_length=200)
    base_revision: int = Field(ge=0)
    ledger_id: str = Field(min_length=1, max_length=200)
    scope: Literal["assembly_definition"]
    owner_path: list[ComponentId] = Field(max_length=8)
    target: AssemblyPromptTarget
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    images: list[InlineImage] = Field(default_factory=list, max_length=MAX_IMAGES)

    @field_validator("prompt")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Describe the intended assembly change")
        return value


class PartPrompt(PartRequest):
    type: Literal["design.part.prompt"] = "design.part.prompt"
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    images: list[InlineImage] = Field(default_factory=list, max_length=MAX_IMAGES)

    @field_validator("prompt")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Describe the new part")
        return value


class PromptCapabilitiesRequest(_Msg):
    type: Literal["design.prompt.capabilities"] = "design.prompt.capabilities"
    message_id: str = Field(min_length=1, max_length=200)


class PromptCapabilitiesResponse(_Msg):
    type: Literal["design.prompt.capabilities"] = "design.prompt.capabilities"
    message_id: str
    session_id: str
    version: Literal[1] = 1
    scope: Literal["source"] = "source"
    max_operations: Literal[32] = 32
    source_faces: Literal[True] = True


class LedgerRequest(_Msg):
    """Resync after a dropped frame. Server replies with `ledger.updated`."""

    type: Literal["ledger.request"] = "ledger.request"
    message_id: str


class LedgerCreate(_Msg):
    type: Literal["ledger.create"] = "ledger.create"
    message_id: str
    name: str = "Untitled Part"
    base_revision: int | None = Field(default=None, ge=0)


class SessionReset(_Msg):
    type: Literal["session.reset"] = "session.reset"
    message_id: str
    name: str = "Untitled Part"


class SessionUndo(_Msg):
    type: Literal["session.undo"] = "session.undo"
    message_id: str
    revision: int | None = Field(default=None, ge=0)
    base_revision: int | None = Field(default=None, ge=0)


class Ping(_Msg):
    type: Literal["ping"] = "ping"
    t: float = Field(default_factory=time.time)


class SessionRedo(_Msg):
    type: Literal["session.redo"] = "session.redo"
    message_id: str
    base_revision: int | None = Field(default=None, ge=0)


class ParameterEdit(_Msg):
    type: Literal["design.parameter"] = "design.parameter"
    message_id: str
    base_revision: int = Field(ge=0)
    ledger_id: str = Field(min_length=1, max_length=200)
    feature_id: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")
    parameter: Literal["length", "width", "height", "diameter", "depth", "amount"]
    value: float = Field(gt=0, allow_inf_nan=False)


class SketchDimensionEdit(_Msg):
    type: Literal["design.sketch_dimension"] = "design.sketch_dimension"
    message_id: str
    base_revision: int = Field(ge=0)
    ledger_id: str = Field(min_length=1, max_length=200)
    feature_id: str = Field(pattern=r"^feat_[a-z0-9_]{1,60}$")
    constraint_id: str = Field(pattern=r"^con_[a-z0-9_]{1,48}$")
    value: float = Field(allow_inf_nan=False, ge=-1e6, le=1e6)


class NamedParameterEdit(_Msg):
    type: Literal["design.named_parameter"] = "design.named_parameter"
    message_id: str
    base_revision: int = Field(ge=0)
    ledger_id: str = Field(min_length=1, max_length=200)
    parameter: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    value: float = Field(allow_inf_nan=False, ge=-1e9, le=1e9)


class PreviewDecision(_Msg):
    type: Literal["design.preview_decision"] = "design.preview_decision"
    message_id: str
    base_revision: int = Field(ge=0)
    preview_id: str = Field(min_length=1, max_length=100)
    action: Literal["apply", "cancel"]


class ClientLog(_Msg):
    type: Literal["client.log"] = "client.log"
    message_id: str | None = None
    level: str
    message: str
    data: dict[str, Any] | None = None


ClientMessage = Annotated[
    Union[
        AssemblyCommand, AssemblyCapabilitiesRequest, AssemblyPrompt, AssemblyPromptCapabilitiesRequest,
        PartCreate, PartRename, PartPrompt, PartViewRequest, PartCapabilitiesRequest,
        ClientHello,
        UserPrompt,
        SpatialProbe,
        LedgerRequest,
        LedgerCreate,
        SessionReset,
        SessionUndo,
        SessionRedo,
        Ping,
        ClientLog,
        ParameterEdit,
        NamedParameterEdit,
        SketchDimensionEdit,
        PreviewDecision,
        DraftStart,
        DraftUpdate,
        DraftAction,
        DraftPrompt,
        DraftProposalDecision,
        DesignCommand,
        CapabilitiesRequest,
        PatternCapabilitiesRequest,
        DesignPrompt,
        PromptCapabilitiesRequest,
    ],
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- #
# server -> client
# --------------------------------------------------------------------------- #


class Limits(_Msg):
    max_prompt_chars: int = MAX_PROMPT_CHARS
    max_patch_ops: int = MAX_PATCH_OPS
    max_features: int = MAX_FEATURES
    build_timeout_s: float = BUILD_TIMEOUT_S
    startup_timeout_s: float = Field(default=60.0, gt=0, le=600, allow_inf_nan=False)
    max_images: int = MAX_IMAGES
    max_image_bytes: int = MAX_IMAGE_BYTES


class SessionReady(_Msg):
    type: Literal["session.ready"] = "session.ready"
    session_id: str
    contract_version: str = CONTRACT_VERSION
    store: dict[str, Ledger]
    revision: int = 0
    can_undo: bool = False
    can_redo: bool = False
    project_id: str | None = None
    project_name: str = "Untitled Part"
    saved_revision: int | None = None
    limits: Limits = Field(default_factory=Limits)
    primary_ledger_id: str | None = None
    agents_available: bool = True  # False => no API key; UI shows a banner


class AgentPhase(str, Enum):
    ROUTING = "routing"
    LOCATING = "locating"
    THINKING = "thinking"
    PATCHING = "patching"
    BUILDING = "building"
    EXPORTING = "exporting"
    DONE = "done"


class AgentStatus(_Msg):
    """Progress ticker. Purely cosmetic — never carries state the UI needs."""

    type: Literal["agent.status"] = "agent.status"
    message_id: str
    phase: AgentPhase
    detail: str = ""
    elapsed_ms: int = 0


class AgentMessage(_Msg):
    """What the agent says to the user. One per turn, after the work."""

    type: Literal["agent.message"] = "agent.message"
    message_id: str
    text: str
    assumptions: list[Assumption] = Field(default_factory=list)
    needs_clarification: bool = False


class LedgerUpdated(_Msg):
    """The ledger changed AND the geometry rebuilt. Both, or neither."""

    type: Literal["ledger.updated"] = "ledger.updated"
    message_id: str
    ledger_id: str | None = None
    revision: int
    store: dict[str, Ledger]
    can_undo: bool = False
    can_redo: bool = False
    patch: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""


ArtifactKind = Literal["glb", "step"]


class BlobRef(_Msg):
    artifact: ArtifactKind
    content_type: str
    bytes: int
    sha256: str
    url: str


# Compatibility name retained for existing shared-package consumers.
BlobDescriptor = BlobRef


class BuildStats(_Msg):
    bbox: dict[str, list[float]] | None = None
    volume_mm3: float | None = None
    face_count: int | None = None
    build_ms: float | None = None
    export_ms: float | None = None


class GeometryReady(_Msg):
    """Manifest declaring the binary frames that follow."""

    type: Literal["geometry.ready"] = "geometry.ready"
    message_id: str
    revision: int
    blobs: list[BlobRef] = Field(default_factory=list)
    stats: BuildStats = Field(default_factory=BuildStats)
    warnings: list[str] = Field(default_factory=list)
    stub: bool = False
    empty: bool = False


class GeometryFailed(_Msg):
    """The patch was valid but the solid would not build. The ledger has been
    ROLLED BACK; `current_revision` is what the client is still showing."""

    type: Literal["geometry.failed"] = "geometry.failed"
    message_id: str
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
    interference: Interference | None = None


class PreviewChange(_Msg):
    ledger_id: str
    path: str
    before: Any = None
    after: Any = None


class AttachmentContact(_Msg):
    definition_id: str = Field(min_length=1, max_length=200)
    joint_id: str = Field(pattern=r"^joint_[a-z0-9_]{1,60}$")
    separation_mm: float = Field(ge=0, le=1e-4, allow_inf_nan=False)
    normal_residual: float = Field(ge=0, le=1e-7, allow_inf_nan=False)
    area_mm2: float = Field(gt=1e-6, allow_inf_nan=False)
    references_resolved: Literal[True]
    contact_verified: Literal[True]


class IndependenceResult(_Msg):
    component_path: OccurrencePath
    old_source_id: str = Field(min_length=1, max_length=200)
    new_source_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    copied_assemblies: int = Field(ge=0, le=8)


class PreviewReady(_Msg):
    type: Literal["design.preview"] = "design.preview"
    message_id: str
    preview_id: str
    base_revision: int
    summary: str
    prompt: str = ""
    changes: list[PreviewChange]
    checks: list[str]
    attachment_contacts: list[AttachmentContact] = Field(default_factory=list, max_length=4096)
    independence: IndependenceResult | None = None
    assumptions: list[Assumption] = Field(default_factory=list)
    glb_url: str | None = None
    step_url: str | None = None
    empty: bool = False
    display_ledger_id: str | None = None
    source_glb_url: str | None = None
    source_step_url: str | None = None
    source_empty: bool = False
    context_component_path: OccurrencePath | None = None
    source_to_project: RigidMatrix | None = None
    context_warning: str | None = None
    draft_id: str | None = None
    draft_revision: int | None = None


class PreviewClosed(_Msg):
    type: Literal["design.preview_closed"] = "design.preview_closed"
    message_id: str
    preview_id: str


class Pong(_Msg):
    type: Literal["pong"] = "pong"
    t: float = Field(default_factory=time.time)


ServerMessage = Annotated[
    Union[
        AssemblyCapabilitiesResponse, AssemblyPromptCapabilitiesResponse,
        PartViewReady, PartCapabilitiesResponse,
        SessionReady,
        CapabilitiesResponse,
        PatternCapabilitiesResponse,
        PromptCapabilitiesResponse,
        AgentStatus,
        AgentMessage,
        LedgerUpdated,
        GeometryReady,
        GeometryFailed,
        SpatialResult,
        ErrorMessage,
        Pong,
        PreviewReady,
        PreviewClosed,
        DraftState,
        DraftClosed,
        DraftProposal,
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
    "AssemblyCommand", "AssemblyCapabilitiesRequest", "AssemblyCapabilitiesResponse",
    "AssemblyPrompt", "AssemblyPromptCapabilitiesRequest", "AssemblyPromptCapabilitiesResponse",
    "PartCreate", "PartRename", "PartPrompt", "PartViewRequest", "PartViewReady",
    "PartCapabilitiesRequest", "PartCapabilitiesResponse",
    "PROTOCOL_VERSION",
    "Target",
    "Ray",
    "EntityRef",
    "SpatialHit",
    "ClientHello",
    "InlineImage",
    "ImageMediaType",
    "MAX_IMAGES",
    "MAX_IMAGE_BYTES",
    "UserPrompt",
    "SpatialProbe",
    "LedgerRequest",
    "LedgerCreate",
    "SessionReset",
    "SessionUndo",
    "SessionRedo",
    "Ping",
    "ClientLog",
    "ClientMessage",
    "DesignCommand",
    "CapabilitiesRequest",
    "CapabilitiesResponse",
    "PatternCapabilitiesRequest",
    "PatternCapabilitiesResponse",
    "DesignPrompt",
    "PromptCapabilitiesRequest",
    "PromptCapabilitiesResponse",
    "Limits",
    "SessionReady",
    "AgentPhase",
    "AgentStatus",
    "AgentMessage",
    "LedgerUpdated",
    "ArtifactKind",
    "BlobRef",
    "BlobDescriptor",
    "BuildStats",
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
