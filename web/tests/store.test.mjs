import test from "node:test";
import assert from "node:assert/strict";
import { create } from "zustand";
import { loadModule, deferred } from "./load-module.mjs";

async function setup(occtOverrides = {}, globals = {}) {
  const events = [], prompts = [], calls = [];
  const window = new EventTarget();
  for (const name of ["cadenza:glb", "cadenza:pickmesh", "cadenza:clear", "cadenza:invalidate", "cadenza:revise"])
    window.addEventListener(name, e => events.push({ name, detail: e.detail }));
  const occt = { clearModel: async () => { calls.push("clear"); },
    loadStep: async () => ({ volume: 10 }), getPickMesh: async () => ({ marker: "mesh" }),
    getWireframe: async () => ({ points: [], edgeGroups: [] }), ...occtOverrides };
  let handlers;
  const socket = { connect: h => { handlers = h; }, sendLog() {}, setShownRevision() {}, sendPrompt: p => prompts.push(p), reset() {}, undo() {}, send: frame => calls.push(frame) };
  const assembly = await loadModule("../src/lib/assembly.ts");
  const featureCommands = await loadModule("../src/lib/featureCommands.ts");
  const { useStore: store } = await loadModule("../src/lib/store.ts", {
    zustand: { create }, "../workers/occtClient": { occt }, "./image": { prepareImage() {} }, "./socket": { socket }, "./featureCommands": featureCommands, "./assembly": assembly,
  }, { window, ...globals });
  const ready = (revision = 0) => store.getState().onFrame({ type: "session.ready", session_id: "ses_test", revision,
    can_undo: revision > 0, store: { part: { revision, features: [] } }, agents_available: true });
  return { store, ready, events, prompts, calls, window, connection: state => handlers.onState(state) };
}

const emptyDraft = { id: "draft_test", base_revision: 0, revision: 0, ledger_id: "part", profile_id: "feat_draft",
  is_new: true, definition: { entities: [], constraints: [] }, relationships: { parameters: {}, bindings: [], constraints: [] },
  solution: null, diagnostics: [], extrusion_id: "feat_extrusion", extrusion_amount: null };

const boxCapability = { kind: "box", dimensions: ["length", "width", "height"], origin_axes: ["x", "y", "z"],
  create_operations: ["add", "subtract"], suppress: true, remove: true };
const commandLedger = { project_id: "part", features: [{ id: "feat_box", kind: "box", suppressed: false,
  parameters: { length: 20, width: 20, height: 10, through: false, draft_angle: 0 },
  placement: { origin: [0, 0, 0], rotation_deg: [0, 0, 0], relative_to: null } }], relationships: { parameters: {}, bindings: [] } };
const widthCommand = [{ op: "set_dimension", feature_id: "feat_box", parameter: "width", value: 30 }];

test("source face actions pin semantic source context and reject stale generations/revisions", async () => {
  const s = await promptSetup();
  const reference = { ledger_id:"part", source_ledger_id:"part", component_path:[], source_part_id:null,
    feature_id:"feat_box", feature_kind:"box", operation:"add", role:"top_face", version:1 };
  const selection = { source:{ledgerId:"part",revision:0,generation:4,componentPath:["comp_a"]},
    surface_reference:reference, mode:"face", point:[0,0,10],normal:[0,0,1],faceHash:42 };
  s.store.setState({ selectedSourceId:"part", selectedSourcePath:["comp_a"], sourceFaceAvailable:true,
    geometryGeneration:4, model:{source:"source",revision:0}, selections:[selection] });
  s.store.getState().startFacePrompt("edit_feature");
  assert.equal(s.store.getState().featurePromptTarget.face.mode,"edit_feature");
  assert.equal(s.store.getState().featurePromptTarget.featureId,"feat_box");
  assert.equal(s.store.getState().submitFeaturePrompt("Set height to 12 mm"),true);
  const request=s.calls.at(-1);
  assert.equal(request.type,"design.prompt");assert.equal(request.ledger_id,"part");
  assert.equal(request.face.reference,reference);assert.equal(request.face.faceHash,undefined);
  s.store.getState().onFrame({type:"error",message_id:request.message_id,code:"BAD_MESSAGE",message:"Try again"});
  assert.equal(s.store.getState().featurePromptTarget.face.mode,"edit_feature");
  s.store.setState({sessionRevision:1});
  assert.equal(s.store.getState().submitFeaturePrompt("Retry"),false);
  s.store.setState({sessionRevision:0,featurePromptTarget:null,selections:[{...selection,source:{...selection.source,generation:3}}]});
  s.store.getState().startFacePrompt("add_on_face");assert.equal(s.store.getState().featurePromptTarget,null);
  s.store.setState({selections:[selection]});s.store.getState().startFacePrompt("add_on_face");
  assert.equal(s.store.getState().featurePromptTarget.featureId,undefined);
  assert.equal(s.store.getState().featurePromptTarget.face.mode,"add_on_face");
});

test("interference paths are mapped from checked definitions including repeated nested uses", async () => {
  const s=await promptSetup();
  s.store.setState({primaryLedgerId:"root",ledgers:{
    root:{features:[],assembly:{components:[{id:"comp_left",sub_assembly_id:"inner"},{id:"comp_right",sub_assembly_id:"inner"}]}},
    inner:{project_id:"inner",metadata:{name:"Pair"},features:[],assembly:{components:[{id:"comp_a",name:"Plate"},{id:"comp_b",name:"Spacer"}]}}
  }});
  s.store.getState().onFrame({type:"error",message_id:"change",code:"GEOMETRY_KERNEL_FAILURE",message:"Overlap",
    interference:{definition_id:"inner",component_a:["comp_a"],component_b:["comp_b"],volume_mm3:12.5}});
  assert.deepEqual(Array.from(s.store.getState().interference.paths),["comp_left/comp_a","comp_left/comp_b","comp_right/comp_a","comp_right/comp_b"]);
  assert.match(s.store.getState().interference.text,/Plate overlaps Spacer/);
  assert.match(s.store.getState().interference.text,/12.50/);
  s.store.getState().onFrame({type:"ledger.updated",revision:1,store:s.store.getState().ledgers});
  assert.equal(s.store.getState().interference,null);
});

async function commandSetup() {
  const s = await setup(); s.store.getState().connect(); s.connection("open"); s.ready();
  s.store.setState({ ledgers: { part: structuredClone(commandLedger) } });
  const response = { type: "design.capabilities", message_id: s.store.getState().capabilityRequest,
    session_id: "ses_test", version: 1, scope: "source", length_unit: "mm", features: [boxCapability] };
  return { ...s, response };
}

async function promptSetup() {
  const s = await commandSetup();
  s.store.getState().onFrame(s.response);
  s.store.getState().onFrame({ type: "design.prompt.capabilities", message_id: s.store.getState().featurePromptDiscovery,
    session_id: "ses_test", version: 1, scope: "source", max_operations: 32 });
  return s;
}

async function partSetup(globals = {}) {
  const s = await setup({}, globals); s.store.getState().connect(); s.connection("open"); s.ready();
  s.store.setState({ partCapabilities: { version: 1, source_view: true }, primaryLedgerId: "root",
    ledgers: { root: { project_id: "root", features: [] },
      part: { ...structuredClone(commandLedger), metadata: { name: "Plate" } },
      second: { ...structuredClone(commandLedger), project_id: "second", metadata: { name: "Spacer" } } } });
  return s;
}

function viewResponse(s, ledger = "part", overrides = {}) {
  return { type: "design.part.view_ready", message_id: s.store.getState().sourceRequest.id,
    session_id: "ses_test", revision: 0, ledger_id: ledger, view_id: "view", empty: false,
    glb_url: "/source.glb", step_url: null, ...overrides };
}

async function contextSetup(globals = {}) {
  const s = await partSetup(globals), state = s.store.getState();
  s.store.setState({ partCapabilities: { ...state.partCapabilities, source_context: true },
    ledgers: { ...state.ledgers, root: { ...state.ledgers.root, assembly: { components: [
      { id: "comp_left", sub_assembly_id: "part" }, { id: "comp_right", sub_assembly_id: "part" }] } } } });
  return s;
}

test("source occurrence requests queue and correlate two uses of the same part, including A-B-A", async () => {
  const s = await contextSetup({ fetch: async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(1) }) });
  const choose = path => s.store.getState().selectSource("part", false, [path]);
  choose("comp_left");const first = viewResponse(s, "part", { component_path: ["comp_left"] });
  choose("comp_right");await s.store.getState().loadSourceView(first);
  assert.deepEqual(s.calls.filter(c => c.type === "design.part.view").at(-1).component_path, ["comp_right"]);
  assert.equal(s.events.filter(e => e.name === "cadenza:glb").length, 0);
  const second = viewResponse(s, "part", { component_path: ["comp_right"] });
  await s.store.getState().loadSourceView(second);
  assert.deepEqual(s.store.getState().selectedSourcePath, ["comp_right"]);
  choose("comp_left"); const third = viewResponse(s, "part", { component_path: ["comp_left"] });
  choose("comp_right");choose("comp_left");await s.store.getState().loadSourceView(third);
  assert.equal(s.store.getState().sourceLoading, false);assert.deepEqual(s.store.getState().sourceView.component_path, ["comp_left"]);
  const count = s.calls.length;choose("comp_missing");assert.equal(s.calls.length, count);
});

test("stale context downloads cannot replace newer occurrence; context failure leaves an explicit standalone view", async () => {
  const pending = deferred();
  const s = await contextSetup({ fetch: async url => ({ ok: true, arrayBuffer: () => url === "/context.glb" ? pending.promise : Promise.resolve(new ArrayBuffer(1)) }) });
  s.store.getState().selectSource("part", false, ["comp_left"]);
  const frame = viewResponse(s, "part", { component_path: ["comp_left"], context_glb_url: "/context.glb",
    source_to_project: [1,0,0,20,0,1,0,0,0,0,1,0,0,0,0,1] });
  const loading = s.store.getState().loadSourceView(frame);
  s.store.getState().selectSource("part", false, ["comp_right"]);
  pending.resolve(new ArrayBuffer(1));await loading;
  assert.equal(s.events.filter(e => e.name === "cadenza:glb").length, 0);
  assert.deepEqual(s.store.getState().sourceRequest.componentPath, ["comp_right"]);
  const failed = await contextSetup({ fetch: async url => ({ ok: url !== "/context.glb", arrayBuffer: async () => new ArrayBuffer(1) }) });
  failed.store.getState().selectSource("part", false, ["comp_left"]);
  await failed.store.getState().loadSourceView({ ...frame, message_id: failed.store.getState().sourceRequest.id });
  assert.match(failed.store.getState().sourceContextWarning, /unavailable/);
  assert.equal(failed.events.filter(e => e.name === "cadenza:glb").at(-1).detail.context, undefined);
  assert.equal(failed.store.getState().sourceLoading, false);
});

