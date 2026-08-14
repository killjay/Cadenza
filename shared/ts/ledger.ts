/**
 * The JSON ledger — mirror of cadenza_contracts/ledger.py.
 *
 * Keep the two in lockstep; they are ONE contract expressed twice. If you edit
 * this file without editing the Python, you have created a bug that only shows
 * up at runtime, on a Friday.
 *
 * Units: millimetres, degrees. Coordinates: right-handed, Z-UP.
 * glTF is Y-up — the frontend converts at the Three.js boundary and nowhere
 * else. Anything sent back to the server is in ledger coordinates.
 */

export const SCHEMA_VERSION = 1;
export const MAX_FEATURES = 64;

export type Vec3 = [number, number, number];

export interface Metadata {
  name: string;
  workspace_mode: "3D";
  global_units: "mm";
  manufacturing_intent: string | null;
  tolerance_class: string | null;
}

export interface Placement {
  /**
   * box      — centre of the footprint at the BOTTOM face
   * cylinder — centre of the bottom circular face, axis +Z
   * hole     — where the axis pierces the face it is cut from, cut along -Z.
   *            For `through: true` holes origin[2] is IGNORED: the hole spans
   *            the whole solid and re-derives its extent at every rebuild.
   */
  origin: Vec3;
  rotation_deg: Vec3;
}

export interface TargetRef {
  mode: "coordinate";
  point: Vec3;
  normal: Vec3 | null;
  reference_feature: string | null;
}

export interface Assumption {
  field: string;
  value: unknown;
  basis: string;
  confidence: number;
}

export interface AIContext {
  original_intent: string | null;
  assumptions: Assumption[];
}

interface FeatureBase {
  id: string;
  name: string;
  suppressed: boolean;
  placement: Placement;
  target: TargetRef | null;
  ai_context: AIContext;
}

export interface BoxParameters {
  length: number; // +X
  width: number;  // +Y
  height: number; // +Z
}

export interface CylinderParameters {
  diameter: number;
  height: number;
}

export interface HoleParameters {
  diameter: number;
  through: boolean;
  depth: number | null;
}

/**
 * Standard involute spur gear. `module` and `teeth` define it; the rest a
 * machinist would quote is derived (pitch diameter = module * teeth, outer
 * diameter = module * (teeth + 2) before profile shift).
 */
export interface GearParameters {
  module: number;
  teeth: number;
  /** Face width, along +Z. */
  height: number;
  pressure_angle_deg: number;
  /** Profile shift, in modules. */
  shift: number;
}

export interface BoxFeature extends FeatureBase {
  kind: "box";
  operation: "add" | "subtract";
  parameters: BoxParameters;
}

export interface CylinderFeature extends FeatureBase {
  kind: "cylinder";
  operation: "add" | "subtract";
  parameters: CylinderParameters;
}

export interface HoleFeature extends FeatureBase {
  kind: "hole";
  operation: "subtract";
  parameters: HoleParameters;
}

export interface GearFeature extends FeatureBase {
  kind: "gear";
  operation: "add" | "subtract";
  parameters: GearParameters;
}

export type Feature = BoxFeature | CylinderFeature | HoleFeature | GearFeature;
export type FeatureKind = Feature["kind"];

export interface Ledger {
  schema_version: 1;
  project_id: string;
  /** Monotonic, server-owned. A revision that exists has been built. */
  revision: number;
  metadata: Metadata;
  /** ORDERED — order is build order and matters for booleans. */
  features: Feature[];
}

export function featureById(ledger: Ledger, id: string): Feature | undefined {
  return ledger.features.find((f) => f.id === id);
}

/** Human label for a feature, for the tree panel and the selection chip. */
export function featureLabel(f: Feature): string {
  return f.name || f.id;
}
