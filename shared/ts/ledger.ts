/** TypeScript mirror of cadenza_contracts/ledger.py. Units are mm/degrees, Z-up. */

export const SCHEMA_VERSION = 1;
export const MAX_FEATURES = 64;

export type Vec2 = [number, number];
export type Vec3 = [number, number, number];

export interface Metadata {
  name: string;
  workspace_mode: "3D";
  global_units: "mm";
  manufacturing_intent: string | null;
  tolerance_class: string | null;
}

export interface Placement {
  origin: Vec3;
  rotation_deg: Vec3;
  relative_to: string | null;
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
  targets: TargetRef[];
  ai_context: AIContext;
}

export interface BoxParameters {
  length: number;
  width: number;
  height: number;
  through: boolean;
  draft_angle: number;
}

export interface CylinderParameters {
  diameter: number;
  height: number;
  through: boolean;
  draft_angle: number;
}

export interface HoleParameters {
  diameter: number;
  through: boolean;
  depth: number | null;
}

export interface GearParameters {
  module: number;
  teeth: number;
  height: number;
  pressure_angle_deg: number;
  shift: number;
}

export interface ConeParameters {
  bottom_diameter: number;
  top_diameter: number;
  height: number;
}

export interface SphereParameters { diameter: number; }
export interface TorusParameters { major_diameter: number; minor_diameter: number; }

export interface EdgeFilletParameters {
  target_feature: string | null;
  length: number;
  radius: number;
}

export interface EdgeChamferParameters {
  target_feature: string | null;
  length: number;
  width: number;
}

export interface LinearPatternParameters {
  target_feature: string;
  count: number;
  spacing: number;
  axis: Vec3;
}

export interface CircularPatternParameters {
  target_feature: string;
  count: number;
  radius: number; // Compatibility hint; seed position and center determine the actual radius.
  sweep_angle: number;
  axis: Vec3;
}

export interface SketchParameters {
  vertices: Vec2[];
  height: number;
  draft_angle: number;
}

export interface ProfileEntity {
  id: string; kind: "rectangle" | "circle"; operation: "add" | "subtract";
  x: number; y: number; width: number | null; height: number | null; diameter: number | null;
}
export interface DimensionConstraint {
  id: string; kind: "dimension"; entity_id: string;
  parameter: "x" | "y" | "width" | "height" | "diameter"; value: number;
}
export interface CentreConstraint {
  id: string; kind: "coincident"; entity_a: string; entity_b: string;
}
export interface ProfileSolution {
  status: "underconstrained" | "fully_constrained" | "conflicting";
  degrees_of_freedom: number; conflicts: string[]; redundant: string[];
  free_parameters: string[]; entities: ProfileEntity[];
}
export interface ProfileParameters {
  entities: ProfileEntity[]; constraints: (DimensionConstraint | CentreConstraint)[];
  solution: ProfileSolution | null;
}
export interface ProfileFeature extends FeatureBase {
  kind: "profile"; operation: "add"; parameters: ProfileParameters;
}

export interface ThreadedHoleParameters {
  designation: string;
  pitch: number | null;
  diameter: number;
  through: boolean;
  depth: number | null;
}

export interface FastenerParameters {
  designation: string;
  diameter: number;
  length: number;
  head_type: "socket" | "hex" | "flat" | "button";
}

export interface ShellParameters {
  target_feature: string | null;
  thickness: number;
  open_faces: string[];
}

export interface RevolveParameters {
  vertices: Vec2[];
  angle: number;
  axis: Vec3;
}

export interface ExtrudeParameters {
  target_feature: string;
  amount: number;
  taper: number;
  twist: number;
}

export interface EdgeDict {
  type: string;
  p1: Vec2;
  p2: Vec2;
  p3: Vec2 | null;
}

export interface LoftSection {
  vertices: Vec2[] | null;
  edges: EdgeDict[] | null;
  placement: Placement | null;
}

export interface LoftParameters { sections: LoftSection[]; }

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

export interface ConeFeature extends FeatureBase {
  kind: "cone";
  operation: "add" | "subtract";
  parameters: ConeParameters;
}

export interface SphereFeature extends FeatureBase {
  kind: "sphere";
  operation: "add" | "subtract";
  parameters: SphereParameters;
}

export interface TorusFeature extends FeatureBase {
  kind: "torus";
  operation: "add" | "subtract";
  parameters: TorusParameters;
}

export interface EdgeFilletFeature extends FeatureBase {
  kind: "edge_fillet";
  operation: "subtract";
  parameters: EdgeFilletParameters;
}

export interface EdgeChamferFeature extends FeatureBase {
  kind: "edge_chamfer";
  operation: "subtract";
  parameters: EdgeChamferParameters;
}