test("source face inspection requires current loaded STEP identity and never enables primary-coordinate edits", async () => {
  const s = await contextSetup();
  s.store.setState({ selectedSourceId: "part", selectedSourcePath: ["comp_left"], geometryGeneration: 5,
    sourceView: { ledger_id: "part" }, model: { source: "source", pickMs: 1, colorMode: "part" } });
  const selection = { mode: "face", faceHash: 12, point: [1,2,3], normal: [0,0,1], screen: { x: 0, y: 0 },
    source: { ledgerId: "part", revision: 0, generation: 5, componentPath: ["comp_left"] } };
  for (const source of [undefined, { ...selection.source, revision: 1 }, { ...selection.source, generation: 4 },
    { ...selection.source, ledgerId: "second" }, { ...selection.source, componentPath: ["comp_right"] }]) {
    s.store.getState().select({ ...selection, source });assert.equal(s.store.getState().selections.length, 0);
  }
  s.store.setState({ sourceLoading: true });s.store.getState().select(selection);assert.equal(s.store.getState().selections.length, 0);
  s.store.setState({ sourceLoading: false });
  for (const model of [{ source: "source", pickMs: 1 }, { source: "source", colorMode: "part" }]) {
    s.store.setState({ model });s.store.getState().select(selection);assert.equal(s.store.getState().selections.length, 0);
  }
  s.store.setState({ model: { source: "source", pickMs: 1, colorMode: "part" } });s.store.getState().select(selection);
  assert.equal(s.store.getState().selections.length, 1);assert.equal(s.store.getState().promptOpen, false);
  assert.equal(s.store.getState().assemblySelection, null);
  s.store.getState().submitPrompt("Edit this face");s.store.getState().startDraft();assert.equal(s.prompts.length, 0);
  s.store.getState().select(null);assert.equal(s.store.getState().selections.length, 0);
  s.store.setState({ selectedSourceId: null });s.store.getState().select(selection);assert.equal(s.store.getState().selections.length, 0);
});

test("source rename requires explicit capability and stages a correlated revision-bound request", async () => {
  const s = await partSetup();
  const before = s.calls.length;
  s.store.getState().renamePart("part", "BasePlate");
  assert.equal(s.calls.length, before); // Old servers still permit creation/view, but not rename.
  s.store.setState({ partCapabilities: { ...s.store.getState().partCapabilities, rename_source: true } });
  for (const [id, name] of [["missing", "Base"], ["root", "Base"], ["part", " "], ["part", "Plate"]])
    s.store.getState().renamePart(id, name);
  assert.equal(s.calls.length, before);
  s.store.getState().renamePart("part", " BasePlate ");
  assert.equal(s.calls.length, before + 1);
  const frame = s.calls.at(-1), state = s.store.getState();
  assert.equal(frame.type, "design.part.rename");
  assert.equal(frame.name, "BasePlate");
  assert.equal(frame.ledger_id, "part");
  assert.equal(frame.base_revision, state.sessionRevision);
  assert.equal(frame.message_id, state.partRequest);
  assert.equal(state.ledgers.part.metadata.name, "Plate");
  s.store.getState().renamePart("part", "Other");
  assert.equal(s.calls.length, before + 1);
  state.onFrame({ type: "error", message_id: frame.message_id, code: "BAD_MESSAGE", message: "Duplicate name" });
  assert.equal(s.store.getState().busy, false);
  assert.equal(s.store.getState().partRequest, null);
  assert.equal(s.store.getState().ledgers.part.metadata.name, "Plate");
});

test("clearing explicit AI targets respects the unsent-edit guard", async () => {
  const s = await partSetup();
  s.store.setState({ newPartName: "New", featurePromptTarget: { ledgerId: "part" },
    assemblyPromptTarget: { ledgerId: "root", ownerPath: [], target: { kind: "definition" } } });
  s.window.addEventListener("cadenza:discard-numeric-edits", event => event.preventDefault());
  s.store.getState().startPartPrompt(null);
  s.store.getState().startFeaturePrompt(null);
  s.store.getState().startAssemblyPrompt(null);
  assert.equal(s.store.getState().newPartName, "New");
  assert.ok(s.store.getState().featurePromptTarget);
  assert.ok(s.store.getState().assemblyPromptTarget);
});

test("unsent numeric edits can block source and AI target changes without sending frames", async () => {
  const s = await partSetup();
  const refuse = event => event.preventDefault();
  s.window.addEventListener("cadenza:discard-numeric-edits", refuse);
  const before = s.calls.length;
  s.store.getState().selectSource("part");
  s.store.getState().startPartPrompt("New part");
  assert.equal(s.store.getState().selectedSourceId, null);
  assert.equal(s.store.getState().newPartName, null);
  assert.equal(s.calls.length, before);
  s.store.getState().selectSource("part", true); // Accepted-state refresh never asks to keep a stale draft.
  assert.equal(s.store.getState().selectedSourceId, "part");
  s.window.removeEventListener("cadenza:discard-numeric-edits", refuse);
});

test("source views do not change revision and reject primary replay or stale requests", async () => {
  const s = await partSetup({ fetch: async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(7) }) });
  s.store.getState().selectSource("part");
  const response = viewResponse(s);
  await s.store.getState().loadSourceView({ ...response, message_id: "old" });
  assert.equal(s.store.getState().model, null);
  await s.store.getState().loadSourceView(response);
  assert.equal(s.store.getState().model.source, "source");
  assert.equal(s.store.getState().sessionRevision, 0);
  const model = s.store.getState().model;
  await s.store.getState().onGeometry({ revision: 0, glb: new ArrayBuffer(3), source: "server" });
  assert.equal(s.store.getState().model, model);
  s.store.setState({ busy: true });
  s.store.getState().onFrame({ type: "geometry.ready", revision: 0, empty: true });
  assert.equal(s.store.getState().busy, true);
  assert.equal(s.store.getState().model, model);
});

test("rapid source selection queues only the latest and clears stale geometry", async () => {
  const pending = deferred();
  const s = await partSetup({ fetch: async () => { await pending.promise; return { ok: true, arrayBuffer: async () => new ArrayBuffer(4) }; } });
  s.store.getState().selectSource("part");
  const loading = s.store.getState().loadSourceView(viewResponse(s));
  s.store.getState().selectSource("second");
  assert.equal(s.calls.filter(c => c.type === "design.part.view").length, 1);
  pending.resolve(); await loading;
  assert.equal(s.store.getState().model, null);
  assert.equal(s.store.getState().sourceRequest.ledgerId, "second");
  await s.store.getState().loadSourceView(viewResponse(s, "second"));
  assert.equal(s.store.getState().sourceView.ledger_id, "second");
});

test("A to B to A during artifact loading cannot mark a cleared viewport as loaded", async () => {
  const pending = deferred();
  const s = await partSetup({ fetch: async () => { await pending.promise; return { ok: true, arrayBuffer: async () => new ArrayBuffer(4) }; } });
  s.store.getState().selectSource("part");
  const loading = s.store.getState().loadSourceView(viewResponse(s));
  s.store.getState().selectSource("second");
  s.store.getState().selectSource("part");
  pending.resolve(); await loading;
  assert.equal(s.store.getState().model, null);
  assert.equal(s.store.getState().sourceView, null);
  assert.equal(s.store.getState().sourceLoading, true);
  assert.equal(s.calls.filter(c => c.type === "design.part.view").length, 2);
  await s.store.getState().loadSourceView(viewResponse(s));
  assert.equal(s.store.getState().model.source, "source");
});

test("source preview uses source bytes even when primary is empty", async () => {
  const urls = [];
  const s = await partSetup({ fetch: async url => { urls.push(url); return { ok: true, arrayBuffer: async () => new ArrayBuffer(7) }; } });
  await s.store.getState().loadPreview({ type: "design.preview", message_id: "part_new", preview_id: "p",
    base_revision: 0, summary: "New part", changes: [], checks: [], assumptions: [], prompt: "", empty: true,
    glb_url: null, step_url: null, display_ledger_id: "new", source_empty: false, source_glb_url: "/new.glb" });
  assert.deepEqual(urls, ["/new.glb"]);
  assert.equal(s.store.getState().model.source, "preview");
  s.store.getState().decidePreview("apply");
  assert.equal(s.store.getState().selectedSourceId, "new");
});

test("candidate context uses candidate artifacts and whole-assembly toggle preserves the proposal", async () => {
  const urls=[];
  const s=await partSetup({fetch:async url=>{urls.push(url);return {ok:true,arrayBuffer:async()=>new ArrayBuffer(7)};}});
  const preview={type:"design.preview",message_id:"edit",preview_id:"candidate",base_revision:0,display_ledger_id:"part",
    source_glb_url:"/candidate/source.glb",source_step_url:"/candidate/source.step",glb_url:"/candidate/model.glb",step_url:"/candidate/model.step",
    context_component_path:["comp_a"],source_to_project:[1,0,0,20,0,1,0,0,0,0,1,0,0,0,0,1],empty:false};
  await s.store.getState().loadPreview(preview);
  assert.deepEqual(urls,["/candidate/source.glb","/candidate/source.step","/candidate/model.glb"]);
  const event=s.events.filter(e=>e.name==="cadenza:glb").at(-1);
  assert.equal(event.detail.context.componentPath,preview.context_component_path);
  assert.equal(s.store.getState().previewWhole,false);
  urls.length=0;await s.store.getState().loadPreview(preview,true);
  assert.deepEqual(urls,["/candidate/model.glb","/candidate/model.step"]);
  assert.equal(s.events.filter(e=>e.name==="cadenza:glb").at(-1).detail.context,undefined);
  assert.equal(s.store.getState().preview,preview);assert.equal(s.store.getState().previewWhole,true);
});

test("candidate context failure blocks Apply and whole-view reload recovers", async () => {
  let fail=true;
  const s=await partSetup({fetch:async url=>({ok:!(fail&&url==="/model"),arrayBuffer:async()=>new ArrayBuffer(7)})});
  const preview={type:"design.preview",message_id:"edit",preview_id:"candidate",base_revision:0,display_ledger_id:"part",
    source_glb_url:"/source",glb_url:"/model",context_component_path:["comp_a"],source_to_project:[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]};
  await s.store.getState().loadPreview(preview);
  assert.ok(s.store.getState().previewError);const count=s.calls.length;
  s.store.getState().decidePreview("apply");assert.equal(s.calls.length,count);
  fail=false;await s.store.getState().loadPreview(preview,true);assert.equal(s.store.getState().previewError,null);
  const recoveredCount=s.calls.length;
  s.store.setState({previewRendering:true});s.store.getState().decidePreview("apply");assert.equal(s.calls.length,recoveredCount);
});

test("source-view actions cannot reach primary probes, sketch draft or legacy prompts", async () => {
  const s = await partSetup(); s.store.setState({ selectedSourceId: "part" });
  s.store.getState().startDraft();
  s.store.getState().submitComposer("Change it");
  s.store.getState().submitPrompt("Change it");
  s.store.getState().select({ faceHash: 1, point: [0,0,0] });
  assert.equal(s.calls.some(c => c.type === "sketch.draft_start"), false);
  assert.equal(s.prompts.length, 0);
  assert.equal(s.store.getState().selections.length, 0);
});

