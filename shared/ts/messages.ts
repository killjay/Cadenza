/**
 * The WebSocket protocol — mirror of cadenza_contracts/messages.py.
 *
 * TEXT frames   = JSON control plane, discriminated on `type`.
 * BINARY frames = [4-byte BE header length][UTF-8 JSON header][payload bytes].
 *                 Decode with `decodeBlobFrame`.
 */

import type { BuildStats, Ray, SpatialHit } from "./geometry";
import type { Ledger, Vec3 } from "./ledger";

export const PROTOCOL_VERSION = "1.0.0";
export const CONTRACT_VERSION = "1.0.0";

export type ErrorCode =
  | "BAD_MESSAGE"
  | "PROTOCOL_MISMATCH"
  | "UNKNOWN_SESSION"
  | "BUSY"
  | "AGENT_UNAVAILABLE"
  | "AGENT_INVALID_OUTPUT"
  | "AGENT_REFUSED"
  | "AGENT_NEEDS_CLARIFICATION"
  | "AGENT_TIMEOUT"
  | "PATCH_INVALID"
  | "PATCH_FORBIDDEN_PATH"
  | "PATCH_UNKNOWN_TARGET"
  | "PATCH_APPLY_FAILED"
  | "LEDGER_INVALID"
  | "REVISION_CONFLICT"
  | "GEOMETRY_UNSUPPORTED_FEATURE"
  | "GEOMETRY_INVALID_PARAMETER"
  | "GEOMETRY_KERNEL_FAILURE"
  | "GEOMETRY_EMPTY_RESULT"
  | "GEOMETRY_EXPORT_FAILURE"
  | "GEOMETRY_TIMEOUT"
  | "NO_HIT"
  | "INTERNAL";

export interface PatchOp {
  op: "add" | "remove" | "replace" | "move" | "copy" | "test";
  path: string;
  value?: unknown;
  from?: string;
}

// ---------------------------------------------------------------------------
// client -> server
// ---------------------------------------------------------------------------

/**
 * A Point & Speak click. `point` is the Three.js raycast hit against the
 * rendered GLB, converted OUT of glTF Y-up into ledger coordinates (mm, Z-up)
 * before it is sent. The server never guesses an axis convention.
 */
export interface Target {
  point: Vec3;
  normal?: Vec3 | null;
  ray?: Ray | null;
}

export interface ClientHello {
  type: "client.hello";
  contract_version: string;
  session_id?: string | null;
}

export const MAX_IMAGES = 4;
/** Cap on DECODED bytes — what actually reaches the provider. */
export const MAX_IMAGE_BYTES = 5_000_000;

export type ImageMediaType = "image/png" | "image/jpeg" | "image/webp" | "image/gif";

/**
 * A sketch or drawing attached to a prompt. Base64 in the JSON control plane,
 * not a binary frame: an image is meaningless without the prompt it belongs to,
 * and a second transport would need upload ids, a lifetime, and a story for the
 * orphan case where the upload lands but the prompt never does.
 *
 * `data` is bare base64 — no `data:` prefix. Downscale before encoding; see
 * `web/src/lib/image.ts`.
 */
export interface InlineImage {
  media_type: ImageMediaType;
  data: string;
  name?: string | null;
}

export interface UserPrompt {
  type: "user.prompt";
  message_id: string;
  prompt: string;
  /** Present => Point & Speak edit. Absent => plain prompt. */
  target?: Target | null;
  /** Stale value => server refuses the turn with REVISION_CONFLICT. */
  base_revision?: number | null;
  /** Any image present routes the turn to the Draftsman. */
  images?: InlineImage[];
}

export interface SpatialProbe {
  type: "spatial.probe";
  message_id: string;
  point: Vec3;
  ray?: Ray | null;
  prefer?: "face" | "edge" | "vertex" | "any";
}

export interface LedgerRequest {
  type: "ledger.request";
  message_id: string;
}

export interface SessionReset {
  type: "session.reset";
  message_id: string;
}

export interface Ping {
  type: "ping";
  t: number;
}

export type ClientMessage =
  | ClientHello
  | UserPrompt
  | SpatialProbe
  | LedgerRequest
  | SessionReset
  | Ping;

