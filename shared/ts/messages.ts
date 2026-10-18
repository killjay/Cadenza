/** TypeScript mirror of cadenza_contracts/messages.py. */

import type { Assumption, Ledger, Vec3, SurfaceReference, ProfileEntity, ProfileSolution,
  DimensionConstraint, CentreConstraint, Relationships, Placement } from "./ledger";
import type { Ray } from "./geometry";

export const PROTOCOL_VERSION = "1.0.0";
export const CONTRACT_VERSION = "1.0.0";
export const MAX_IMAGES = 4;
export const MAX_IMAGE_BYTES = 5_000_000;

export type ErrorCode =
  | "BAD_MESSAGE" | "PROTOCOL_MISMATCH" | "UNKNOWN_SESSION" | "BUSY"
  | "AGENT_UNAVAILABLE" | "AGENT_INVALID_OUTPUT" | "AGENT_REFUSED"
  | "AGENT_NEEDS_CLARIFICATION" | "AGENT_TIMEOUT" | "PATCH_INVALID"
  | "PATCH_FORBIDDEN_PATH" | "PATCH_UNKNOWN_TARGET" | "PATCH_APPLY_FAILED"
  | "LEDGER_INVALID" | "REVISION_CONFLICT" | "GEOMETRY_UNSUPPORTED_FEATURE"
  | "GEOMETRY_INVALID_PARAMETER" | "GEOMETRY_KERNEL_FAILURE"
  | "GEOMETRY_EMPTY_RESULT" | "GEOMETRY_EXPORT_FAILURE" | "GEOMETRY_TIMEOUT"
  | "NO_HIT" | "INTERNAL";

export interface Target {
  point: Vec3;
  normal?: Vec3 | null;
  ray?: Ray | null;
  saved_reference?: { ledger_id: string; reference_id: string } | null;
  surface_reference?: SurfaceReference | null;
}

export type ImageMediaType = "image/png" | "image/jpeg" | "image/webp" | "image/gif";

export interface InlineImage {
  media_type: ImageMediaType;
  data: string;
  name?: string | null;
}

export interface ClientHello {
  type: "client.hello";
  contract_version: string;
  session_id?: string | null;
}

export interface UserPrompt {
  type: "user.prompt";
  message_id: string;
  prompt: string;
  targets?: Target[];
  base_revision?: number | null;
  active_ledger_ids?: string[];
  images?: InlineImage[];
  preview?: boolean;
}

export interface SpatialProbe {
  type: "spatial.probe";
  message_id: string;
  point: Vec3;
  ray?: Ray | null;
  prefer?: "face" | "edge" | "vertex" | "any";
}

export interface LedgerRequest { type: "ledger.request"; message_id: string; }

export interface LedgerCreate {
  type: "ledger.create";
  message_id: string;
  name?: string;
  base_revision?: number | null;
}

export interface SessionReset { type: "session.reset"; message_id: string; name?: string; }

export interface SessionUndo {
  type: "session.undo";
  message_id: string;
  revision?: number | null;
  base_revision?: number | null;
}

export interface Ping { type: "ping"; t: number; }

export interface SessionRedo {
  type: "session.redo";
  message_id: string;
  base_revision?: number | null;
}

export interface ClientLog {
  type: "client.log";
  message_id?: string | null;
  level: string;
  message: string;
  data?: Record<string, unknown> | null;
}