test("accepted revisions keep source actions blocked until refreshed source artifacts arrive", async () => {
  const s = await partSetup();
  s.store.setState({ selectedSourceId: "part", busy: true, canRedo: true });
  s.store.getState().onFrame({ type: "ledger.updated", revision: 1, store: s.store.getState().ledgers,
    can_undo: true, can_redo: true });
  assert.equal(s.store.getState().sourceLoading, true);
  s.store.getState().redoSession();
  assert.equal(s.calls.some(c => c.type === "session.redo"), false);
  s.store.getState().onFrame({ type: "geometry.ready", revision: 1, empty: true });
  assert.equal(s.store.getState().sourceRequest.ledgerId, "part");
});

test("new-part clarification preserves input and Revise preserves creation mode in source view", async () => {
  const s = await partSetup();
  s.store.getState().startPartPrompt("Support");
  assert.equal(s.store.getState().submitPartPrompt("Create support"), true);
  const id = s.store.getState().partRequest;
  s.store.getState().onFrame({ type: "agent.message", message_id: id, text: "What thickness?", needs_clarification: true });
  s.store.getState().onFrame({ type: "agent.status", message_id: id, phase: "done" });
  assert.equal(s.store.getState().newPartName, "Support");
  s.store.setState({ selectedSourceId: "part", preview: { preview_id: "p", base_revision: 0, prompt: "Create support" } });
  s.store.getState().decidePreview("cancel", true);
  s.store.getState().onFrame({ type: "design.preview_closed", preview_id: "p", message_id: "closed" });
  assert.equal(s.store.getState().newPartName, "Support");
  assert.equal(s.calls.filter(c => c.type === "design.part.prompt").length, 1);
});

function patternResponse(s) {
  return { type: "design.pattern.capabilities", message_id: s.store.getState().patternDiscovery, session_id: "ses_test",
    version: 1, scope: "source", operations: ["add_linear_pattern", "add_circular_pattern", "update_linear_pattern", "update_circular_pattern"],
    seed_kinds: ["box", "cylinder", "hole"], directions: ["+x", "-x", "+y", "-y", "+z", "-z"], count_min: 2, count_max: 32,
    copy_budget: 128, radius_mode: "seed_position" };
}

test("pattern discovery is independent, correlated and cannot consume operation errors", async () => {
  const s = await commandSetup(); s.store.getState().onFrame(s.response);
  const response = patternResponse(s);
  for (const stale of [{ ...response, message_id: "old" }, { ...response, session_id: "old" }]) {
    s.store.getState().onFrame(stale); assert.equal(s.store.getState().patternCapabilities, null);
  }
  const promptId = s.store.getState().featurePromptDiscovery;
  s.store.setState({ busy: true, featurePromptRequest: "b3_running" });
  s.store.getState().onFrame({ type: "error", message_id: response.message_id, code: "BAD_MESSAGE", message: "Unsupported" });
  assert.equal(s.store.getState().busy, true); assert.equal(s.store.getState().featurePromptDiscovery, promptId);
  s.store.getState().onFrame({ type: "error", message_id: "b3_running", code: "BAD_MESSAGE", message: "Invalid operation" });
  assert.equal(s.store.getState().busy, false); assert.match(s.store.getState().conversation.at(-1).text, /Invalid operation/);
  s.store.setState({ patternDiscovery: response.message_id }); s.store.getState().onFrame(response);
  assert.ok(s.store.getState().patternCapabilities);
  s.connection("closed"); assert.equal(s.store.getState().patternCapabilities, null);
});

test("uncorrelated optional discoveries fail closed without unlocking a mutation", async () => {
  for (const detail of ["Unknown design.pattern.capabilities", "Unsupported capabilities request"]) {
    const s = await commandSetup(); s.store.getState().onFrame(s.response);
    const promptId = s.store.getState().featurePromptDiscovery;
    s.store.getState().previewFeatureCommand("part", widthCommand);
    s.store.getState().onFrame({ type: "error", message_id: null, code: "BAD_MESSAGE", detail, message: "Unsupported" });
    assert.equal(s.store.getState().patternDiscovery, null); assert.ok(s.store.getState().capabilities);
    assert.equal(s.store.getState().busy, true);
    assert.equal(s.store.getState().featurePromptDiscovery, detail.startsWith("Unknown") ? promptId : null);
  }
});

test("legacy parser expected-tag lists do not misidentify rejected discovery or mutation", async () => {
  const s = await commandSetup(); s.store.getState().onFrame(s.response);
  const promptId = s.store.getState().featurePromptDiscovery;
  s.store.getState().previewFeatureCommand("part", widthCommand);
  s.store.getState().onFrame({ type: "error", message_id: null, code: "BAD_MESSAGE", message: "Could not parse",
    detail: "Input tag 'design.pattern.capabilities' found using 'type' does not match expected tags: 'design.prompt.capabilities', 'design.capabilities'" });
  assert.equal(s.store.getState().patternDiscovery, null);
  assert.equal(s.store.getState().featurePromptDiscovery, promptId); assert.equal(s.store.getState().busy, true);
  s.store.getState().onFrame({ type: "error", message_id: null, code: "BAD_MESSAGE", message: "Invalid command",
    detail: "Input tag 'design.command' does not match expected tags: 'design.prompt.capabilities'" });
  assert.equal(s.store.getState().featurePromptDiscovery, promptId); assert.equal(s.store.getState().busy, false);
  assert.match(s.store.getState().conversation.at(-1).text, /Invalid command/);
});

test("pattern commands and AI targets retain every store mutation guard", async () => {
  const s = await promptSetup(); s.store.getState().onFrame(patternResponse(s));
  const op = { op: "add_linear_pattern", name: "Row", target_feature: "feat_box", count: 3, spacing: 30, direction: "+x" };
  const baseline = s.store.getState();
  for (const block of [{ busy: true }, { preview: {} }, { draft: {} }, { draftStarting: true }, { patternCapabilities: null },
    { connection: "closed" }, { model: { source: "local" } }]) {
    s.store.setState({ ...baseline, ...block }); s.store.getState().previewFeatureCommand("part", [op]);
  }
  assert.equal(s.calls.filter(f => f?.type === "design.command").length, 0);
  s.store.setState(baseline); s.store.getState().previewFeatureCommand("part", [op]);
  assert.equal(s.calls.at(-1).type, "design.command"); assert.equal(s.prompts.length, 0);
  const source = structuredClone(commandLedger);
  source.features[0].operation = "add";
  source.features.push({ id: "feat_pattern", kind: "linear_pattern", suppressed: false, operation: "add",
    placement: { origin: [0, 0, 0], rotation_deg: [0, 0, 0], relative_to: null },
    parameters: { target_feature: "feat_box", count: 3, spacing: 30, axis: [1, 0, 0] } });
  s.store.setState({ ...baseline, ledgers: { part: source } });
  s.store.getState().startFeaturePrompt("part", "feat_pattern");
  assert.equal(s.store.getState().featurePromptTarget.featureId, "feat_pattern");
  assert.equal(s.store.getState().submitFeaturePrompt("Use 4 total"), true);
  assert.equal(s.calls.at(-1).feature_id, "feat_pattern");
});

test("targeted prompts pin IDs, preserve input and do not use checkbox targets or legacy prompts", async () => {
  const s = await promptSetup();
  s.store.setState({ activeLedgerIds: ["wrong"], attachments: [{ media_type: "image/png", data: "fixture", previewUrl: "blob:fixture" }] });
  s.store.getState().startFeaturePrompt("part", "feat_box");
  const before = JSON.stringify(s.store.getState().ledgers);
  assert.equal(s.store.getState().submitFeaturePrompt("Set height to 12 mm"), true);
  assert.equal(s.store.getState().submitFeaturePrompt("duplicate"), false);
  const request = s.calls.at(-1);
  assert.equal(request.type, "design.prompt"); assert.equal(request.ledger_id, "part");
  assert.equal(request.feature_id, "feat_box"); assert.equal(request.base_revision, 0);
  assert.equal(request.scope, "source"); assert.equal(request.images.length, 1);
  assert.equal(s.prompts.length, 0); assert.equal(JSON.stringify(s.store.getState().ledgers), before);
  s.store.getState().onFrame({ type: "agent.status", message_id: "b3_old", phase: "done" });
  assert.equal(s.store.getState().busy, true);
  s.store.getState().onFrame({ type: "error", message_id: request.message_id, code: "AGENT_INVALID_OUTPUT", message: "Try again" });
  assert.equal(s.store.getState().busy, false); assert.equal(s.store.getState().attachments.length, 1);
  assert.equal(s.store.getState().featurePromptTarget.featureId, "feat_box");
  assert.equal(s.store.getState().submitFeaturePrompt("Corrected request"), true);
  s.connection("closed");
  s.store.getState().onFrame({ type: "design.preview", message_id: request.message_id, base_revision: 0, preview_id: "late" });
  assert.equal(s.store.getState().preview, null); assert.equal(s.store.getState().featurePromptTarget, null);
});

test("B3 discovery rejection keeps B2 controls and cannot unlock a concurrent edit", async () => {
  for (const identified of [true, false]) {
    const s = await commandSetup(); s.store.getState().onFrame(s.response);
    const id = s.store.getState().featurePromptDiscovery;
    s.store.getState().previewFeatureCommand("part", widthCommand);
    s.store.getState().onFrame({ type: "error", message_id: identified ? id : null, code: "BAD_MESSAGE",
      detail: "Unknown input tag design.prompt.capabilities", message: "Unsupported" });
    assert.equal(s.store.getState().featurePromptAvailable, false);
    assert.ok(s.store.getState().capabilities); assert.equal(s.store.getState().busy, true);
  }
});

test("targeted prompt submission checks every guard and does not mint local state", async () => {
  const s = await promptSetup(); s.store.getState().startFeaturePrompt("part", "feat_box");
  const baseline = s.store.getState();
  for (const block of [{ busy: true }, { preview: {} }, { draft: {} }, { draftStarting: true },
    { connection: "closed" }, { agentsAvailable: false }, { featurePromptAvailable: false },
    { capabilities: null }, { model: { source: "local" } }, { ledgers: {} }]) {
    s.store.setState({ ...baseline, ...block });
    assert.equal(s.store.getState().submitFeaturePrompt("Set height to 15 mm"), false);
  }
  s.store.setState(baseline);
  assert.equal(s.store.getState().submitFeaturePrompt(" "), false);
  assert.equal(s.store.getState().submitFeaturePrompt("x".repeat(4001)), false);
  assert.equal(s.calls.filter(f => f?.type === "design.prompt").length, 0);
});

test("B3 revise retains route and attachments; accepted replacement clears target", async () => {
  const s = await promptSetup(); s.store.getState().startFeaturePrompt("part", "feat_box");
  s.store.getState().submitFeaturePrompt("Change height");
  s.store.setState({ busy: false, featurePromptRequest: null,
    preview: { preview_id: "preview", base_revision: 0, prompt: "Change height" } });
  s.store.getState().decidePreview("cancel", true);
  assert.equal(s.store.getState().reviseInput.featurePromptTarget.featureId, "feat_box");
  s.store.getState().onFrame({ type: "design.preview_closed", preview_id: "preview" });
  await s.store.getState().onGeometry({ revision: 0, glb: new ArrayBuffer(2), source: "server" });
  assert.equal(s.store.getState().featurePromptTarget.featureId, "feat_box");
  s.store.getState().onFrame({ type: "ledger.updated", revision: 1, store: { part: commandLedger }, can_undo: true });
  assert.equal(s.store.getState().featurePromptTarget, null);
});

