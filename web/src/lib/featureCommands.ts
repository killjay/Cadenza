import type { CapabilitiesResponse, CommandOperation, Feature, FeatureCapability, Ledger, PatternCapabilitiesResponse, PatternDirection, GearCapabilitiesResponse } from "./types";

import { readGear, gearReason } from "./gear";
import { readRevolve, revolveReason } from "./revolve";

export const sourceNotice = "Source edits update all reused instances together.";

export function numericInputError(text: string, min?: number | string, max?: number | string, step: number | string = "any"): string {
  if (text === "") return ""; // Required fields use native valueMissing validation.
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(text.trim()) || !Number.isFinite(Number(text)))
    return "Enter a finite decimal number.";
  const value = Number(text);
  if (min !== undefined && value < Number(min)) return `Enter a value of at least ${min}.`;
  if (max !== undefined && value > Number(max)) return `Enter a value no greater than ${max}.`;
  if (step !== "any") {
    const steps = (value - Number(min ?? 0)) / Number(step);
    if (Math.abs(steps - Math.round(steps)) > 1e-7) return `Use increments of ${step}.`;
  }
  return "";
}

export type FeatureEditField = { key: string; label: string; value: number | null; derived: boolean;
  operation: { op: "set_dimension"; parameter: Extract<CommandOperation, { op: "set_dimension" }>["parameter"] }
    | { op: "set_origin"; axis: "x" | "y" | "z" } };

export function featureEditCommands(featureId: string, fields: FeatureEditField[], draft: Record<string, string>): CommandOperation[] {
  return fields.filter(field => !field.derived && field.key in draft).flatMap(field => {
    const text = draft[field.key], error = numericInputError(text);
    if (error) throw new Error(`${field.label}: ${error}`);
    const value = readNumber(text, field.operation.op === "set_origin");
    return value === field.value ? [] : [{ ...field.operation, feature_id: featureId, value }];
  });
}

export function requestNumericDraftDiscard(target: EventTarget, preservePrompt = false): boolean {
  return target.dispatchEvent(new CustomEvent("cadenza:discard-numeric-edits", { cancelable: true, detail: { preservePrompt } }));
}

export function simpleFeature(feature: Feature): boolean {
  switch (feature.kind) {
    case "box": case "cylinder": return !feature.parameters.through && feature.parameters.draft_angle === 0;
    case "hole": return true;
    case "extrude": return feature.parameters.amount > 0 && feature.parameters.taper === 0 && feature.parameters.twist === 0;
    default: return false;
  }
}

export function fieldBinding(ledger: Ledger, featureId: string, field: string) {
  const binding = ledger.relationships?.bindings.find(b => b.feature_id === featureId && b.target === field);
  const driver = binding && ledger.relationships?.parameters[binding.expression.trim()];
  return { expression: binding?.expression, derived: !!binding && (!driver || driver.expression !== null) };
}

export function profileTargets(ledger: Ledger): Feature[] {
  return ledger.features.filter(f => !f.suppressed && (f.kind === "profile"
    || (f.kind === "sketch" && f.parameters.height === 0)));
}

export function readNumber(value: FormDataEntryValue | null, signed = false): number {
  if (typeof value !== "string" || !value.trim()) throw new Error("Enter a number in every required field.");
  const number = Number(value);
  if (!Number.isFinite(number) || (signed ? Math.abs(number) > 1e9 : number <= 0))
    throw new Error(signed ? "Position must be a finite number between -1e9 and 1e9 mm." : "Dimensions must be finite and greater than zero.");
  return number;
}

export function creationCommand(data: FormData, capability: FeatureCapability, ledger: Ledger): CommandOperation {
  const name = String(data.get("name") ?? "").trim();
  if (!name || name.length > 100) throw new Error("Enter a name of 1 to 100 characters.");
  const operation = data.get("operation");
  if ((operation !== "add" && operation !== "subtract") || !capability.create_operations.includes(operation))
    throw new Error("Choose an available operation.");
  const number = (key: string) => readNumber(data.get(key));
  const placement = { origin: capability.kind === "extrude" ? [0, 0, 0] as [number, number, number]
    : [readNumber(data.get("x"), true), readNumber(data.get("y"), true), readNumber(data.get("z"), true)] as [number, number, number],
    rotation_deg: [0, 0, 0] as [number, number, number], relative_to: null };
  const base = { op: "add_feature", name, operation, placement } as const;
  switch (capability.kind) {
    case "box": return { ...base, kind: "box", parameters: { length: number("length"), width: number("width"), height: number("height") } };
    case "cylinder": return { ...base, kind: "cylinder", parameters: { diameter: number("diameter"), height: number("height") } };
    case "hole": {
      const through = data.get("through") === "through";
      return { ...base, kind: "hole", parameters: { diameter: number("diameter"), through, depth: through ? null : number("depth") } };
    }
    case "extrude": {
      const target = String(data.get("target_feature") ?? "");
      if (!profileTargets(ledger).some(f => f.id === target)) throw new Error("Choose an active profile in this source part.");
      return { ...base, kind: "extrude", parameters: { target_feature: target, amount: number("amount") } };
    }
  }
}