// ---------------------------------------------------------------------------
// server -> client
// ---------------------------------------------------------------------------

export interface Limits {
  max_prompt_chars: number;
  max_patch_ops: number;
  max_features: number;
  build_timeout_s: number;
  max_images: number;
  max_image_bytes: number;
}

export interface SessionReady {
  type: "session.ready";
  session_id: string;
  contract_version: string;
  ledger: Ledger;
  limits: Limits;
  /** false => no API key configured; show a banner, geometry still works. */
  agents_available: boolean;
}

export type AgentPhase =
  | "routing"
  | "locating"
  | "thinking"
  | "patching"
  | "building"
  | "exporting"
  | "done";

export interface AgentStatus {
  type: "agent.status";
  message_id: string;
  phase: AgentPhase;
  detail?: string | null;
  elapsed_ms?: number | null;
}

export interface AgentMessage {
  type: "agent.message";
  message_id: string;
  text: string;
  assumptions: Array<Record<string, unknown>>;
  needs_clarification: boolean;
}

export interface LedgerUpdated {
  type: "ledger.updated";
  message_id?: string | null;
  revision: number;
  ledger: Ledger;
  patch: PatchOp[];
  summary: string;
}

export type ArtifactKind = "glb" | "step";

export interface BlobDescriptor {
  artifact: ArtifactKind;
  content_type: string;
  bytes: number;
  sha256: string;
  url?: string | null;
}

export interface GeometryReady {
  type: "geometry.ready";
  message_id?: string | null;
  revision: number;
  /** Declares the binary frames that follow. Key blobs off the BINARY header,
   *  not off arrival order. */
  blobs: BlobDescriptor[];
  stats: BuildStats;
  warnings: string[];
}

export interface GeometryFailed {
  type: "geometry.failed";
  message_id?: string | null;
  /** The ledger has been rolled back; this is what you are still showing. */
  current_revision: number;
  code: ErrorCode;
  message: string;
  feature_id?: string | null;
  detail?: Record<string, unknown> | null;
}

export interface SpatialResult {
  type: "spatial.result";
  message_id: string;
  hit: SpatialHit | null;
}

export interface ErrorMessage {
  type: "error";
  message_id?: string | null;
  code: ErrorCode;
  message: string;
  detail?: string | null;
  recoverable: boolean;
}

export interface Pong {
  type: "pong";
  t: number;
}

export type ServerMessage =
  | SessionReady
  | AgentStatus
  | AgentMessage
  | LedgerUpdated
  | GeometryReady
  | GeometryFailed
  | SpatialResult
  | ErrorMessage
  | Pong;

// ---------------------------------------------------------------------------
// binary frames
// ---------------------------------------------------------------------------

export interface BlobHeader {
  type: "geometry.blob";
  revision: number;
  artifact: ArtifactKind;
  bytes: number;
}

export const MAX_BLOB_HEADER_BYTES = 4096;

/** Inverse of the Python `encode_blob_frame`. Throws on a malformed frame. */
export function decodeBlobFrame(frame: ArrayBuffer): {
  header: BlobHeader;
  payload: ArrayBuffer;
} {
  if (frame.byteLength < 4) throw new Error("binary frame shorter than its length prefix");
  const view = new DataView(frame);
  const n = view.getUint32(0, false); // big-endian
  if (n === 0 || n > MAX_BLOB_HEADER_BYTES) throw new Error(`implausible blob header length: ${n}`);
  if (frame.byteLength < 4 + n) throw new Error("binary frame truncated inside its header");
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(frame, 4, n))) as BlobHeader;
  const payload = frame.slice(4 + n);
  if (payload.byteLength !== header.bytes) {
    throw new Error(`blob payload is ${payload.byteLength} bytes, header declared ${header.bytes}`);
  }
  return { header, payload };
}

/** Narrowing helper so `switch (msg.type)` stays exhaustive under strict TS. */
export function isServerMessage(v: unknown): v is ServerMessage {
  return typeof v === "object" && v !== null && typeof (v as { type?: unknown }).type === "string";
}
