import test from "node:test";
import assert from "node:assert/strict";
import { loadModule } from "./load-module.mjs";

const { edgeMarginOperations } = await loadModule("../src/lib/sketchRelationships.ts");
function draft() {
  return { profile_id: "feat_profile", relationships: { parameters: {}, bindings: [], constraints: [] }, definition: {
    entities: [{ id: "ent_outer", kind: "rectangle", operation: "add", x: 10, y: 5, width: 100, height: 60 },
      { id: "ent_hole", kind: "circle", operation: "subtract", x: 0, y: 0, diameter: 6 }],
    constraints: ["x", "y", "width", "height"].map((parameter, i) => ({ id: `con_${parameter}`, kind: "dimension", entity_id: "ent_outer", parameter, value: [10, 5, 100, 60][i] }))
  } };
}
test("edge recipes preserve centre offsets and use typed new-dimension references", () => {
  for (const [edge, expression] of Object.entries({ left: "sketch_x - (sketch_width / 2 - margin)", right: "sketch_x + (sketch_width / 2 - margin)",
    top: "sketch_y + (sketch_height / 2 - margin)", bottom: "sketch_y - (sketch_height / 2 - margin)" })) {
    const state = draft(), before = structuredClone(state);
    const ops = edgeMarginOperations(state, "ent_hole", edge, "margin", 15);
    assert.deepEqual(state, before);
    assert.ok(ops.length <= 32);
    assert.ok(ops.some(o => o.op === "bind_dimension" && o.expression === expression));
    assert.deepEqual(structuredClone(ops.filter(o => o.op === "bind_dimension" && typeof o.constraint_id === "object").map(o => o.constraint_id)),
      [{ constraint_alias: "opening_x" }, { constraint_alias: "opening_y" }]);
  }
});
test("edge recipes reuse literal drivers and reject silent dependency replacement", () => {
  const state = draft();
  state.relationships.parameters.width = { unit: "mm", value: 100, expression: null };
  state.relationships.bindings.push({ feature_id: "feat_profile", constraint_id: "con_width", expression: "width" });
  const ops = edgeMarginOperations(state, "ent_hole", "left", "margin", 15);
  assert.ok(ops.some(o => o.expression === "sketch_x - (width / 2 - margin)"));
  assert.ok(!ops.some(o => o.op === "add_driver" && o.parameter === "sketch_width"));
  state.relationships.bindings[0].expression = "width * 2";
  assert.throws(() => edgeMarginOperations(state, "ent_hole", "left", "margin", 15), /derived/);
  state.relationships.bindings = [];
  state.definition.constraints.push({ kind: "coincident", entity_a: "ent_outer", entity_b: "ent_hole" });
  assert.throws(() => edgeMarginOperations(state, "ent_hole", "left", "margin", 15), /shared-centre/);
  state.definition.constraints.pop();
  assert.throws(() => edgeMarginOperations(state, "ent_hole", "left", "width", 15), /new driver/);
  assert.throws(() => edgeMarginOperations(state, "ent_hole", "left", "margin", 0), /positive/);
  state.definition.constraints.push({ id: "con_duplicate", kind: "dimension", entity_id: "ent_outer", parameter: "width", value: 100 });
  assert.throws(() => edgeMarginOperations(state, "ent_hole", "left", "margin", 15), /redundant/);
});
