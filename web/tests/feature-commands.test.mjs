import test from "node:test";
import assert from "node:assert/strict";
import { loadModule } from "./load-module.mjs";

const helper = await loadModule("../src/lib/featureCommands.ts");
const capability = { kind: "hole", dimensions: ["diameter", "depth"], origin_axes: ["x", "y", "z"], create_operations: ["subtract"], suppress: true, remove: true };
const hole = { id: "feat_hole", kind: "hole", suppressed: false, placement: { origin: [0, 0, 0], relative_to: null }, parameters: { diameter: 4, through: true, depth: null } };
const ledger = { features: [hole], relationships: { parameters: {}, bindings: [] } };
function data(values) { const form = new FormData(); for (const [key, value] of Object.entries(values)) form.set(key, value); return form; }

test("patterns require explicit seed, strict total count and supported settings", () => {
  const seed = { ...structuredClone(hole), operation: "subtract", placement: { origin: [20, 0, 8], rotation_deg: [0, 0, 0], relative_to: null } };
  const source = { features: [seed] };
  const values = { kind: "linear_pattern", name: "Row", target_feature: seed.id, count: "4", spacing: "10", direction: "+x" };
  const op = helper.patternCommand(data(values), source);
  assert.equal(op.op, "add_linear_pattern"); assert.equal(op.count, 4);
  for (const override of [{ count: "4.5" }, { count: "1" }, { count: "33" }, { count: "true" }, { count: "" },
    { spacing: "0" }, { spacing: "Infinity" }, { direction: "+z" }, { target_feature: "" }])
    assert.throws(() => helper.patternCommand(data({ ...values, ...override }), source));
  assert.equal(helper.commandAvailable(source, { features: [] }, op), false);
  assert.equal(helper.commandAvailable(source, { features: [] }, op, {}), true);
  const circle = { ...values, kind: "circular_pattern", center_x: "0", center_y: "0" };
  assert.equal(helper.patternCommand(data(circle), source).op, "add_circular_pattern");
  assert.throws(() => helper.patternCommand(data({ ...circle, center_x: "20" }), source));
  const pattern = { id: "feat_pattern", kind: "circular_pattern", operation: "subtract", suppressed: false,
    parameters: { target_feature: seed.id, count: 4, radius: 777, axis: [0, 0, 1], sweep_angle: 360 },
    placement: { origin: [0, 0, 0], rotation_deg: [0, 0, 0], relative_to: null } };
  source.features.push(pattern);
  assert.equal(helper.patternRadius(pattern, seed), 20);
  assert.equal(helper.patternReason(pattern, source), null);
  const update = helper.patternCommand(data({ count: "8", center_x: "5", center_y: "0" }), source, pattern);
  assert.equal(update.op, "update_circular_pattern"); assert.equal("radius" in update, false);
  assert.equal(helper.commandAvailable(source, { features: [] }, update, {}), true);
  pattern.parameters.axis = [1, 0, 0];
  assert.ok(helper.patternReason(pattern, source));
  assert.equal(helper.commandAvailable(source, { features: [] }, update, {}), false);
});

test("form numbers reject missing, nonfinite and nonpositive dimensions but accept signed positions", () => {
  for (const value of [null, "", " ", "NaN", "Infinity", "-Infinity", "0", "-2"])
    assert.throws(() => helper.readNumber(value));
  assert.equal(helper.readNumber("-3", true), -3);
  assert.equal(helper.readNumber("0", true), 0);
  assert.equal(helper.readNumber("1e-8"), 1e-8);
  assert.throws(() => helper.readNumber("1000000001", true));
});

test("creation never sends inactive blind depth or accepts arbitrary operation/profile targets", () => {
  const form = data({ name: "  Bore  ", operation: "subtract", diameter: 4, through: "through", depth: "stale", x: -3, y: 0, z: 5 });
  const op = helper.creationCommand(form, capability, ledger);
  assert.equal(op.name, "Bore"); assert.equal(op.parameters.depth, null); assert.equal(op.parameters.through, true);
  assert.equal(JSON.stringify(op.placement), JSON.stringify({ origin: [-3, 0, 5], rotation_deg: [0, 0, 0], relative_to: null }));
  form.set("through", "blind"); assert.throws(() => helper.creationCommand(form, capability, ledger));
  form.set("depth", "2"); assert.equal(helper.creationCommand(form, capability, ledger).parameters.depth, 2);
  form.set("operation", "add"); assert.throws(() => helper.creationCommand(form, capability, ledger));
  const extrusion = { ...capability, kind: "extrude", create_operations: ["add", "subtract"] };
  form.set("amount", "8"); form.set("target_feature", "feat_hole");
  assert.throws(() => helper.creationCommand(form, extrusion, ledger));
  const profiles = { ...ledger, features: [{ id: "feat_profile", kind: "profile", suppressed: false }] };
  form.set("target_feature", "feat_profile");
  const extrude = helper.creationCommand(form, extrusion, profiles);
  assert.equal(extrude.parameters.amount, 8); assert.equal(extrude.placement.origin.join(), "0,0,0");
  profiles.features[0].suppressed = true;
  assert.throws(() => helper.creationCommand(form, extrusion, profiles));
});

test("availability guards derived fields, suppressed features, through depth and relative positions", () => {
  const caps = { features: [capability] };
  const dimension = { op: "set_dimension", feature_id: hole.id, parameter: "diameter", value: 6 };
  assert.ok(helper.commandAvailable(ledger, caps, dimension));
  assert.equal(helper.commandAvailable(ledger, caps, { ...dimension, parameter: "depth" }), false);
  const bound = structuredClone(ledger);
  bound.relationships.bindings = [{ feature_id: hole.id, target: "x", expression: "offset + 3" }];
  const move = { op: "set_origin", feature_id: hole.id, axis: "x", value: -5 };
  assert.equal(helper.commandAvailable(bound, caps, move), false);
  bound.relationships.bindings[0].expression = "offset";
  bound.relationships.parameters.offset = { value: 2, expression: null };
  assert.ok(helper.commandAvailable(bound, caps, move));
  bound.features[0].placement.relative_to = "feat_parent";
  assert.equal(helper.commandAvailable(bound, caps, move), false);
  bound.features[0].suppressed = true;
  assert.equal(helper.commandAvailable(bound, caps, dimension), false);
  assert.ok(helper.commandAvailable(bound, caps, { op: "set_suppressed", feature_id: hole.id, suppressed: false }));
  assert.equal(helper.commandAvailable({ ...ledger, assembly: {} }, caps, dimension), false);
  assert.equal(helper.commandAvailable(ledger, caps, { ...dimension, feature_id: "missing" }), false);
});