export interface SketchFeature extends FeatureBase {
  kind: "sketch";
  operation: "add" | "subtract";
  parameters: SketchParameters;
}

export interface LinearPatternFeature extends FeatureBase {
  kind: "linear_pattern";
  operation: "add" | "subtract";
  parameters: LinearPatternParameters;
}

export interface CircularPatternFeature extends FeatureBase {
  kind: "circular_pattern";
  operation: "add" | "subtract";
  parameters: CircularPatternParameters;
}

export interface ThreadedHoleFeature extends FeatureBase {
  kind: "threaded_hole";
  operation: "subtract";
  parameters: ThreadedHoleParameters;
}

export interface FastenerFeature extends FeatureBase {
  kind: "fastener";
  operation: "add";
  parameters: FastenerParameters;
}

export interface ShellFeature extends FeatureBase {
  kind: "shell";
  operation: "modify";
  parameters: ShellParameters;
}

export interface RevolveFeature extends FeatureBase {
  kind: "revolve";
  operation: "add" | "subtract";
  parameters: RevolveParameters;
}

export interface ExtrudeFeature extends FeatureBase {
  kind: "extrude";
  operation: "add" | "subtract";
  parameters: ExtrudeParameters;
}

export interface LoftFeature extends FeatureBase {
  kind: "loft";
  operation: "add" | "subtract";
  parameters: LoftParameters;
}

export type Feature =
  | BoxFeature | CylinderFeature | HoleFeature | GearFeature | ConeFeature
  | SphereFeature | TorusFeature | EdgeFilletFeature | EdgeChamferFeature
  | SketchFeature | ProfileFeature | LinearPatternFeature | CircularPatternFeature
  | ThreadedHoleFeature | FastenerFeature | ShellFeature | RevolveFeature
  | ExtrudeFeature | LoftFeature;

export type FeatureKind = Feature["kind"];

export interface Part {
  id: string;
  name: string;
  features: Feature[];
}

export interface Component {
  id: string;
  name: string;
  part_id: string | null;
  catalog_sku: string | null;
  sub_assembly_id: string | null;
  placement: Placement;
}

// Targets are assembly coordinates after initial placement, before joint solving.
// Runtime supports rigid A-anchors-B forests: both opposing normals, or neither
// for translation only; initial placement determines twist. Unsupported motion,
// closed loops and feature-bound coordinate anchors are rejected by geometry.
// Planar targets re-resolve selected primitive faces with recorded UV and twist.
export interface PlanarTarget { mode: "planar_face"; reference: SurfaceReference; uv: [number, number]; }

export interface Joint {
  id: string;
  kind: "rigid" | "revolute" | "cylindrical" | "slider" | "ball";
  component_a: string;
  component_b: string;
  target_a: TargetRef | PlanarTarget;
  target_b: TargetRef | PlanarTarget;
  twist_deg?: number;
}

export interface Assembly {
  parts: Part[];
  components: Component[];
  joints: Joint[];
}

export interface SurfaceReference {
  version: 1;
  ledger_id: string;
  component_path: string[];
  source_ledger_id: string;
  source_part_id: string | null;
  feature_id: string;
  feature_kind: "box" | "cylinder" | "hole" | "extrude";
  profile_id?: string | null;
  entity_id?: string | null;
  operation: "add" | "subtract";
  role: "top_face" | "bottom_face" | "side_x_pos" | "side_x_neg" | "side_y_pos" | "side_y_neg" | "lateral_face" | "bore";
}

export interface Parameter {
  unit: "mm" | "scalar";
  value: number | null;
  expression: string | null;
}
export interface ParameterBinding {
  feature_id: string;
  target: "length" | "width" | "height" | "diameter" | "depth" | "amount" | "x" | "y" | "z" | "value";
  constraint_id?: string | null;
  expression: string;
}
export interface Relationships {
  parameters: Record<string, Parameter>;
  bindings: ParameterBinding[];
  constraints: { left: string; operator: "<" | "<=" | ">" | ">="; right: string; message: string }[];
}

export interface Ledger {
  schema_version: 1;
  project_id: string;
  revision: number;
  metadata: Metadata;
  features: Feature[];
  assembly: Assembly | null;
  /** Optional on older documents; saved annotations can become unresolved. */
  surface_references?: Record<string, SurfaceReference>;
  /** Optional on older documents; numeric features are resolved by the server. */
  relationships?: Relationships;
}

export function featureById(ledger: Ledger, id: string): Feature | undefined {
  return ledger.features.find((feature) => feature.id === id);
}

export function featureLabel(feature: Feature): string {
  return feature.name || feature.id;
}
