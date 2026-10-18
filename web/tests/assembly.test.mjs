import test from "node:test";
import assert from "node:assert/strict";
import { loadModule } from "./load-module.mjs";

test("assembly occurrence traversal preserves repeated nested identity and bounds malformed graphs", async () => {
  const { assemblyRows } = await loadModule("../src/lib/assembly.ts");
  const component = (id, source) => ({ id, name: id, sub_assembly_id: source });
  const ledgers = {
    root: { project_id: "root", assembly: { components: [component("comp_a", "inner"), component("comp_b", "inner")] } },
    inner: { project_id: "inner", assembly: { components: [component("comp_leaf", "part")] } },
    part: { project_id: "part", features: [] },
  };
  const rows = assemblyRows(ledgers, "root");
  assert.deepEqual(Array.from(rows, r => r.path), ["comp_a", "comp_a/comp_leaf", "comp_b", "comp_b/comp_leaf"]);
  assert.equal(rows.filter(r => r.source?.project_id === "part").length, 2);
  ledgers.inner.assembly.components.push(component("comp_cycle", "root"));
  assert.equal(assemblyRows(ledgers, "root").length, 6);
  ledgers.root.assembly.components = Array.from({ length: 100 }, (_, i) => component(`comp_${i}`, "missing"));
  assert.equal(assemblyRows(ledgers, "root").length, 32);
});

test("part usage counts placed occurrences, not source definitions or disconnected assemblies", async () => {
  const { partUsage } = await loadModule("../src/lib/assembly.ts");
  const component = (id, source) => ({ id, name: id, sub_assembly_id: source });
  const part = id => ({ project_id: id, features: [{ id: "feat_stock" }] });
  const ledgers = {
    root: { project_id: "root", assembly: { components: [component("comp_a", "inner"), component("comp_b", "inner")] } },
    inner: { project_id: "inner", assembly: { components: [component("comp_leaf", "spacer")] } },
    unused: { project_id: "unused", assembly: { components: [component("comp_other", "pattern")] } },
    spacer: part("spacer"), pattern: part("pattern"),
    empty: { project_id: "empty", features: [] },
  };
  const result = partUsage(ledgers, "root");
  assert.equal(result.uses.get("spacer"), 2);
  assert.equal(result.uses.get("inner"), 2);
  assert.deepEqual(Array.from(result.unplaced, p => p.project_id), ["pattern"]);
  ledgers.root.assembly.components.push(component("comp_pattern", "pattern"));
  assert.equal(partUsage(ledgers, "root").unplaced.length, 0);
});

test("a standalone primary is not listed as an unplaced part", async () => {
  const { partUsage } = await loadModule("../src/lib/assembly.ts");
  const ledgers = { primary: { project_id: "primary", features: [{ id: "feat_stock" }] },
    secondary: { project_id: "secondary", features: [{ id: "feat_stock" }] } };
  const result = partUsage(ledgers, "primary");
  assert.equal(result.rows.length, 0);
  assert.deepEqual(Array.from(result.unplaced, p => p.project_id), ["secondary"]);
});