test("B3 revise cannot restore a feature removed from the accepted snapshot", async () => {
  const s = await promptSetup();
  s.store.setState({ reviseInput: { activeLedgerIds: ["part"], selections: [], attachments: [],
    featurePromptTarget: { ledgerId: "part", featureId: "feat_missing" } } });
  await s.store.getState().onGeometry({ revision: 0, glb: new ArrayBuffer(2), source: "server" });
  assert.equal(s.store.getState().featurePromptTarget, null);
});

test("capabilities are correlated, reset on disconnect and never enable controls for obsolete sessions", async () => {
  const s = await commandSetup();
  const before = JSON.stringify(s.store.getState().ledgers);
  s.store.getState().previewFeatureCommand("part", widthCommand);
  assert.equal(s.calls.filter(f => f?.type === "design.command").length, 0);
  for (const invalid of [{ ...s.response, message_id: "old" }, { ...s.response, session_id: "old" }]) {
    s.store.getState().onFrame(invalid); assert.equal(s.store.getState().capabilities, null);
  }
  s.store.getState().onFrame(s.response);
  assert.ok(s.store.getState().capabilities);
  assert.equal(s.store.getState().busy, false);
  assert.equal(JSON.stringify(s.store.getState().ledgers), before);
  s.connection("closed"); s.connection("open"); s.ready();
  s.store.getState().onFrame(s.response);
  assert.equal(s.store.getState().capabilities, null);
  s.store.getState().onFrame({ ...s.response, message_id: s.store.getState().capabilityRequest, version: 2 });
  assert.equal(s.store.getState().capabilities, null);
});

test("legacy discovery rejection does not unlock a concurrent edit or hide a correlated mutation error", async () => {
  for (const correlated of [true, false]) {
    const s = await commandSetup();
    s.store.getState().editParameter("part", "feat_box", "width", 24);
    const request = s.calls.at(-1);
    assert.equal(request.type, "design.parameter");
    s.store.getState().onFrame({ type: "error", code: "BAD_MESSAGE", message: "Unknown discovery",
      detail: "Input tag design.capabilities does not match any expected tags",
      message_id: correlated ? s.response.message_id : null });
    assert.equal(s.store.getState().capabilities, null);
    assert.equal(s.store.getState().busy, true);
    s.store.getState().onFrame({ type: "error", code: "PATCH_INVALID", message: "Invalid dimension", message_id: request.message_id });
    assert.equal(s.store.getState().busy, false);
    assert.match(s.store.getState().conversation.at(-1).text, /Invalid dimension/);
  }
});

test("feature commands use explicit source and current revision, send once and preserve accepted state", async () => {
  const s = await commandSetup(); s.store.getState().onFrame(s.response);
  const before = JSON.stringify(s.store.getState().ledgers);
  s.store.setState({ sessionRevision: 7, activeLedgerIds: [] });
  s.store.getState().previewFeatureCommand("part", widthCommand);
  s.store.getState().previewFeatureCommand("part", widthCommand);
  const commands = s.calls.filter(f => f?.type === "design.command");
  assert.equal(commands.length, 1); assert.equal(commands[0].ledger_id, "part");
  assert.equal(commands[0].base_revision, 7); assert.equal(commands[0].scope, "source");
  assert.equal(JSON.stringify(s.store.getState().ledgers), before); assert.equal(s.prompts.length, 0);
});

test("feature actions check every mutation guard at invocation", async () => {
  const s = await commandSetup(); s.store.getState().onFrame(s.response);
  for (const guard of [{ busy: true }, { draft: emptyDraft }, { draftStarting: true }, { preview: {} },
    { connection: "closed" }, { model: { source: "local" } }, { capabilities: null }]) {
    const before = s.store.getState(); s.store.setState(guard);
    s.store.getState().previewFeatureCommand("part", widthCommand);
    s.store.setState(before);
  }
  s.store.getState().previewFeatureCommand("missing", widthCommand);
  s.store.getState().previewFeatureCommand("part", [{ ...widthCommand[0], parameter: "radius" }]);
  s.store.getState().previewFeatureCommand("part", []);
  assert.equal(s.calls.filter(f => f?.type === "design.command").length, 0);
});

function draftState(s, draft = emptyDraft, messageId = s.store.getState().draftRequest) {
  s.store.getState().onFrame({ type: "sketch.draft_state", session_id: "ses_test", message_id: messageId, draft });
}

async function drafting(globals = {}) {
  const s = await setup({}, globals);
  s.ready(); s.store.getState().connect(); s.connection("open");
  s.store.getState().startDraft(); draftState(s);
  return s;
}

test("sketch proposals are private, correlated and invalidated by manual edits", async () => {
  const s = await drafting();
  s.store.getState().promptSketch("Create a 100 x 60 plate");
  const request = s.calls.at(-1);
  assert.equal(request.type, "sketch.prompt");
  const frame = { type: "sketch.proposal", message_id: request.message_id, session_id: "ses_test",
    draft_id: "draft_test", draft_revision: 0, base_revision: 0, selected_entity_id: null,
    proposal_id: "proposal_one", summary: "Create plate", question: null, assumptions: [], interpretation: [], changes: [], checks: [] };
  s.store.getState().onFrame({ ...frame, message_id: "obsolete" });
  assert.equal(s.store.getState().draftProposal, null);
  s.store.getState().onFrame(frame);
  assert.equal(s.store.getState().draftProposal.proposal_id, "proposal_one");
  assert.equal(s.store.getState().draft.revision, 0);
  s.store.getState().decideSketchProposal("update");
  assert.equal(s.calls.at(-1).type, "sketch.proposal_decision");
  assert.equal(s.store.getState().draft.revision, 0);
  draftState(s, { ...emptyDraft, revision: 1 });
  assert.equal(s.store.getState().draftProposal, null);
  s.store.getState().promptSketch("Add an opening");
  const next = { ...frame, message_id: s.calls.at(-1).message_id, draft_revision: 1 };
  s.store.getState().onFrame(next);
  s.store.getState().updateDraft([{ op: "set_extrusion", amount: 8 }]);
  assert.equal(s.store.getState().draftProposal, null);
  s.store.getState().onFrame(next);
  assert.equal(s.store.getState().draftProposal, null);
});

test("clarifications carry identity and cannot survive selection or reconnect", async () => {
  const s = await drafting();
  const entities = [{ id: "ent_outer" }, { id: "ent_hole" }];
  draftState(s, { ...emptyDraft, revision: 1, definition: { entities, constraints: [] } });
  s.store.getState().promptSketch("Make it bigger");
  const frame = { type: "sketch.proposal", message_id: s.calls.at(-1).message_id, session_id: "ses_test",
    draft_id: "draft_test", draft_revision: 1, base_revision: 0, selected_entity_id: "ent_outer",
    proposal_id: "question_one", summary: "Need dimensions", question: "What width?", assumptions: [], interpretation: [], changes: [], checks: [] };
  s.store.getState().onFrame(frame);
  const count = s.calls.length;
  s.store.getState().decideSketchProposal("update");
  assert.equal(s.calls.length, count);
  s.store.getState().promptSketch("140 mm", true);
  assert.equal(s.calls.at(-1).reply_to, "question_one");
  s.store.getState().onFrame({ ...frame, message_id: s.calls.at(-1).message_id });
  s.store.getState().selectDraftEntity("ent_hole");
  assert.equal(s.store.getState().draftProposal, null);
  s.store.getState().promptSketch("Set diameter to 8 mm");
  const late = { ...frame, message_id: s.calls.at(-1).message_id, selected_entity_id: "ent_hole" };
  s.connection("closed"); s.connection("open"); s.ready();
  s.store.getState().onFrame(late);
  assert.equal(s.store.getState().draftProposal, null);
  assert.equal(s.store.getState().draft, null);
});

test("relationship batches stay revision-bound and wait for server-owned identities", async () => {
  const s = await drafting();
  const operations = [
    { op: "add_entity", alias: "outer", entity: { kind: "circle", diameter: 40 } },
    { op: "add_dimension", alias: "size", entity_id: { entity_alias: "outer" }, parameter: "diameter", value: 40 },
    { op: "add_driver", parameter: "diameter", value: 40 },
    { op: "bind_dimension", constraint_id: { constraint_alias: "size" }, expression: "diameter" },
  ];
  s.store.getState().updateDraft(operations);
  const request = s.calls.at(-1);
  assert.deepEqual(request.operations, operations);
  assert.equal(request.draft_revision, 0);
  assert.deepEqual(s.store.getState().draft.definition.entities, []);
  assert.equal(s.prompts.length, 0);
  const entity = { id: "ent_server", kind: "circle", operation: "add", x: 0, y: 0, diameter: 40 };
  draftState(s, { ...emptyDraft, revision: 1, definition: { entities: [entity], constraints: [] } });
  assert.equal(s.store.getState().draftEntityId, "ent_server");
  assert.equal(s.store.getState().busy, false);
});

test("draft responses require matching session, request, accepted and draft revisions", async () => {
  const s = await drafting();
  s.store.getState().updateDraft([{ op: "set_extrusion", amount: 8 }]);
  const request = s.calls.at(-1);
  assert.equal(request.draft_id, "draft_test");
  assert.equal(request.draft_revision, 0);
  draftState(s, { ...emptyDraft, revision: 1 }, "old_request");
  s.store.getState().onFrame({ type: "error", message_id: "old_request", code: "PATCH_INVALID", message: "Old error" });
  assert.equal(s.store.getState().busy, true);
  assert.equal(s.store.getState().draft.revision, 0);
  draftState(s, { ...emptyDraft, revision: 1, base_revision: 9 });
  assert.equal(s.store.getState().draft.revision, 0);
  draftState(s, { ...emptyDraft, revision: 1 });
  assert.equal(s.store.getState().draft.revision, 1);
  draftState(s, emptyDraft, request.message_id);
  assert.equal(s.store.getState().draft.revision, 1);
  assert.equal(s.store.getState().busy, false);
});

test("draft blocks competing edits, imports and late preview resurrection", async () => {
  const s = await drafting();
  const count = s.calls.filter(call => typeof call === "object").length;
  s.store.getState().submitPrompt("change part");
  s.store.getState().editParameter("part", "feat_draft", "width", 150);
  s.store.getState().undoSession();
  await s.store.getState().onGeometry({ revision: 0, source: "local", glb: new ArrayBuffer(2) });
  assert.equal(s.calls.filter(call => typeof call === "object").length, count);
  assert.equal(s.prompts.length, 0);
  assert.equal(s.store.getState().model, null);
  s.store.getState().actOnDraft("discard");
  const request = s.calls.at(-1);
  s.store.getState().onFrame({ type: "sketch.draft_closed", session_id: "ses_test", draft_id: "draft_test", message_id: "obsolete" });
  assert.ok(s.store.getState().draft);
  s.store.getState().onFrame({ type: "sketch.draft_closed", session_id: "ses_test", draft_id: "draft_test", message_id: request.message_id });
  await s.store.getState().loadPreview({ preview_id: "late", base_revision: 0, draft_id: "draft_test", draft_revision: 0, message_id: request.message_id, empty: true });
  draftState(s, emptyDraft, request.message_id);
  assert.equal(s.store.getState().draft, null);
  assert.equal(s.store.getState().preview, null);
});

