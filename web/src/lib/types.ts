/**
 * PROVISIONAL wire types.
 *
 * The architect owns `cadenza/contracts` and is generating a TypeScript copy
 * into `cadenza/contracts/ts`. Until that lands, this file is a hand-mirror of
 * the Python contracts (ledger.py / messages.py / errors.py) as of the day it
 * was written. It is deliberately the ONLY place in the web app that spells
 * out a wire shape — when the generated types arrive this file becomes a
 * re-export and nothing else changes.
 *
 *   export * from "@cadenza/contracts";
 *
 * Invariants copied from ledger.py and relied on by the viewport:
 *   * millimetres, always
 *   * right-handed, Z-UP (so is the three.js scene — see viewport/scene.ts)
 *   * `features` is an ordered array; order is build order
 *   * no topological ids are ever stored; selection is by coordinate
 */

export type Vec3 = [number, number, number];

/* ------------------------------------------------------------------ */
/* ledger                                                              */
/* ------------------------------------------------------------------ */

export interface Placement {
  origin: Vec3;
  rotation_deg: Vec3;
}

export interface TargetRef {
  mode: "coordinate";
  point: Vec3;
  normal?: Vec3 | null;
  reference_feature?: string | null;
}

export interface Assumption {
  field: string;
  value: unknown;
  basis: string;
  confidence: number;
}

export interface AIContext {
  original_intent?: string | null;
  assumptions: Assumption[];
}

interface FeatureBase {
  id: string;
  name: string;
  suppressed: boolean;
  placement: Placement;
  target?: TargetRef | null;
  ai_context: AIContext;
}

export interface BoxFeature extends FeatureBase {
  kind: "box";
  operation: "add" | "subtract";
  parameters: { length: number; width: number; height: number };
}

export interface CylinderFeature extends FeatureBase {
  kind: "cylinder";
  operation: "add" | "subtract";
  parameters: { diameter: number; height: number };
}

export interface HoleFeature extends FeatureBase {
  kind: "hole";
  operation: "subtract";
  parameters: { diameter: number; through: boolean; depth?: number | null };
}

export interface GearFeature extends FeatureBase {
  kind: "gear";
  operation: "add" | "subtract";
  parameters: {
    module: number;
    teeth: number;
    height: number;
    pressure_angle_deg: number;
    shift: number;
  };
}

export type Feature = BoxFeature | CylinderFeature | HoleFeature | GearFeature;

export interface Metadata {
  name: string;
  workspace_mode: "3D";
  global_units: "mm";
  manufacturing_intent?: string | null;
  tolerance_class?: string | null;
}

export interface Ledger {
  schema_version: 1;
  project_id: string;
  revision: number;
  metadata: Metadata;
  features: Feature[];
}

/* ------------------------------------------------------------------ */
/* wire envelope                                                       */
/* ------------------------------------------------------------------ */

/**
 * CONTRACTS.md §2, mirrored. Frame types are DOTTED (`user.prompt`, not
 * `user_prompt`) and every turn carries a client-minted `message_id` that each
 * server frame in that turn echoes back — that correlation is what lets the UI
 * attach a spinner to the right bubble.
 */

export interface Target {
  point: Vec3;
  normal?: Vec3 | null;
  ray?: { origin: Vec3; direction: Vec3 } | null;
}

export type ImageMediaType = "image/png" | "image/jpeg" | "image/webp" | "image/gif";

/** A sketch riding along with the prompt. `data` is bare base64, no `data:` prefix. */
export interface InlineImage {
  media_type: ImageMediaType;
  data: string;
  name?: string | null;
}

export interface Limits {
  max_prompt_chars: number;
  max_patch_ops: number;
  max_features: number;
  build_timeout_s: number;
  max_images: number;
  max_image_bytes: number;
}

export type ClientFrame =
  | { type: "client.hello"; contract_version: string; session_id?: string | null }
  | {
      type: "user.prompt";
      message_id: string;
      prompt: string;
      target?: Target | null;
      base_revision?: number | null;
      images?: InlineImage[];
    }
  | {
      type: "spatial.probe";
      message_id: string;
      point: Vec3;
      prefer?: "face" | "edge" | "vertex" | "any";
    }
  | { type: "ledger.request"; message_id: string }
  | { type: "session.reset"; message_id: string; name?: string }
  | { type: "ping"; t: number };

export interface Assumption {
  field: string;
  value: unknown;
  basis: string;
  confidence: number;
}

export interface BlobRef {
  artifact: "glb" | "step";
  content_type: string;
  bytes: number;
  sha256: string;
  url: string;
}

export interface SpatialHitFrame {
  point: Vec3;
  normal?: Vec3 | null;
  distance_mm: number;
  feature_id?: string | null;
  feature_name?: string | null;
  feature_kind?: string | null;
  descriptor: string;
  confidence: number;
}

/** Server -> client. Geometry bytes arrive as length-prefixed binary frames. */
export type ServerFrame =
  | {
      type: "session.ready";
      session_id: string;
      contract_version: string;
      ledger: Ledger;
      limits: Limits;
      agents_available: boolean;
    }
  | { type: "agent.status"; message_id: string; phase: string; detail?: string }
  | {
      type: "agent.message";
      message_id: string;
      text: string;
      assumptions?: Assumption[];
      needs_clarification?: boolean;
    }
  | {
      type: "ledger.updated";
      message_id: string;
      revision: number;
      ledger: Ledger;
      summary?: string;
    }
  | {
      type: "geometry.ready";
      message_id: string;
      revision: number;
      blobs: BlobRef[];
      warnings?: string[];
      stub?: boolean;
    }
  | {
      type: "geometry.failed";
      message_id: string;
      current_revision: number;
      code: string;
      message: string;
    }
  | { type: "spatial.result"; message_id: string; hit: SpatialHitFrame | null }
  | {
      type: "error";
      message_id?: string | null;
      code: string;
      message: string;
      detail?: string | null;
      recoverable?: boolean;
    }
  | { type: "pong"; t: number };

/* ------------------------------------------------------------------ */
/* client-side only                                                    */
/* ------------------------------------------------------------------ */

/** What a click on the model resolves to. Produced by viewport/pick.ts. */
export interface FaceSelection {
  /** OCCT face hash — exact B-rep identity, stable within one shape. */
  faceHash: number;
  /** World/model-space hit point in mm. This is the `click_coordinate`. */
  point: Vec3;
  /** Outward surface normal at the hit point. */
  normal: Vec3;
  /** Screen position of the click, for anchoring the floating prompt. */
  screen: { x: number; y: number };
  /** Exact kernel facts about the face, filled in asynchronously. */
  info?: FaceInfo;
}

export interface FaceInfo {
  faceHash: number;
  surfaceType: string;
  area: number;
  centerOfMass: Vec3;
  /** Exact normal from the surface's UV parameterisation, not the tessellation. */
  normal: Vec3 | null;
  /** Radius, when the face is a cylinder — lets the UI say "Ø10 hole". */
  radius: number | null;
}
