import test from "node:test";
import assert from "node:assert/strict";
import { loadModule } from "./load-module.mjs";

test("browser trail is bounded and excludes prompt and binary contents", async () => {
  const d = await loadModule("../src/lib/diagnostics.ts");
  for (let i = 0; i < 205; i++) d.recordDiagnostic("frame.sent", { message_id: String(i), prompt: "private", images: ["raw"] });
  const evidence = d.browserEvidence();
  assert.equal(evidence.events.length, 200);
  assert.equal(evidence.omitted, 5);
  assert.equal(evidence.partial, true);
  assert.equal(JSON.stringify(evidence).includes("private"), false);
  assert.equal(evidence.events[0].data.message_id, "5");
});

test("HTTP export requires explicit image inclusion and does not depend on a socket", async () => {
  let request;
  const d = await loadModule("../src/lib/diagnostics.ts", {}, { fetch: async (url, options) => {
    request = { url, options }; return { ok: true, blob: async () => "zip" };
  } });
  assert.equal(await d.diagnosticExport("old-session", false), "zip");
  assert.equal(JSON.parse(request.options.body).include_images, false);
  assert.equal(request.url, "/api/diagnostics/sessions/old-session/export");
});