export interface ParameterEdit {
  type: "design.parameter"; message_id: string; base_revision: number;
  ledger_id: string; feature_id: string;
  parameter: "length" | "width" | "height" | "diameter" | "depth" | "amount"; value: number;
}
export interface FeatureCapability {
  kind: "box" | "cylinder" | "hole" | "extrude";
  dimensions: ParameterEdit["parameter"][];
  origin_axes: ("x" | "y" | "z")[];
  create_operations: ("add" | "subtract")[];
  suppress: boolean;
  remove: boolean;
}
export interface CapabilitiesRequest { type: "design.capabilities"; message_id: string; }
export interface PatternCapabilitiesRequest { type: "design.pattern.capabilities"; message_id: string; }
export type PatternDirection = "+x" | "-x" | "+y" | "-y" | "+z" | "-z";
export interface PatternCapabilitiesResponse {
  type: "design.pattern.capabilities"; message_id: string; session_id: string;
  version: 1; scope: "source";
  operations: ["add_linear_pattern", "add_circular_pattern", "update_linear_pattern", "update_circular_pattern"];
  seed_kinds: ["box", "cylinder", "hole"]; directions: PatternDirection[];
  count_min: 2; count_max: 32; copy_budget: 128; radius_mode: "seed_position";
}
export interface SourceFaceContext {
  mode: "edit_feature" | "add_on_face"; reference: SurfaceReference;
  point: Vec3; normal: Vec3; component_path?: string[] | null;
}
export interface Interference {
  definition_id: string; component_a: string[]; component_b: string[]; volume_mm3: number;
}
export interface DesignPrompt {
  type: "design.prompt"; message_id: string; base_revision: number; ledger_id: string;
  feature_id?: string | null; scope: "source"; prompt: string; images?: InlineImage[];
  face?: SourceFaceContext | null;
}
export interface PromptCapabilitiesRequest { type: "design.prompt.capabilities"; message_id: string; }
export type AssemblyPromptTarget = { kind: "definition" }
  | { kind: "instance"; component_id: string; detach_joint_ids?: string[] }
  | { kind: "attachment"; component_a: string; component_b: string; joint_id?: string | null };
export interface AssemblyPrompt {
  type: "design.assembly.prompt"; message_id: string; base_revision: number; ledger_id: string;
  owner_path: string[]; scope: "assembly_definition"; target: AssemblyPromptTarget; prompt: string; images?: InlineImage[];
}
export interface AssemblyPromptCapabilitiesRequest {
  type: "design.assembly.prompt.capabilities"; message_id: string; base_revision: number;
  ledger_id: string; owner_path: string[];
}
export interface AssemblyPromptCapabilitiesResponse {
  type: "design.assembly.prompt.capabilities"; message_id: string; session_id: string; revision: number;
  ledger_id: string; owner_path: string[]; version: 1; target_modes: ("definition" | "instance" | "attachment")[];
  max_operations: 16; available: boolean; vision_available: boolean; reason: string | null;
}
export interface PromptCapabilitiesResponse {
  type: "design.prompt.capabilities"; message_id: string; session_id: string;
  version: 1; scope: "source"; max_operations: 32;
  source_faces?: true;
}
export interface CapabilitiesResponse {
  type: "design.capabilities"; message_id: string; session_id: string; version: 1;
  scope: "source"; length_unit: "mm"; features: FeatureCapability[];
}
export interface SketchDimensionEdit {
  type: "design.sketch_dimension"; message_id: string; base_revision: number;
  ledger_id: string; feature_id: string; constraint_id: string; value: number;
}
export interface NamedParameterEdit {
  type: "design.named_parameter"; message_id: string; base_revision: number;
  ledger_id: string; parameter: string; value: number;
}
export interface PreviewDecision {
  type: "design.preview_decision"; message_id: string; base_revision: number;
  preview_id: string; action: "apply" | "cancel";
}

export type DraftVariable = DimensionConstraint["parameter"];
export type EntitySeed = { operation?: "add" | "subtract"; x?: number; y?: number } & (
  { kind: "rectangle"; width: number; height: number; diameter?: null }
  | { kind: "circle"; diameter: number; width?: null; height?: null });
export type DraftEntityReference = string | { entity_alias: string };
export type DraftConstraintReference = string | { constraint_alias: string };
export type DraftOperation =
  | { op: "add_entity"; entity: EntitySeed; alias?: string | null }
  | { op: "remove_entity"; entity_id: DraftEntityReference }
  | { op: "set_seed"; entity_id: DraftEntityReference; parameter: DraftVariable; value: number }
  | { op: "add_dimension"; entity_id: DraftEntityReference; parameter: DraftVariable; value: number; alias?: string | null }
  | { op: "add_centre"; entity_a: DraftEntityReference; entity_b: DraftEntityReference }
  | { op: "remove_constraint"; constraint_id: DraftConstraintReference }
  | { op: "set_dimension"; constraint_id: DraftConstraintReference; value: number }
  | { op: "set_driver"; parameter: string; value: number }
  | { op: "add_driver"; parameter: string; value: number; unit?: "mm" | "scalar" }
  | { op: "bind_dimension"; constraint_id: DraftConstraintReference; expression: string }
  | { op: "set_extrusion"; amount: number };
