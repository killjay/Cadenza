import type { DraftOperation, DraftConstraintReference, SketchDraft, DraftVariable } from "./types";

/** Compile the inspector's centre-to-edge recipe into ordinary atomic draft commands. */
export function edgeMarginOperations(draft: SketchDraft, entityId: string,
  edge: "left" | "right" | "top" | "bottom", name: string, distance: number): DraftOperation[] {
  const entities = draft.solution?.entities ?? draft.definition.entities;
  const outer = entities[0], opening = entities.find(e => e.id === entityId);
  if (outer?.kind !== "rectangle" || opening?.operation !== "subtract")
    throw new Error("Select a circular opening inside a rectangular outer profile.");
  if (draft.solution?.status === "conflicting") throw new Error("Repair conflicting constraints first.");
  if (!/^[a-z][a-z0-9_]{0,47}$/.test(name) || name in draft.relationships.parameters)
    throw new Error("Choose a new driver name: lowercase letters, numbers and underscores, starting with a letter (up to 48 characters).");
  if (!Number.isFinite(distance) || distance <= 0 || distance > 1e6)
    throw new Error("Centre-to-edge distance must be positive and at most 1,000,000 mm.");
  if (draft.definition.constraints.some(c => c.kind === "coincident" && [c.entity_a, c.entity_b].includes(entityId)))
    throw new Error("Remove this opening's shared-centre constraint explicitly before assigning an edge distance.");
  const operations: DraftOperation[] = [];
  const names = new Set([...Object.keys(draft.relationships.parameters), name]);
  const dimension = (id: string, parameter: DraftVariable) => {
    const found = draft.definition.constraints.filter(c => c.kind === "dimension" && c.entity_id === id && c.parameter === parameter);
    if (found.length > 1) throw new Error(`Choose a single ${parameter} dimension; remove redundant constraints first.`);
    return found[0];
  };
  const driver = (parameter: "x" | "y" | "width" | "height") => {
    const c = dimension(outer.id, parameter);
    if (!c || c.kind !== "dimension") throw new Error(`Fix the outer ${parameter} dimension first.`);
    const binding = draft.relationships.bindings.find(b => b.feature_id === draft.profile_id && b.constraint_id === c.id);
    if (binding) {
      const key = binding.expression.trim(), existing = draft.relationships.parameters[key];
      if (!existing || existing.expression !== null || existing.unit !== "mm")
        throw new Error(`Outer ${parameter} uses a derived expression; use explicit dimension bindings instead.`);
      return key;
    }
    const base = `sketch_${parameter}`;
    let key = base, suffix = 2;
    while (names.has(key)) key = `${base}_${suffix++}`;
    names.add(key);
    operations.push({ op: "add_driver", parameter: key, value: c.value },
      { op: "bind_dimension", constraint_id: c.id, expression: key });
    return key;
  };
  const x = driver("x"), y = driver("y"), horizontal = edge === "left" || edge === "right";
  const size = driver(horizontal ? "width" : "height");
  operations.push({ op: "add_driver", parameter: name, value: distance });
  for (const parameter of ["x", "y"] as const) {
    const c = dimension(entityId, parameter);
    if (c && draft.relationships.bindings.some(b => b.feature_id === draft.profile_id && b.constraint_id === c.id))
      throw new Error(`Opening ${parameter} already has a binding; edit its driver instead.`);
    let ref: DraftConstraintReference;
    if (c) ref = c.id;
    else {
      const alias = `opening_${parameter}`;
      operations.push({ op: "add_dimension", alias, entity_id: entityId, parameter, value: opening[parameter] });
      ref = { constraint_alias: alias };
    }
    const centre = parameter === "x" ? x : y;
    const offset = horizontal === (parameter === "x");
    const sign = edge === "left" || edge === "bottom" ? "-" : "+";
    operations.push({ op: "bind_dimension", constraint_id: ref,
      expression: offset ? `${centre} ${sign} (${size} / 2 - ${name})` : centre });
  }
  return operations;
}