test("draft preview cancel retains draft and disconnect invalidates pending downloads", async () => {
  const s = await drafting();
  s.store.getState().actOnDraft("finish");
  await s.store.getState().loadPreview({ preview_id: "review", base_revision: 0, draft_id: "draft_test", draft_revision: 0,
    message_id: s.store.getState().draftRequest, empty: true });
  s.store.getState().decidePreview("cancel");
  s.store.getState().onFrame({ type: "design.preview_closed", preview_id: "review", message_id: s.store.getState().draftRequest });
  draftState(s);
  assert.equal(s.store.getState().preview, null);
  assert.equal(s.store.getState().draft.id, "draft_test");
  const gate = deferred();
  const t = await drafting({ fetch: () => gate.promise });
  t.store.getState().actOnDraft("finish");
  const loading = t.store.getState().loadPreview({ preview_id: "pending", base_revision: 0, draft_id: "draft_test", draft_revision: 0,
    message_id: t.store.getState().draftRequest, glb_url: "/preview" });
  t.connection("closed"); t.connection("open");
  gate.resolve({ ok: true, arrayBuffer: async () => new ArrayBuffer(2) });
  await loading;
  assert.equal(t.store.getState().draft, null);
  assert.equal(t.store.getState().preview, null);
  assert.equal(t.store.getState().model, null);
});

test("malformed draft input reports an error and allows correction without losing draft", async () => {
  const s = await drafting();
  s.store.getState().updateDraft([{ op: "set_extrusion", amount: 1e9 }]);
  s.store.getState().onFrame({ type: "error", code: "BAD_MESSAGE", message: "Outside supported limits" });
  assert.equal(s.store.getState().busy, false);
  assert.equal(s.store.getState().draftError, "Outside supported limits");
  assert.equal(s.store.getState().draft.id, "draft_test");
  s.store.getState().updateDraft([{ op: "set_extrusion", amount: 8 }]);
  draftState(s, { ...emptyDraft, revision: 1, extrusion_amount: 8 });
  assert.equal(s.store.getState().draft.extrusion_amount, 8);
  assert.equal(s.store.getState().draftError, null);
});

const surfaceResult = { status: "resolved", face: { point: [20, 20, 40], normal: [0, 0, 1], center: [0, 0, 40], area_mm2: 100 } };

test("sketch selection follows server IDs, survives review and clears on deletion or reconnect", async () => {
  const s = await drafting();
  const outer = { id: "ent_outer", kind: "circle", x: 0, y: 0, diameter: 100 };
  const hole = { id: "ent_server_hole", kind: "circle", x: 0, y: 0, diameter: 20, operation: "subtract" };
  s.store.getState().updateDraft([{ op: "add_entity", entity: { kind: "circle", diameter: 100 } }]);
  draftState(s, { ...emptyDraft, revision: 1, definition: { entities: [outer], constraints: [] } });
  assert.equal(s.store.getState().draftEntityId, outer.id);
  s.store.getState().updateDraft([{ op: "add_entity", entity: { kind: "circle", operation: "subtract", diameter: 20 } }]);
  const second = { ...emptyDraft, revision: 2, definition: { entities: [outer, hole], constraints: [] } };
  draftState(s, second);
  assert.equal(s.store.getState().draftEntityId, hole.id);
  s.store.getState().selectDraftEntity("ent_missing");
  assert.equal(s.store.getState().draftEntityId, hole.id);
  s.store.getState().selectDraftEntity(outer.id, "seed-x");
  assert.equal(s.store.getState().draftFocus.field, "seed-x");
  const focus = s.store.getState().draftFocus;
  s.store.getState().selectDraftEntity(outer.id, "seed-x");
  assert.notEqual(s.store.getState().draftFocus.request, focus.request);
  s.store.getState().actOnDraft("finish");
  await s.store.getState().loadPreview({ preview_id: "draft_review", base_revision: 0, draft_id: "draft_test", draft_revision: 2,
    message_id: s.store.getState().draftRequest, empty: true });
  s.store.getState().selectDraftEntity(hole.id);
  assert.equal(s.store.getState().draftEntityId, outer.id);
  s.store.getState().decidePreview("cancel");
  s.store.getState().onFrame({ type: "design.preview_closed", preview_id: "draft_review" });
  draftState(s, second);
  assert.equal(s.store.getState().draftEntityId, outer.id);
  s.store.getState().selectDraftEntity(hole.id);
  s.store.getState().updateDraft([{ op: "remove_entity", entity_id: hole.id }]);
  draftState(s, { ...emptyDraft, revision: 3, definition: { entities: [outer], constraints: [] } });
  assert.equal(s.store.getState().draftEntityId, outer.id);
  assert.equal(s.store.getState().draftFocus, null);
  s.connection("closed");
  assert.equal(s.store.getState().draftEntityId, null);
});
const surfaceResponse = result => ({ ok: true, json: async () => ({ session_id: "ses_test", revision: 4, result }) });
function surfaceReady(s) {
  s.ready(4);
  s.store.getState().connect();
  s.connection("open");
  s.store.setState({ model: { revision: 4, source: "server" }, ledgers: { part: { surface_references: {
    ref_top: { feature_id: "feat_plate", component_path: [], source_ledger_id: "part" },
  } } } });
}

test("saved surface uses a fresh check and forwards identity in both prompt paths", async () => {
  const requests = [];
  const s = await setup({}, { fetch: async (url, init) => { requests.push(JSON.parse(init.body)); return surfaceResponse(surfaceResult); } });
  surfaceReady(s);
  await s.store.getState().checkSurface("part", "ref_top");
  assert.equal(s.store.getState().selections.length, 0);
  await s.store.getState().checkSurface("part", "ref_top", true);
  assert.equal(requests.length, 2);
  assert.equal(requests[1].base_revision, 4);
  for (const action of ["submitPrompt", "submitComposer"]) {
    s.store.setState({ busy: false });
    s.store.getState()[action]("make it thicker");
    const target = s.prompts.at(-1).targets[0];
    assert.equal(target.saved_reference.reference_id, "ref_top");
    assert.equal(target.saved_reference.ledger_id, "part");
    assert.equal(target.point[2], 40);
  }
});

test("late surface checks cannot cross revisions, sessions, local previews or reconnects", async () => {
  for (const invalidate of [
    s => s.store.getState().onFrame({ type: "ledger.updated", revision: 5, store: {}, can_undo: true }),
    s => s.ready(4),
    s => s.store.getState().onGeometry({ revision: 4, glb: new ArrayBuffer(3), source: "local" }),
    s => { s.connection("closed"); s.connection("open"); },
  ]) {
    const gate = deferred();
    const s = await setup({}, { fetch: () => gate.promise });
    surfaceReady(s);
    const pending = s.store.getState().checkSurface("part", "ref_top", true);
    await invalidate(s);
    gate.resolve(surfaceResponse(surfaceResult));
    await pending;
    assert.equal(s.store.getState().selections.length, 0);
    assert.equal(Object.keys(s.store.getState().surfaceChecks).length, 0);
    assert.equal(s.store.getState().surfaceChecking, null);
  }
});

test("surface checks cannot replace a newer user selection and unavailable surfaces cannot be selected", async () => {
  const gate = deferred();
  const s = await setup({}, { fetch: () => gate.promise });
  surfaceReady(s);
  const pending = s.store.getState().checkSurface("part", "ref_top", true);
  s.store.getState().select({ mode: "face", faceHash: 8 });
  gate.resolve(surfaceResponse(surfaceResult));
  await pending;
  assert.equal(s.store.getState().selections[0].faceHash, 8);
  for (const status of ["missing", "ambiguous", "unsupported"]) {
    const t = await setup({}, { fetch: async () => surfaceResponse({ status, message: status }) });
    surfaceReady(t);
    await t.store.getState().checkSurface("part", "ref_top", true);
    assert.equal(t.store.getState().selections.length, 0);
    assert.equal(t.store.getState().surfaceChecks["part:ref_top"].status, status);
  }
});

test("HTTP check failures are visible and stale models cannot become edit targets", async () => {
  let requests = 0;
  const s = await setup({}, { fetch: async () => { requests++; return { ok: false, json: async () => ({ detail: "Design changed" }) }; } });
  surfaceReady(s);
  s.store.setState({ model: { revision: 3 } });
  await s.store.getState().checkSurface("part", "ref_top", true);
  assert.equal(requests, 0);
  await s.store.getState().checkSurface("part", "ref_top");
  assert.equal(s.store.getState().surfaceChecks["part:ref_top"].message, "Design changed");
  assert.equal(s.store.getState().surfaceChecking, null);
});

const previewFrame = { type: "design.preview", message_id: "msg_preview", preview_id: "candidate_a", base_revision: 4,
  summary: "Height 40 mm", prompt: "Make this plate 40 mm thick", changes: [], checks: [], assumptions: [],
  glb_url: "/preview.glb", step_url: null, empty: false };

test("preview is separate from accepted state, blocks edits and applies exact identity", async () => {
  const s = await setup({}, { fetch: async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(4) }) });
  surfaceReady(s);
  const accepted = s.store.getState().ledgers;
  await s.store.getState().loadPreview(previewFrame);
  assert.equal(s.store.getState().sessionRevision, 4);
  assert.equal(s.store.getState().ledgers, accepted);
  assert.equal(s.store.getState().model.source, "preview");
  s.store.getState().submitComposer("another edit");
  s.store.getState().editParameter("part", "feat_plate", "height", 50);
  s.store.getState().select({ mode: "face", faceHash: 1 });
  assert.equal(s.prompts.length, 0);
  assert.equal(s.store.getState().selections.length, 0);
  assert.equal(s.calls.some(call => call.type === "design.parameter"), false);
  s.store.getState().decidePreview("apply");
  const frame = s.calls.find(call => call.type === "design.preview_decision");
  assert.equal(frame.preview_id, "candidate_a");
  assert.equal(frame.base_revision, 4);
  s.store.getState().onFrame({ type: "ledger.updated", revision: 5, store: accepted, can_undo: true });
  await s.store.getState().onGeometry({ revision: 5, glb: new ArrayBuffer(4) });
  s.store.getState().onFrame({ type: "design.preview_closed", preview_id: "candidate_a" });
  assert.equal(s.store.getState().preview, null);
  assert.equal(s.store.getState().model.source, "server");
});

test("late preview downloads cannot survive cancellation, revision changes or reconnection", async () => {
  for (const invalidate of [
    s => s.store.getState().onFrame({ type: "design.preview_closed", preview_id: "candidate_a" }),
    s => s.store.getState().onFrame({ type: "ledger.updated", revision: 5, store: {}, can_undo: true }),
    s => { s.connection("closed"); s.connection("open"); s.ready(4); },
  ]) {
    const gate = deferred();
    const s = await setup({}, { fetch: () => gate.promise });
    surfaceReady(s);
    const task = s.store.getState().loadPreview(previewFrame);
    invalidate(s);
    gate.resolve({ ok: true, arrayBuffer: async () => new ArrayBuffer(4) });
    await task;
    assert.notEqual(s.store.getState().model?.source, "preview");
    assert.equal(s.events.filter(event => event.name === "cadenza:glb").length, 0);
  }
});