export interface SketchDraft {
  id: string; base_revision: number; revision: number; ledger_id: string; profile_id: string; is_new: boolean;
  definition: { entities: ProfileEntity[]; constraints: (DimensionConstraint | CentreConstraint)[] };
  relationships: Relationships; solution: ProfileSolution | null; diagnostics: string[]; extrusion_amount: number | null;
  extrusion_id: string | null;
}
export interface DraftStart {
  type: "sketch.draft_start"; message_id: string; base_revision: number; profile_id?: string | null;
}
interface DraftRequest { message_id: string; base_revision: number; draft_id: string; draft_revision: number; }
export interface DraftUpdate extends DraftRequest { type: "sketch.draft_update"; operations: DraftOperation[]; }
export interface DraftAction extends DraftRequest { type: "sketch.draft_action"; action: "finish" | "discard"; }
export interface DraftState { type: "sketch.draft_state"; message_id: string; session_id: string; draft: SketchDraft; }
export interface DraftClosed { type: "sketch.draft_closed"; message_id: string; session_id: string; draft_id: string; }

export interface DraftPrompt extends DraftRequest {
  type: "sketch.prompt"; prompt: string; selected_entity_id?: string | null; reply_to?: string | null;
}
export interface DraftProposalDecision extends DraftRequest {
  type: "sketch.proposal_decision"; proposal_id: string; selected_entity_id?: string | null; action: "update" | "cancel";
}
export interface DraftProposal {
  type: "sketch.proposal"; message_id: string; session_id: string; draft_id: string; draft_revision: number;
  base_revision: number; selected_entity_id: string | null; proposal_id: string; summary: string; question: string | null;
  assumptions: string[]; interpretation: string[]; changes: { label: string; before: string; after: string }[]; checks: string[];
}

export interface BoxCreationParams { length: number; width: number; height: number; }
export interface CylinderCreationParams { diameter: number; height: number; }
export interface HoleCreationParams { diameter: number; depth?: number | null; through: boolean; }
export interface ExtrudeCreationParams { amount: number; target_feature: string; }
export type CommandPlacement = Omit<Placement, "relative_to"> & { relative_to: null };

export type CommandOperation =
  | { op: "add_linear_pattern"; name: string; target_feature: string; count: number; spacing: number; direction: PatternDirection }
  | { op: "add_circular_pattern"; name: string; target_feature: string; count: number; center_xy: [number, number] }
  | { op: "update_linear_pattern"; feature_id: string; count: number; spacing: number; direction: PatternDirection }
  | { op: "update_circular_pattern"; feature_id: string; count: number; center_xy: [number, number] }
  | { op: "set_dimension"; feature_id: string; parameter: "length" | "width" | "height" | "diameter" | "depth" | "amount"; value: number }
  | { op: "set_origin"; feature_id: string; axis: "x" | "y" | "z"; value: number }
  | { op: "add_feature"; kind: "box"; name: string; operation: "add" | "subtract"; placement: CommandPlacement; parameters: BoxCreationParams }
  | { op: "add_feature"; kind: "cylinder"; name: string; operation: "add" | "subtract"; placement: CommandPlacement; parameters: CylinderCreationParams }
  | { op: "add_feature"; kind: "hole"; name: string; operation: "add" | "subtract"; placement: CommandPlacement; parameters: HoleCreationParams }
  | { op: "add_feature"; kind: "extrude"; name: string; operation: "add" | "subtract"; placement: CommandPlacement; parameters: ExtrudeCreationParams }
  | { op: "set_suppressed"; feature_id: string; suppressed: boolean }
  | { op: "remove_feature"; feature_id: string };

