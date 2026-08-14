/** Geometry types that reach the browser — mirror of cadenza_contracts/geometry.py. */

import type { Vec3 } from "./ledger";

export type ExportFormat = "glb" | "step";
export type EntityKind = "face" | "edge" | "vertex";

export interface BBox {
  min: Vec3;
  max: Vec3;
}

export interface BuildStats {
  bbox: BBox;
  volume_mm3: number;
  face_count: number;
  build_ms: number;
  export_ms: number;
}

export interface Ray {
  origin: Vec3;
  direction: Vec3;
}

/**
 * A topological entity in ONE revision only.
 * `index` is valid for exactly that revision. Never persist it, never send it
 * back as a selector, never cache it across a rebuild — selection is by
 * coordinate, always.
 */
export interface EntityRef {
  kind: EntityKind;
  index: number;
  revision: number;
}

export interface SpatialHit {
  entity: EntityRef;
  /** Snapped onto the exact B-rep surface (the GLB is only an approximation). */
  point: Vec3;
  normal: Vec3 | null;
  distance_mm: number;
  feature_id: string | null;
  feature_name: string | null;
  feature_kind: string | null;
  /** Human sentence — show it in the selection chip. The server also feeds it
   *  verbatim to the Machinist prompt, so what the user reads is what the
   *  model was told. */
  descriptor: string;
  area_mm2: number | null;
  confidence: number;
}

export type GeometryErrorCode =
  | "UNSUPPORTED_FEATURE"
  | "INVALID_PARAMETER"
  | "KERNEL_FAILURE"
  | "EMPTY_RESULT"
  | "EXPORT_FAILURE"
  | "TIMEOUT";