test("failed preview loads block Apply but allow Cancel and revision of the original request", async () => {
  const s = await setup({}, { fetch: async () => ({ ok: false }) });
  surfaceReady(s);
  await s.store.getState().loadPreview(previewFrame);
  assert.ok(s.store.getState().previewError);
  s.store.getState().decidePreview("apply");
  assert.equal(s.calls.some(call => call.type === "design.preview_decision"), false);
  s.store.getState().decidePreview("cancel", true);
  assert.equal(s.calls.find(call => call.type === "design.preview_decision").action, "cancel");
  assert.equal(s.events.find(event => event.name === "cadenza:revise").detail, previewFrame.prompt);
});

test("numeric changes send typed commands without a prompt and reject invalid values", async () => {
  const s = await setup();
  surfaceReady(s);
  for (const value of [-1, 0, NaN, Infinity]) s.store.getState().editParameter("part", "feat_plate", "height", value);
  assert.equal(s.calls.some(call => call.type === "design.parameter"), false);
  s.store.getState().editParameter("part", "feat_plate", "height", 40);
  const frame = s.calls.find(call => call.type === "design.parameter");
  assert.equal(frame.value, 40);
  assert.equal(frame.base_revision, 4);
  assert.equal(s.prompts.length, 0);
});

test("named parameter commands send accepted revision without AI and guard derived or busy edits", async () => {
  const s = await setup();
  surfaceReady(s);
  const initialCalls = s.calls.length; // Read-only capability discovery is not an edit.
  s.store.setState({ ledgers: { part: { relationships: { parameters: {
    margin: { value: 15, expression: null, unit: "mm" },
    derived: { value: null, expression: "margin / 2", unit: "mm" },
  } } } } });
  const edit = (name = "margin", value = 20) => s.store.getState().editNamedParameter("part", name, value);
  edit("derived"); edit("missing"); edit("margin", Infinity); edit("margin", 1e10);
  assert.equal(s.calls.length, initialCalls);
  for (const blocked of [{ busy: true }, { preview: previewFrame }, { connection: "closed" }, { model: { source: "local" } }]) {
    const original = s.store.getState();
    s.store.setState(blocked); edit(); s.store.setState(original);
  }
  assert.equal(s.calls.length, initialCalls);
  edit();
  const frame = s.calls.at(-1);
  assert.equal(frame.type, "design.named_parameter");
  assert.equal(frame.parameter, "margin");
  assert.equal(frame.value, 20);
  assert.equal(frame.base_revision, 4);
  assert.equal(s.prompts.length, 0);
  assert.equal(s.store.getState().busy, true);
});

test("sketch dimensions send a typed revision-bound command without AI", async () => {
  const s = await setup();
  surfaceReady(s);
  const initialCalls = s.calls.length;
  const edit = value => s.store.getState().editSketchDimension("part", "feat_profile", "con_x", value);
  edit(NaN); edit(1e7);
  assert.equal(s.calls.length, initialCalls);
  for (const blocked of [{ busy: true }, { preview: previewFrame }, { connection: "closed" }, { model: { source: "local" } }]) {
    const original = s.store.getState();
    s.store.setState(blocked); edit(20); s.store.setState(original);
  }
  assert.equal(s.calls.length, initialCalls);
  edit(-10);
  assert.equal(s.calls.at(-1).type, "design.sketch_dimension");
  assert.equal(s.calls.at(-1).constraint_id, "con_x");
  assert.equal(s.calls.at(-1).value, -10);
  assert.equal(s.calls.at(-1).base_revision, 4);
  assert.equal(s.prompts.length, 0);
  assert.equal(s.store.getState().busy, true);
});

test("Revise restores drawing attachments and selection scope on accepted geometry replay", async () => {
  const s = await setup({}, { fetch: async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(4) }) });
  surfaceReady(s);
  s.store.setState({ attachments: [{ media_type: "image/png", data: "drawing", previewUrl: "blob:drawing" }],
    selections: [{ mode: "face", faceHash: 123, point: [20, 20, 20], normal: [0, 0, 1] }], activeLedgerIds: ["part"] });
  s.store.getState().submitComposer("Change this using the drawing");
  await s.store.getState().loadPreview(previewFrame);
  s.store.getState().decidePreview("cancel", true);
  s.store.getState().onFrame({ type: "design.preview_closed", preview_id: "candidate_a" });
  await s.store.getState().onGeometry({ revision: 4, glb: new ArrayBuffer(4) });
  s.store.getState().submitComposer("Use the other dimension in the drawing");
  const revised = s.prompts.at(-1);
  assert.equal(revised.images[0].data, "drawing");
  assert.equal(revised.targets[0].point[2], 20);
  assert.equal(revised.activeLedgerIds[0], "part");
  assert.equal(revised.baseRevision, 4);
});

test("redo uses accepted revision and saved status survives incoming edits", async () => {
  const s = await setup();
  s.ready(4);
  s.store.setState({ canRedo: true, savedRevision: 4 });
  s.store.getState().redoSession();
  const frame = s.calls.find(call => call.type === "session.redo");
  assert.equal(frame.base_revision, 4);
  assert.equal(s.store.getState().busy, true);
  s.store.getState().onFrame({ type: "ledger.updated", revision: 5, store: {}, can_undo: true, can_redo: false });
  assert.equal(s.store.getState().canRedo, false);
  assert.equal(s.store.getState().savedRevision, 4);
  assert.equal(s.store.getState().sessionRevision, 5);
});

test("server revision is independent of a displayed local preview", async () => {
  const s = await setup();
  s.ready(4);
  await s.store.getState().onGeometry({ revision: 0, glb: new ArrayBuffer(3), source: "local" });
  s.store.getState().submitComposer("edit preview");
  assert.equal(s.prompts.length, 0);
  assert.equal(s.store.getState().sessionRevision, 4);
  s.ready(4);
  s.store.getState().submitComposer("add a hole");
  assert.equal(s.prompts[0].baseRevision, 4);
});

test("reset during STEP import suppresses stale selection meshes and clears worker", async () => {
  const gate = deferred(), started = deferred();
  const s = await setup({ loadStep: async () => { started.resolve(); return gate.promise; } });
  s.ready(1);
  s.store.setState({ kernel: { state: "ready" } });
  const loading = s.store.getState().onGeometry({ revision: 1, glb: new ArrayBuffer(3), step: new ArrayBuffer(3) });
  await started.promise;
  s.ready(0);
  gate.resolve({ volume: 99 });
  await loading;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(s.store.getState().model, null);
  assert.equal(s.events.some(e => e.name === "cadenza:pickmesh"), false);
  assert.ok(s.calls.filter(c => c === "clear").length >= 2);
});

test("overlapping builds publish only the latest worker result", async () => {
  const gate = deferred(), started = deferred();
  let loads = 0;
  const s = await setup({ loadStep: async () => { if (++loads === 1) { started.resolve(); await gate.promise; } return { volume: loads }; } });
  s.ready(1); s.store.setState({ kernel: { state: "ready" } });
  const first = s.store.getState().onGeometry({ revision: 1, glb: new ArrayBuffer(3), step: new ArrayBuffer(3) });
  await started.promise;
  s.store.getState().onFrame({ type: "ledger.updated", revision: 2, store: { part: { revision: 2 } }, can_undo: true });
  const second = s.store.getState().onGeometry({ revision: 2, glb: new ArrayBuffer(3), step: new ArrayBuffer(3) });
  gate.resolve();
  await Promise.all([first, second]);
  assert.equal(s.store.getState().model.revision, 2);
  const meshes = s.events.filter(e => e.name === "cadenza:pickmesh");
  assert.equal(meshes.length, 1);
  assert.equal(meshes[0].detail.generation, s.store.getState().geometryGeneration);
});

test("selected sketch identity reaches prompts and ambiguous surfaces cannot submit", async () => {
  const s = await setup(); s.ready(2);
  await s.store.getState().onGeometry({ revision: 2, glb: new ArrayBuffer(3) });
  const identity = { version: 1, ledger_id: "part", source_ledger_id: "part", source_part_id: null,
    component_path: [], feature_id: "feat_extrusion", feature_kind: "extrude", operation: "add",
    profile_id: "feat_profile", entity_id: "ent_bore", role: "bore" };
  const selection = { mode: "face", faceHash: 1, point: [10, 0, 4], normal: [-1, 0, 0], screen: { x: 0, y: 0 }, surface_reference: identity };
  s.store.getState().select(selection);
  s.store.getState().submitComposer("Enlarge this opening");
  assert.deepEqual(s.prompts[0].targets[0].surface_reference, identity);
  s.store.setState({ busy: false });
  s.store.getState().select(selection);
  s.store.getState().submitPrompt("Enlarge this opening");
  assert.deepEqual(s.prompts[1].targets[0].surface_reference, identity);
  s.store.setState({ busy: false });
  s.store.getState().select({ ...selection, surface_reference: undefined, selection_error: "Ambiguous surface" });
  s.store.getState().submitComposer("Change it");
  s.store.getState().submitPrompt("Change it");
  assert.equal(s.prompts.length, 2);
});

test("empty undo clears visual state while retaining its accepted revision", async () => {
  const s = await setup(); s.ready(2);
  await s.store.getState().onGeometry({ revision: 2, glb: new ArrayBuffer(3) });
  s.store.getState().onFrame({ type: "ledger.updated", revision: 3, store: { part: { revision: 3 } }, can_undo: false });
  s.store.getState().onFrame({ type: "geometry.ready", revision: 3, empty: true, blobs: [] });
  assert.equal(s.store.getState().model, null);
  assert.equal(s.store.getState().sessionRevision, 3);
  assert.equal(s.store.getState().busy, false);
});


async function assemblySetup(version = 2) {
  const s = await partSetup();
  s.store.setState({ assemblyVersion: version });
  s.store.getState().discoverAssembly();
  const cap = { type: "design.assembly.capabilities", message_id: s.store.getState().assemblyDiscovery,
    session_id: "ses_test", revision: 0, ledger_id: "root", version, owner_path: [], max_subassemblies: 8, max_initial_children: 16, can_initialize: true, can_edit: false,
    reason: null, max_operations: 16, max_components: 32, max_leaves: 128, max_depth: 8,
    operations: ["initialize_assembly", "add_instance", "duplicate_instance", "set_instance_placement", "rename_instance", "remove_instance", "add_rigid_joint", "remove_joint", "create_subassembly", ...(version === 3 ? ["add_planar_attachment"] : [])] };
  return { ...s, cap };
}
const assemblyOps = [{ op: "initialize_assembly", name: "Fixture" }, { op: "add_instance", name: "Plate", source_ledger_id: "part",
  placement: { origin: [0,0,0], rotation_deg: [0,0,0] } }];