export interface DesignCommand {
  type: "design.command";
  message_id: string;
  base_revision: number;
  ledger_id: string;
  scope: "source";
  operations: CommandOperation[];
}

export type PartOperation = Extract<CommandOperation, { op: "add_feature"; kind: "box" | "cylinder" | "hole" }>;
export interface PartCreate { type: "design.part.create"; message_id: string; base_revision: number; name: string; operations: PartOperation[]; }
export interface PartRename { type: "design.part.rename"; message_id: string; base_revision: number; ledger_id: string; name: string; }
export interface PartPrompt { type: "design.part.prompt"; message_id: string; base_revision: number; name: string; prompt: string; images: InlineImage[]; }
export interface PartViewRequest { type: "design.part.view"; message_id: string; base_revision: number; ledger_id: string; component_path?: string[] | null; }
export interface PartViewReady { type: "design.part.view_ready"; message_id: string; session_id: string; revision: number;
  ledger_id: string; view_id: string; glb_url: string | null; step_url: string | null; empty: boolean;
  component_path?: string[] | null; source_to_project?: number[] | null; context_glb_url?: string | null; context_warning?: string | null; }
export interface PartCapabilitiesRequest { type: "design.part.capabilities"; message_id: string; }
export interface PartCapabilitiesResponse { type: "design.part.capabilities"; message_id: string; session_id: string;
  version: 1; starter_kinds: ["box", "cylinder"]; max_operations: 32; max_sources: 32; source_view: true; source_context?: true; rename_source?: true; }

export type AssemblyOperation =
  | { op: "initialize_assembly"; name: string }
  | { op: "add_instance"; source_ledger_id: string; name: string; placement: AbsoluteAssemblyPlacement }
  | { op: "duplicate_instance"; component_id: string; name: string; placement: AbsoluteAssemblyPlacement }
  | { op: "set_instance_placement"; component_id: string; placement: AbsoluteAssemblyPlacement }
  | { op: "rename_instance"; component_id: string; name: string }
  | { op: "remove_instance"; component_id: string }
  | { op: "add_planar_attachment"; component_a: string; component_b: string; target_a: PlanarPick; target_b: PlanarPick }
  | { op: "add_rigid_joint"; component_a: string; component_b: string; target_a: AssemblyAnchor; target_b: AssemblyAnchor }
  | { op: "remove_joint"; joint_id: string }
  | { op: "create_subassembly"; name: string; instance_name: string; placement: AbsoluteAssemblyPlacement;
      children: { source_ledger_id: string; name: string; placement: AbsoluteAssemblyPlacement }[] };
export interface PlanarPick { reference: SurfaceReference; point: Vec3; normal: Vec3; }
export interface AssemblyAnchor { point: [number, number, number]; normal: [number, number, number] | null; }
export interface AbsoluteAssemblyPlacement { origin: [number, number, number]; rotation_deg: [number, number, number]; }
export interface AssemblyCommand { type: "design.assembly.command"; message_id: string; base_revision: number;
  ledger_id: string; owner_path?: string[]; scope: "assembly_definition"; operations: AssemblyOperation[]; }
export type AssemblyCapabilitiesRequest = { type: "design.assembly.capabilities"; message_id: string } &
  ({ version?: 1 } | { version: 2 | 3; ledger_id: string; owner_path: string[] });
export type AssemblyCapabilitiesResponse = { type: "design.assembly.capabilities"; message_id: string;
  session_id: string; revision: number; ledger_id: string; can_initialize: boolean; can_edit: boolean;
  reason: string | null; operations: string[]; max_operations: 16; max_components: 32; max_leaves: 128; max_depth: 8; } &
  ({ version: 1 } | { version: 2 | 3; owner_path: string[]; max_subassemblies: 8; max_initial_children: 16 });

