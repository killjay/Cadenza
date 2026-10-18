import test from "node:test";
import assert from "node:assert/strict";
import { loadModule } from "./load-module.mjs";

test("known errors explain recovery; unknown failures do not invent rollback or a cause", async () => {
  const { errorExplanation } = await loadModule("../src/lib/presentation.ts");
  assert.match(errorExplanation("LEDGER_INVALID"), /could not be applied/);
  assert.match(errorExplanation("REVISION_CONFLICT"), /refreshed model/);
  assert.match(errorExplanation("AGENT_TIMEOUT"), /AI request took too long/);
  assert.match(errorExplanation("MODEL_DISPLAY"), /does not confirm a design change/);
  assert.equal(errorExplanation("UNEXPECTED_NEW_CODE"), "The operation could not be completed. Review the technical details before trying again.");
});

test("preview names use structured accepted/candidate context and preserve dimensions and user names", async () => {
  const { previewPresentation, fieldLabel, assumptionValue } = await loadModule("../src/lib/presentation.ts");
  const ledgers = { prj_part: { project_id: "prj_part", metadata: { name: "BasePlate" }, features: [{ id: "feat_seed", name: "Offset hole" }] },
    prj_root: { project_id: "prj_root", metadata: { name: "Fixture" }, features: [], assembly: { components: [{ id: "comp_left", name: "Left" }] } } };
  const changes = [{ ledger_id: "prj_part", path: "/features/feat_new", before: null,
    after: { id: "feat_new", kind: "circular_pattern", name: "Six holes", parameters: { target_feature: "feat_seed", count: 6, radius: 30, sweep_angle: 360 } } }];
  const original = JSON.stringify({ ledgers, changes });
  const display = previewPresentation(ledgers, changes, "prj_part");
  assert.equal(display.changeLabel(changes[0]), "BasePlate / Six holes");
  const result = display.value(changes[0].after);
  assert.match(result, /Offset hole/); assert.match(result, /total instances, including original: 6/);
  assert.match(result, /360 degrees/); assert.match(result, /radius follows the seed position/);
  assert.doesNotMatch(result, /feat_seed|feat_new|radius: 30/);
  assert.equal(display.value(12, "diameter"), "12 mm");
  assert.equal(display.value("feat_missing", "target_feature"), "Unresolved feature");
  assert.equal(display.value("feat_custom_user_name", "name"), "feat_custom_user_name");
  assert.equal(fieldLabel("features/feat_seed/parameters/diameter"), "features / selected item / diameter");
  assert.equal(assumptionValue("diameter", 6), "6 mm");
  assert.equal(assumptionValue("placement.origin", [0, 0, 15]), "[0,0,15] mm");
  assert.equal(assumptionValue("custom_parameter", "prj_requested_name"), "prj_requested_name");
  assert.equal(JSON.stringify({ ledgers, changes }), original);
  const rename = { ledger_id: "prj_root", path: "/assembly/components/comp_left/name", before: "Left", after: "Base" };
  const assembly = previewPresentation(ledgers, [rename], "prj_root");
  assert.equal(assembly.changeLabel(rename), "Fixture / Base / name");
  assert.equal(assembly.value(rename.before, "name"), "Left");
  assert.equal(assembly.value("prj_part", "sub_assembly_id"), "BasePlate");
  const addition = { ledger_id: "prj_new", path: "", before: null, after: { project_id: "prj_new", metadata: { name: "Cap" }, features: [{ id: "feat_cap", name: "Cap stock" }] } };
  const created = previewPresentation(ledgers, [addition], "prj_new");
  assert.equal(created.changeLabel(addition), "Cap");
  assert.equal(created.value("feat_cap", "target_feature"), "Cap stock");
  assert.equal(previewPresentation(ledgers, [addition], "prj_root").value("prj_new", "sub_assembly_id"), "Cap");
});