export function commandAvailable(ledger: Ledger, capabilities: CapabilitiesResponse, op: CommandOperation, patterns?: PatternCapabilitiesResponse | null, gears?: GearCapabilitiesResponse | null): boolean {
  if (ledger.assembly) return false;
  if (op.op === "add_gear" || op.op === "update_gear") {
    if (!gears) return false;
    try { readGear(Object.fromEntries(Object.entries(op).map(([k, v]) => [k, String(v)]))); } catch { return false; }
    if (op.op === "add_gear") return !ledger.features.some(f => f.kind === "gear" && !f.suppressed)
      && op.placement.rotation_deg.slice(0, 2).every(v => v === 0);
    const selected = ledger.features.find(f => f.id === op.feature_id);
    return !!selected && !selected.suppressed && !gearReason(selected, ledger)
      && !ledger.relationships?.bindings.some(b => b.feature_id === selected.id);
  }
  if (op.op === "add_revolve" || op.op === "update_revolve") {
    if (!capabilities.revolve) return false;
    try { readRevolve(op.vertices.map(row => row.map(String)), String(op.angle)); } catch { return false; }
    if (op.op === "add_revolve") return true;
    const selected = ledger.features.find(f => f.id === op.feature_id);
    return !!selected && !selected.suppressed && !revolveReason(selected)
      && !ledger.relationships?.bindings.some(binding => binding.feature_id === selected.id);
  }
  if (op.op === "add_linear_pattern" || op.op === "add_circular_pattern") {
    const seed = ledger.features.find(f => f.id === op.target_feature);
    return !!patterns && !!seed && patternSeed(seed) && patternSettingsValid(op, seed);
  }
  if (op.op === "add_feature") {
    return !!capabilities.features.find(c => c.kind === op.kind)?.create_operations.includes(op.operation)
      && (op.kind !== "extrude" || profileTargets(ledger).some(f => f.id === op.parameters.target_feature));
  }
  const feature = ledger.features.find(f => f.id === op.feature_id);
  if (feature?.kind === "gear") return !!gears && !gearReason(feature, ledger)
    && (op.op === "remove_feature" || op.op === "set_suppressed");
  if (feature?.kind === "revolve") return !!capabilities.revolve && !revolveReason(feature)
    && (op.op === "remove_feature" || op.op === "set_suppressed");
  if (feature && isPattern(feature)) {
    if (!patterns || patternReason(feature, ledger)) return false;
    if (op.op === "remove_feature" || op.op === "set_suppressed") return true;
    if (feature.suppressed) return false;
    if (op.op === "update_linear_pattern" || op.op === "update_circular_pattern")
      return feature.kind === (op.op === "update_linear_pattern" ? "linear_pattern" : "circular_pattern")
        && patternSettingsValid(op, ledger.features.find(f => f.id === feature.parameters.target_feature)!);
    return false;
  }
  if (op.op === "update_linear_pattern" || op.op === "update_circular_pattern") return false;
  const capability = capabilities.features.find(c => c.kind === feature?.kind);
  if (!feature || !capability || !simpleFeature(feature)) return false;
  if (op.op === "remove_feature") return capability.remove;
  if (op.op === "set_suppressed") return capability.suppress;
  if (feature.suppressed || !Number.isFinite(op.value)) return false;
  const field = op.op === "set_dimension" ? op.parameter : op.axis;
  if (fieldBinding(ledger, feature.id, field).derived) return false;
  if (op.op === "set_dimension") return capability.dimensions.includes(op.parameter) && op.value > 0
    && !(feature.kind === "hole" && feature.parameters.through && op.parameter === "depth");
  return capability.origin_axes.includes(op.axis) && feature.placement.relative_to === null && Math.abs(op.value) <= 1e9;
}