export type ClientMessage =
  | AssemblyCommand | AssemblyCapabilitiesRequest | AssemblyPrompt | AssemblyPromptCapabilitiesRequest
  | PartCreate | PartRename | PartPrompt | PartViewRequest | PartCapabilitiesRequest
  | ClientHello | UserPrompt | SpatialProbe | LedgerRequest | LedgerCreate
  | SessionReset | SessionUndo | SessionRedo | Ping | ClientLog | ParameterEdit | NamedParameterEdit | SketchDimensionEdit | PreviewDecision
  | DraftStart | DraftUpdate | DraftAction | DraftPrompt | DraftProposalDecision | DesignCommand | CapabilitiesRequest
  | DesignPrompt | PromptCapabilitiesRequest | PatternCapabilitiesRequest;
export type ClientFrame = ClientMessage;

export interface Limits {
  max_prompt_chars: number;
  max_patch_ops: number;
  max_features: number;
  build_timeout_s: number;
  startup_timeout_s?: number;
  max_images: number;
  max_image_bytes: number;
}

export interface SessionReady {
  type: "session.ready";
  session_id: string;
  contract_version: string;
  store: Record<string, Ledger>;
  revision: number;
  can_undo: boolean;
  can_redo: boolean;
  project_id: string | null;
  project_name: string;
  saved_revision: number | null;
  limits: Limits;
  primary_ledger_id?: string | null;
  agents_available: boolean;
}

export type AgentPhase =
  | "routing" | "locating" | "thinking" | "patching"
  | "building" | "exporting" | "done";

export interface AgentStatus {
  type: "agent.status";
  message_id: string;
  phase: AgentPhase;
  detail: string;
  elapsed_ms: number;
}

export interface AgentMessage {
  type: "agent.message";
  message_id: string;
  text: string;
  assumptions: Assumption[];
  needs_clarification: boolean;
}

export interface PatchRecord {
  op: string;
  path: string;
  value?: unknown;
  from?: string | null;
  ledger_id?: string | null;
}

export interface LedgerUpdated {
  type: "ledger.updated";
  message_id: string;
  ledger_id?: string | null;
  revision: number;
  store: Record<string, Ledger>;
  can_undo: boolean;
  can_redo: boolean;
  patch: PatchRecord[];
  summary: string;
}

export type ArtifactKind = "glb" | "step";

export interface BlobRef {
  artifact: ArtifactKind;
  content_type: string;
  bytes: number;
  sha256: string;
  url: string;
}
export type BlobDescriptor = BlobRef;

export interface WireBuildStats {
  bbox?: Record<string, number[]> | null;
  volume_mm3?: number | null;
  face_count?: number | null;
  build_ms?: number | null;
  export_ms?: number | null;
}

export interface GeometryReady {
  type: "geometry.ready";
  message_id: string;
  revision: number;
  blobs: BlobRef[];
  stats: WireBuildStats;
  warnings: string[];
  stub: boolean;
  empty: boolean;
}

export interface GeometryFailed {
  type: "geometry.failed";
  message_id: string;
  current_revision: number;
  code: ErrorCode;
  message: string;
  feature_id?: string | null;
  detail?: Record<string, unknown> | null;
}

export interface WireEntityRef { kind: string; index: number; revision: number; }

export interface SpatialHitFrame {
  entity?: WireEntityRef | null;
  point: Vec3;
  normal?: Vec3 | null;
  distance_mm: number;
  component_id?: string | null;
  feature_id?: string | null;
  feature_name?: string | null;
  feature_kind?: string | null;
  descriptor?: string;
  area_mm2?: number | null;
  confidence?: number;
}

export interface SpatialResult {
  type: "spatial.result";
  message_id: string;
  hit: SpatialHitFrame | null;
}

export interface ErrorMessage {
  type: "error";
  message_id?: string | null;
  code: ErrorCode;
  message: string;
  detail?: string | null;
  recoverable: boolean;
  interference?: Interference | null;
}

export interface Pong { type: "pong"; t: number; }

