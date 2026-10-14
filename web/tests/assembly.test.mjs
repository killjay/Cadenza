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