export const patternDirections: Record<PatternDirection, [number, number, number]> = {
  "+x": [1, 0, 0], "-x": [-1, 0, 0], "+y": [0, 1, 0], "-y": [0, -1, 0], "+z": [0, 0, 1], "-z": [0, 0, -1],
};
export type PatternFeature = Extract<Feature, { kind: "linear_pattern" | "circular_pattern" }>;
export function isPattern(feature: Feature): feature is PatternFeature {
  return feature.kind === "linear_pattern" || feature.kind === "circular_pattern";
}
export function patternSeed(feature: Feature): boolean {
  return ["box", "cylinder", "hole"].includes(feature.kind) && !feature.suppressed && simpleFeature(feature)
    && !feature.placement.relative_to && feature.placement.rotation_deg.every(v => v === 0)
    && feature.placement.origin.every(v => Number.isFinite(v) && Math.abs(v) <= 1e9);
}
export function patternRadius(pattern: PatternFeature, seed: Feature): number {
  return Math.hypot(seed.placement.origin[0] - pattern.placement.origin[0], seed.placement.origin[1] - pattern.placement.origin[1]);
}
export function patternReason(feature: PatternFeature, ledger: Ledger): string | null {
  const seed = ledger.features.find(f => f.id === feature.parameters.target_feature);
  if (!seed || !patternSeed(seed)) return "The seed must be an active, unrotated box, cylinder or hole with absolute placement.";
  if (feature.operation !== seed.operation || feature.placement.relative_to || feature.placement.rotation_deg.some(v => v !== 0))
    return "This legacy operation or pattern frame is not supported by direct controls.";
  if (!Number.isInteger(feature.parameters.count) || feature.parameters.count < 2 || feature.parameters.count > 32)
    return "Direct controls support 2 to 32 total instances.";
  if (feature.kind === "linear_pattern") {
    const axis = feature.parameters.axis;
    if (feature.placement.origin.some(v => v !== 0) || !Object.values(patternDirections).some(d => d.every((v, i) => axis[i] === v))
      || (seed.kind === "hole" && axis[2] !== 0) || !Number.isFinite(feature.parameters.spacing) || feature.parameters.spacing <= 0 || feature.parameters.spacing > 1e9)
      return "This legacy linear pattern is outside the supported single-row cardinal directions.";
  } else if (feature.parameters.sweep_angle !== 360 || feature.parameters.axis.some((v, i) => v !== [0, 0, 1][i])
    || feature.placement.origin[2] !== 0 || feature.placement.origin.some(v => !Number.isFinite(v) || Math.abs(v) > 1e9) || patternRadius(feature, seed) <= 1e-7)
    return "Direct controls require a full source-Z circle and an off-axis seed.";
  return null;
}
function patternSettingsValid(op: Extract<CommandOperation, { count: number }>, seed: Feature): boolean {
  if (!Number.isInteger(op.count) || op.count < 2 || op.count > 32) return false;
  if ("spacing" in op) return Number.isFinite(op.spacing) && op.spacing > 0 && op.spacing <= 1e9
    && !!patternDirections[op.direction] && !(seed.kind === "hole" && patternDirections[op.direction][2] !== 0);
  return op.center_xy.length === 2 && op.center_xy.every(v => Number.isFinite(v) && Math.abs(v) <= 1e9)
    && Math.hypot(seed.placement.origin[0] - op.center_xy[0], seed.placement.origin[1] - op.center_xy[1]) > 1e-7;
}
export function patternCommand(data: FormData, ledger: Ledger, feature?: PatternFeature): CommandOperation {
  const target = feature?.parameters.target_feature ?? String(data.get("target_feature") ?? "");
  const seed = ledger.features.find(f => f.id === target);
  if (!seed || !patternSeed(seed)) throw new Error("Select an eligible seed explicitly.");
  const count = readNumber(data.get("count"));
  const kind = feature?.kind ?? data.get("kind");
  const name = String(data.get("name") ?? "").trim();
  if (!feature && (!name || name.length > 100)) throw new Error("Enter a pattern name of 1 to 100 characters.");
  let op: Extract<CommandOperation, { count: number }>;
  if (kind === "linear_pattern") {
    const settings = { count, spacing: readNumber(data.get("spacing")), direction: String(data.get("direction")) as PatternDirection };
    op = feature ? { op: "update_linear_pattern", feature_id: feature.id, ...settings }
      : { op: "add_linear_pattern", name, target_feature: target, ...settings };
  } else if (kind === "circular_pattern") {
    const settings = { count, center_xy: [readNumber(data.get("center_x"), true), readNumber(data.get("center_y"), true)] as [number, number] };
    op = feature ? { op: "update_circular_pattern", feature_id: feature.id, ...settings }
      : { op: "add_circular_pattern", name, target_feature: target, ...settings };
  } else throw new Error("Choose a pattern type.");
  if (!patternSettingsValid(op, seed)) throw new Error("Use 2–32 integer total instances, positive spacing and a supported direction, or a center away from the seed.");
  return op;
}