export interface PreviewChange { ledger_id: string; path: string; before: unknown; after: unknown; }
export interface PreviewReady {
  attachment_contacts?: { definition_id: string; joint_id: string; separation_mm: number; normal_residual: number; area_mm2: number; references_resolved: true; contact_verified: true }[];
  type: "design.preview"; message_id: string; preview_id: string; base_revision: number;
  summary: string; prompt: string; changes: PreviewChange[]; checks: string[];
  assumptions: Assumption[]; glb_url: string | null; step_url: string | null; empty: boolean;
  display_ledger_id?: string | null; source_glb_url?: string | null; source_step_url?: string | null; source_empty?: boolean;
  context_component_path?: string[] | null; source_to_project?: number[] | null; context_warning?: string | null;
  draft_id?: string | null; draft_revision?: number | null;
}
export interface PreviewClosed { type: "design.preview_closed"; message_id: string; preview_id: string; }

export type ServerMessage =
  | AssemblyCapabilitiesResponse | AssemblyPromptCapabilitiesResponse
  | PartViewReady | PartCapabilitiesResponse
  | SessionReady | CapabilitiesResponse | PromptCapabilitiesResponse | PatternCapabilitiesResponse | AgentStatus | AgentMessage | LedgerUpdated | GeometryReady
  | GeometryFailed | SpatialResult | ErrorMessage | Pong | PreviewReady | PreviewClosed | DraftState | DraftClosed | DraftProposal;
export type ServerFrame = ServerMessage;

export interface BlobHeader {
  type: "geometry.blob";
  revision: number;
  artifact: ArtifactKind;
  bytes: number;
}

export const MAX_BLOB_HEADER_BYTES = 4096;

export function decodeBlobFrame(frame: ArrayBuffer): { header: BlobHeader; payload: ArrayBuffer } {
  if (frame.byteLength < 4) throw new Error("binary frame shorter than its length prefix");
  const view = new DataView(frame);
  const size = view.getUint32(0, false);
  if (size === 0 || size > MAX_BLOB_HEADER_BYTES) {
    throw new Error(`implausible blob header length: ${size}`);
  }
  if (frame.byteLength < 4 + size) throw new Error("binary frame truncated inside its header");
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(frame, 4, size))) as BlobHeader;
  const payload = frame.slice(4 + size);
  if (payload.byteLength !== header.bytes) {
    throw new Error(`blob payload is ${payload.byteLength} bytes, header declared ${header.bytes}`);
  }
  return { header, payload };
}

const SERVER_MESSAGE_TYPES = new Set([
  "design.assembly.prompt.capabilities",
  "design.assembly.capabilities",
  "design.part.capabilities", "design.part.view_ready",
  "design.pattern.capabilities",
  "design.prompt.capabilities",
  "session.ready", "agent.status", "agent.message", "ledger.updated",
  "geometry.ready", "geometry.failed", "spatial.result", "error", "pong",
  "design.preview", "design.preview_closed",
  "design.capabilities",
  "sketch.draft_state", "sketch.draft_closed", "sketch.proposal",
]);

const CLIENT_MESSAGE_TYPES = new Set([
  "design.assembly.prompt", "design.assembly.prompt.capabilities",
  "design.assembly.command",
  "design.assembly.capabilities",
  "design.part.capabilities", "design.part.create", "design.part.rename", "design.part.prompt", "design.part.view",
  "design.pattern.capabilities",
  "design.prompt", "design.prompt.capabilities",
  "client.hello", "user.prompt", "spatial.probe", "ledger.request",
  "ledger.create", "session.reset", "session.undo", "session.redo", "ping", "client.log",
  "design.parameter", "design.named_parameter", "design.sketch_dimension", "design.preview_decision",
  "sketch.draft_start", "sketch.draft_update", "sketch.draft_action", "sketch.prompt", "sketch.proposal_decision",
  "design.command",
  "design.capabilities",
]);

export function isClientMessage(value: unknown): value is ClientMessage {
  return typeof value === "object" && value !== null
    && CLIENT_MESSAGE_TYPES.has(String((value as { type?: unknown }).type));
}

export function isServerMessage(value: unknown): value is ServerMessage {
  return typeof value === "object" && value !== null
    && SERVER_MESSAGE_TYPES.has(String((value as { type?: unknown }).type));
}
