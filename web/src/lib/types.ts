/** Shared application contracts plus browser-only selection state. */

export * from "@cadenza/shared";

import type { Vec3, Target } from "@cadenza/shared";

export interface SurfaceCheck {
  status: "resolved" | "missing" | "ambiguous" | "unsupported" | "error";
  message?: string;
  face?: { point: Vec3; normal: Vec3; center: Vec3; area_mm2: number };
}

export type SelectionMode = "face" | "side" | "feature" | "part";

/** What a click on the model resolves to. Produced by viewport/pick.ts. */
export interface FaceSelection {
  saved_reference?: Target["saved_reference"];
  surface_reference?: Target["surface_reference"];
  selection_error?: string;
  mode: SelectionMode;
  /** OCCT face hash: exact B-rep identity, stable within one shape. */
  faceHash: number;
  feature_id?: string;
  component_id?: string;
  part_id?: string;
  label?: string;
  /** World/model-space hit point in mm. */
  point: Vec3;
  normal: Vec3;
  screen: { x: number; y: number };
  info?: FaceInfo;
}

export interface FaceInfo {
  faceHash: number;
  surfaceType: string;
  area: number;
  centerOfMass: Vec3;
  normal: Vec3 | null;
  radius: number | null;
}