async function assemblyPromptSetup() {
  const s = await assemblySetup(); s.store.getState().onFrame(s.cap);
  const response = { type: "design.assembly.prompt.capabilities", message_id: s.store.getState().assemblyPromptDiscovery,
    session_id: "ses_test", revision: 0, ledger_id: "root", owner_path: [], version: 1,
    max_operations: 16, target_modes: ["definition", "instance", "attachment"], available: true, vision_available: true, reason: null };
  return { ...s, response };
}

test("assembly AI requires both discoveries and correlates every context field", async () => {
  const s = await assemblyPromptSetup();
  s.store.getState().startAssemblyPrompt({ kind: "definition" });
  assert.equal(s.store.getState().assemblyPromptTarget, null);
  for (const change of [{ session_id: "old" }, { revision: 2 }, { ledger_id: "part" }, { owner_path: ["comp_old"] }, { message_id: "old" }]) {
    s.store.getState().onFrame({ ...s.response, ...change }); assert.equal(s.store.getState().assemblyPromptCapabilities, null);
  }
  s.store.getState().onFrame(s.response); s.store.getState().startAssemblyPrompt({ kind: "definition" });
  const before = structuredClone(s.store.getState().ledgers);
  s.store.setState({ selections: [{ point: [1,2,3] }], attachments: [{ media_type: "image/png", data: "AAAA", name: "drawing", previewUrl: "thumb" }] });
  s.store.getState().submitComposer("Add the accepted plate at X=20 mm");
  const sent = s.calls.findLast(f => f.type === "design.assembly.prompt");
  assert.equal(sent.ledger_id, "root"); assert.deepEqual(structuredClone(sent.owner_path), []); assert.equal(sent.base_revision, 0);
  assert.deepEqual(sent.target, { kind: "definition" }); assert.equal(sent.images.length, 1); assert.equal(sent.targets, undefined);
  assert.equal(s.prompts.length, 0); assert.deepEqual(s.store.getState().ledgers, before);
  assert.deepEqual(s.store.getState().previewInput.assemblyPromptTarget.target, { kind: "definition" });
});

test("assembly target invalidation never falls through to legacy composer", async () => {
  for (const cause of ["revision", "disconnect", "local", "source"]) {
    const s = await assemblyPromptSetup(); s.store.getState().onFrame(s.response);
    s.store.getState().startAssemblyPrompt({ kind: "definition" });
    if (cause === "revision") s.store.getState().onFrame({ type: "ledger.updated", revision: 1, store: s.store.getState().ledgers, can_undo: true });
    if (cause === "disconnect") s.connection("closed");
    if (cause === "local") await s.store.getState().onGeometry({ revision: 0, glb: new ArrayBuffer(3), source: "local" });
    if (cause === "source") s.store.getState().selectSource("part");
    assert.equal(s.store.getState().assemblyPromptTarget, null);
    assert.equal(s.store.getState().assemblyPromptBlocked, true);
    s.store.getState().submitComposer("Retained assembly text");
    assert.equal(s.prompts.length, 0); assert.equal(s.calls.filter(f => f.type === "design.assembly.prompt").length, 0);
  }
});

test("assembly discovery errors and late mutation frames cannot release another busy request", async () => {
  const s = await assemblyPromptSetup(); s.store.setState({ busy: true });
  s.store.getState().onFrame({ type: "error", message_id: s.response.message_id, code: "BAD_MESSAGE", message: "unsupported" });
  assert.equal(s.store.getState().busy, true); assert.equal(s.store.getState().assemblyPromptCapabilities, null);
  s.store.getState().onFrame({ type: "error", message_id: s.response.message_id, code: "BAD_MESSAGE", message: "late" });
  assert.equal(s.store.getState().busy, true);
  s.store.getState().onFrame({ type: "agent.status", message_id: "e3_old", phase: "done" });
  assert.equal(s.store.getState().busy, true);
});

test("assembly instance selection remains pinned and unauthorized detach targets cannot be selected", async () => {
  const s = await assemblyPromptSetup(); s.store.getState().onFrame(s.response);
  s.store.setState({ ledgers: { ...s.store.getState().ledgers, root: { project_id: "root", features: [], assembly: {
    components: [{ id: "comp_a", sub_assembly_id: "part" }, { id: "comp_b", sub_assembly_id: "part" }], joints: [], parts: [] } } } });
  s.store.getState().startAssemblyPrompt({ kind: "instance", component_id: "comp_a", detach_joint_ids: ["joint_other"] });
  assert.equal(s.store.getState().assemblyPromptTarget, null);
  s.store.getState().startAssemblyPrompt({ kind: "instance", component_id: "comp_a", detach_joint_ids: [] });
  s.store.getState().selectAssembly("comp_b");
  s.store.getState().submitComposer("Rename this instance");
  assert.equal(s.calls.findLast(f => f.type === "design.assembly.prompt").target.component_id, "comp_a");
});

test("assembly image-only requests and unavailable vision do not send", async () => {
  const s = await assemblyPromptSetup(); s.store.getState().onFrame({ ...s.response, vision_available: false });
  s.store.getState().startAssemblyPrompt({ kind: "definition" });
  s.store.setState({ attachments: [{ media_type: "image/png", data: "AAAA" }] });
  assert.equal(s.store.getState().submitAssemblyPrompt(" "), false);
  assert.equal(s.store.getState().submitAssemblyPrompt("Add this part"), false);
  assert.equal(s.calls.filter(f => f.type === "design.assembly.prompt").length, 0);
});

test("assembly Revise restores pinned context and images and blocks competing geometry", async () => {
  const s = await assemblyPromptSetup(); s.store.getState().onFrame(s.response);
  s.store.getState().startAssemblyPrompt({ kind: "definition" });
  const images = [{ media_type: "image/png", data: "AAAA", previewUrl: "thumbnail" }];
  s.store.setState({ attachments: images });
  s.store.getState().submitAssemblyPrompt("Add support");
  const context = s.store.getState().assemblyPromptTarget;
  await s.store.getState().onGeometry({ revision: 0, glb: new ArrayBuffer(2), source: "server" });
  assert.equal(s.store.getState().busy, true); assert.equal(s.store.getState().model, null);
  s.store.setState({ busy: false, preview: { preview_id: "e3preview", base_revision: 0, prompt: "Add support" } });
  s.store.getState().decidePreview("cancel", true);
  assert.equal(s.store.getState().reviseInput.assemblyPromptTarget, context);
  s.store.getState().onFrame({ type: "design.preview_closed", preview_id: "e3preview" });
  await s.store.getState().onGeometry({ revision: 0, glb: new ArrayBuffer(2), source: "server" });
  assert.equal(s.store.getState().assemblyPromptTarget, context); assert.equal(s.store.getState().attachments, images);
  assert.equal(s.events.findLast(e => e.name === "cadenza:revise").detail, "Add support");
  s.store.getState().submitComposer("Add support farther away");
  assert.equal(s.calls.findLast(f => f.type === "design.assembly.prompt").images.length, 1);
  assert.equal(s.prompts.length, 0);
});

test("assembly submission rechecks lifecycle guards and provider context", async () => {
  const s = await assemblyPromptSetup(); s.store.getState().onFrame(s.response);
  s.store.getState().startAssemblyPrompt({ kind: "definition" });
  const baseline = s.store.getState();
  for (const guard of [{ busy: true }, { draft: emptyDraft }, { draftStarting: true }, { preview: {} },
    { connection: "closed" }, { sourceLoading: true }, { sourceRequest: {} }, { selectedSourceId: "part" },
    { model: { source: "local" } }, { assemblyPromptCapabilities: null }, { assemblyCapabilities: null },
    { sessionId: "other" }, { sessionRevision: 2 }, { assemblyOwnerPath: ["comp_other"] }, { assemblyOwnerId: "other" }]) {
    s.store.setState(guard); assert.equal(s.store.getState().submitAssemblyPrompt("Add support"), false);
    s.store.setState(baseline);
  }
  assert.equal(s.store.getState().submitAssemblyPrompt("x".repeat(4001)), false);
  assert.equal(s.calls.filter(f => f.type === "design.assembly.prompt").length, 0);
});

test("late accepted-geometry or preview-close frames cannot release an assembly prompt", async () => {
  for (const frame of [{ type: "geometry.ready", revision: 0, empty: true, blobs: [] },
    { type: "design.preview_closed", preview_id: "old" }, { type: "geometry.failed", code: "GEOMETRY_KERNEL_FAILURE", message: "old" },
    { type: "agent.status", phase: "done" }]) {
    const s = await assemblyPromptSetup(); s.store.getState().onFrame(s.response);
    s.store.getState().startAssemblyPrompt({ kind: "definition" }); s.store.getState().submitAssemblyPrompt("Add support");
    s.store.getState().onFrame(frame); assert.equal(s.store.getState().busy, true, frame.type);
    assert.ok(s.store.getState().assemblyPromptRequest, frame.type);
  }
});

test("assembly discovery is session/revision/owner correlated and old-server safe", async () => {
  const s = await assemblySetup();
  for (const change of [{ session_id: "old" }, { revision: 9 }, { ledger_id: "part" }, { message_id: "old" }]) {
    s.store.getState().onFrame({ ...s.cap, ...change }); assert.equal(s.store.getState().assemblyCapabilities, null);
  }
  s.store.getState().onFrame(s.cap); assert.ok(s.store.getState().assemblyCapabilities);
  s.store.getState().discoverAssembly(); s.store.setState({ busy: true });
  s.store.getState().onFrame({ type: "error", code: "BAD_MESSAGE", message: "unsupported",
    detail: "Input tag 'design.assembly.capabilities' not recognized" });
  assert.equal(s.store.getState().assemblyCapabilities, null); assert.equal(s.store.getState().busy, true);
});

test("assembly commands send explicit primary and revision without changing ledgers", async () => {
  const s = await assemblySetup(); s.store.getState().onFrame(s.cap);
  const before = s.store.getState().ledgers;
  s.store.getState().previewAssembly(assemblyOps);
  const frame = s.calls.find(c => c.type === "design.assembly.command");
  assert.deepEqual(frame.operations, assemblyOps); assert.equal(frame.ledger_id, "root");
  assert.equal(frame.scope, "assembly_definition"); assert.equal(frame.base_revision, 0);
  assert.equal(s.store.getState().ledgers, before); assert.equal(s.store.getState().busy, true);
  s.store.getState().onFrame({ type: "error", message_id: "asm_old", code: "BAD_MESSAGE", message: "old" });
  assert.equal(s.store.getState().busy, true);
  s.store.getState().onFrame({ type: "error", message_id: frame.message_id, code: "BAD_MESSAGE", message: "Rejected overlap" });
  assert.equal(s.store.getState().busy, false); assert.equal(s.store.getState().assemblyRequest, null);
  assert.equal(s.store.getState().ledgers, before);
});

test("assembly mutations and selection respect every unavailable state", async () => {
  for (const guard of [{ busy: true }, { preview: {} }, { draft: {} }, { draftStarting: true }, { sourceLoading: true },
    { selectedSourceId: "part" }, { sourceRequest: {id:"view",ledgerId:"part"} }, { connection: "closed" },
    { model: { source: "local" } }, { assemblyCapabilities: null }]) {
    const s = await assemblySetup(); s.store.getState().onFrame(s.cap); s.store.setState(guard);
    s.store.getState().previewAssembly(assemblyOps);
    assert.equal(s.calls.filter(c => c.type === "design.assembly.command").length, 0, JSON.stringify(guard));
  }
});

test("existing assembly edits wait for the accepted project display", async () => {
  const s = await assemblySetup();
  s.store.getState().onFrame({ ...s.cap, can_initialize: false, can_edit: true });
  const operations = [{ op: "rename_instance", component_id: "comp_a", name: "Renamed" }];
  for (const model of [null, { source: "source" }, { source: "preview" }]) {
    s.store.setState({ model }); s.store.getState().previewAssembly(operations);
    assert.equal(s.calls.filter(c => c.type === "design.assembly.command").length, 0);
  }
  s.store.setState({ model: { source: "server" } }); s.store.getState().previewAssembly(operations);
  assert.equal(s.calls.filter(c => c.type === "design.assembly.command").length, 1);
});

test("assembly selections retain full occurrence paths and clear on source/revision/session/disconnect", async () => {
  const s = await assemblySetup(); s.store.getState().onFrame(s.cap);
  s.store.getState().selectAssembly("comp_outer/comp_inner");
  assert.equal(s.store.getState().assemblySelection, "comp_outer/comp_inner");
  s.store.getState().selectSource("part"); assert.equal(s.store.getState().assemblySelection, null);
  s.store.setState({ assemblySelection: "comp_a", selectedSourceId: null });
  s.store.getState().onFrame({ type:"ledger.updated", revision:1, can_undo:true, store:s.store.getState().ledgers });
  assert.equal(s.store.getState().assemblySelection,null); assert.equal(s.store.getState().assemblyCapabilities,null);
  s.store.setState({assemblySelection:"comp_b"}); s.connection("closed");
  assert.equal(s.store.getState().assemblySelection,null); assert.equal(s.store.getState().assemblyRequest,null);
  s.connection("open"); s.store.setState({assemblySelection:"comp_c"}); s.ready();
  assert.equal(s.store.getState().assemblySelection,null);
});


test("E2 owner context requires an exact accepted path and survives only matching revisions", async () => {
  const s = await assemblySetup(); s.store.getState().onFrame(s.cap);
  const component = (id, source) => ({ id, sub_assembly_id: source });
  const ledgers = { root: { project_id: "root", features: [], assembly: { components: [component("comp_a","inner"),component("comp_b","inner")] } },
    inner: { project_id: "inner", features: [], assembly: { components: [component("comp_child","part")] } }, part: commandLedger };
  s.store.setState({ ledgers, model: { source: "server" } });
  s.store.getState().editAssemblyOwner("inner", []); assert.equal(s.store.getState().assemblyOwnerId,"root");
  s.store.getState().editAssemblyOwner("inner", ["comp_a"]);
  assert.equal(s.store.getState().assemblyOwnerId,"inner");
  const request=s.calls.at(-1); assert.equal(request.version,2); assert.deepEqual(Array.from(request.owner_path),["comp_a"]);
  const cap={...s.cap,message_id:request.message_id,ledger_id:"inner",owner_path:["comp_a"],can_initialize:false,can_edit:true};
  s.store.getState().onFrame({...cap,owner_path:["comp_b"]}); assert.equal(s.store.getState().assemblyCapabilities,null);
  s.store.getState().onFrame(cap); assert.ok(s.store.getState().assemblyCapabilities);
  s.store.getState().previewAssembly([{op:"rename_instance",component_id:"comp_child",name:"New"}]);
  const mutation=s.calls.at(-1); assert.equal(mutation.ledger_id,"inner"); assert.deepEqual(Array.from(mutation.owner_path),["comp_a"]);
  s.store.getState().onFrame({type:"ledger.updated",message_id:mutation.message_id,revision:1,store:ledgers,can_undo:true});
  assert.equal(s.store.getState().assemblyOwnerId,"inner"); assert.equal(s.store.getState().assemblySelection,null);
  const changed={...ledgers,root:{...ledgers.root,assembly:{components:[component("comp_b","inner")]}}};
  s.store.getState().onFrame({type:"ledger.updated",message_id:"refresh",revision:2,store:changed,can_undo:true});
  assert.equal(s.store.getState().assemblyOwnerId,"root"); assert.deepEqual(Array.from(s.store.getState().assemblyOwnerPath),[]);
});

test("E2 discovery falls back once to v1 primary without unlocking a mutation", async () => {
  const s=await assemblySetup(); s.store.setState({busy:true});
  s.store.getState().onFrame({type:"error",message_id:s.cap.message_id,code:"BAD_MESSAGE",message:"v2 unsupported"});
  const fallback=s.calls.at(-1); assert.equal(fallback.type,"design.assembly.capabilities"); assert.equal(fallback.version,undefined);
  assert.equal(s.store.getState().busy,true); assert.equal(s.store.getState().assemblyVersion,1);
  const {owner_path,max_subassemblies,max_initial_children,...cap}=s.cap;
  s.store.getState().onFrame({...cap,message_id:fallback.message_id,version:1,operations:cap.operations.slice(0,6)});
  assert.equal(s.store.getState().assemblyCapabilities.version,1);
  s.store.getState().discoverAssembly(); const last=s.calls.at(-1); const count=s.calls.length;
  s.store.getState().onFrame({type:"error",message_id:last.message_id,code:"BAD_MESSAGE",message:"unsupported"});
  assert.equal(s.calls.length,count); assert.equal(s.store.getState().assemblyCapabilities,null); assert.equal(s.store.getState().busy,true);
});

test("E2 nested discovery rejection cannot fall back or change busy", async () => {
  const s=await assemblySetup(); s.store.setState({assemblyOwnerPath:["comp_outer"],assemblyOwnerId:"nested",busy:true});
  const count=s.calls.length;
  s.store.getState().onFrame({type:"error",message_id:s.cap.message_id,code:"BAD_MESSAGE",message:"unsupported"});
  assert.equal(s.calls.length,count); assert.equal(s.store.getState().busy,true); assert.equal(s.store.getState().assemblyCapabilities,null);
});


test("E1 server field rejection identifies v2 discovery without confusing unrelated capabilities", async () => {
  const s=await assemblySetup(); s.store.setState({busy:true});
  const part=s.store.getState().partCapabilities;
  s.store.getState().onFrame({type:"error",code:"BAD_MESSAGE",message:"Could not parse that frame.",
    detail:"3 validation errors for tagged-union[...]\ndesign.assembly.capabilities.version\n  Extra inputs are not permitted\ndesign.assembly.capabilities.ledger_id\n  Extra inputs are not permitted"});
  assert.equal(s.store.getState().assemblyVersion,1);assert.equal(s.store.getState().busy,true);
  assert.equal(s.store.getState().partCapabilities,part);
  assert.equal(s.calls.at(-1).version,undefined);
});


test("presentation errors retain raw evidence and request identity without changing accepted state", async () => {
  const { store, ready } = await setup(); ready(3);
  const accepted = store.getState().ledgers;
  store.getState().onFrame({ type: "error", code: "LEDGER_INVALID", message: "Invalid proposal", detail: "features.3.radius missing", message_id: "review_error", recoverable: true });
  const error = store.getState().conversation.at(-1);
  assert.equal(error.text, "LEDGER_INVALID: Invalid proposal (features.3.radius missing)");
  assert.equal(error.error.code, "LEDGER_INVALID");
  assert.equal(error.error.requestId, "review_error");
  assert.equal(error.error.detail, "features.3.radius missing");
  store.getState().onFrame({ type: "geometry.failed", code: "GEOMETRY_KERNEL_FAILURE", message: "Kernel rejected cut", message_id: "review_geometry", current_revision: 3, feature_id: "feat_hole", detail: { native: "cut" } });
  const geometry = store.getState().conversation.at(-1);
  assert.equal(geometry.error.featureId, "feat_hole");
  assert.equal(geometry.error.detail.native, "cut");
  assert.equal(store.getState().sessionRevision, 3);
  assert.equal(store.getState().ledgers, accepted);
});


test("v3 face controls fall back to v2 without unlocking a mutation", async () => {
  const s = await assemblySetup(3); s.store.setState({ busy: true });
  s.store.getState().onFrame({ type: "error", message_id: s.cap.message_id, code: "BAD_MESSAGE", message: "Unsupported v3" });
  assert.equal(s.store.getState().assemblyVersion, 2);
  assert.equal(s.store.getState().busy, true);
  const request = s.calls.at(-1); assert.equal(request.version, 2);
  s.store.getState().onFrame({ ...s.cap, version: 2, message_id: request.message_id, operations: s.cap.operations.slice(0,-1) });
  assert.equal(s.store.getState().assemblyCapabilities.version, 2);
});

test("face picker pins endpoints, rejects stale picks and submits through assembly preview", async () => {
  const s = await assemblySetup(3);
  const source = id => ({ ...commandLedger, project_id: id });
  const components = [{id:"comp_a",sub_assembly_id:"part"},{id:"comp_b",sub_assembly_id:"other"}];
  s.store.setState({ ledgers: { root: {project_id:"root",features:[],assembly:{components,joints:[]}}, part:source("part"), other:source("other") },
    model: {source:"server"}, partCapabilities: {...s.store.getState().partCapabilities,source_context:true} });
  s.store.getState().onFrame({...s.cap,can_initialize:false,can_edit:true});
  s.store.getState().startFaceAttachment("comp_a","comp_b");
  assert.equal(s.store.getState().selectedSourceId,"part"); assert.equal(s.store.getState().faceAttachment.step,"a");
  const pick = (id,path,role,normal) => ({mode:"face",faceHash:1,point:[0,0,0],normal,
    source:{ledgerId:id,revision:0,generation:s.store.getState().geometryGeneration,componentPath:[path]},
    surface_reference:{version:1,ledger_id:id,source_ledger_id:id,source_part_id:null,component_path:[],feature_id:"feat_box",feature_kind:"box",operation:"add",role}});
  s.store.setState({ sourceLoading:false,sourceRequest:null,selections:[pick("part","comp_a","top_face",[0,0,1])] });
  const saved=s.store.getState().selections;
  s.store.setState({selections:[{...saved[0],source:{...saved[0].source,generation:-1}}]});
  s.store.getState().useAttachmentFace(); assert.equal(s.store.getState().faceAttachment.a,undefined);
  s.store.setState({selections:saved}); s.store.getState().useAttachmentFace();
  assert.ok(s.store.getState().faceAttachment.a); assert.equal(s.store.getState().selectedSourceId,"other");
  s.store.setState({sourceLoading:false,sourceRequest:null,selections:[pick("other","comp_b","bottom_face",[0,0,-1])]});
  s.store.getState().useAttachmentFace(); s.store.getState().previewFaceAttachment();
  const sent=s.calls.find(c=>c.type==="design.assembly.command");
  assert.equal(sent.operations[0].op,"add_planar_attachment"); assert.equal(sent.ledger_id,"root");
  assert.equal(sent.operations[0].target_a.reference.source_ledger_id,"part");
  assert.equal(sent.operations[0].target_b.reference.source_ledger_id,"other");
  assert.equal(s.store.getState().busy,true);
});
