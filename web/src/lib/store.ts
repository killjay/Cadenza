/**
 * App state. One zustand store, actions that own their own async, and a
 * conversation array the panes render straight through.
 *
 * The three.js viewport is NOT in here. Scene objects are mutable, per-frame,
 * and enormous; putting them in React state means re-rendering the tree on
 * camera moves. The viewport is a ref (see components/Viewport.tsx); the store
 * holds only what the UI has to draw.
 *
 * The ledger is NOT patched locally either. It is server-authoritative — the
 * store adopts whatever `ledger.updated` carries and never edits it in place.
 */
import { create } from "zustand";
import { occt } from "../workers/occtClient";
import type { FaceFacts, ModelSummary } from "../workers/occtClient";
import { prepareImage } from "./image";
import type { PreparedImage } from "./image";
import { socket } from "./socket";
import type { ConnectionState, GeometryPayload } from "./socket";
import type { FaceSelection, Ledger, Limits, ServerFrame, SurfaceCheck, Vec3, PreviewReady, ParameterEdit } from "./types";
import type { SketchDraft, DraftOperation, DraftProposal } from "./types";
import type { CapabilitiesResponse, CommandOperation, PatternCapabilitiesResponse, PartCapabilitiesResponse, PartViewReady, PartOperation } from "./types";
import type { AssemblyCapabilitiesResponse, AssemblyOperation, AssemblyPromptTarget, AssemblyPromptCapabilitiesResponse } from "./types";
import { assemblyOwner, validAssemblyPrompt } from "./assembly";
import type { AssemblyPromptContext } from "./assembly";
import { commandAvailable, simpleFeature, isPattern, patternReason } from "./featureCommands";

export interface ConvItem {
  id: string;
  kind: "user" | "assistant" | "status" | "error";
  text: string;
  /** Where the user was pointing when they said it. */
  point?: Vec3;
  /** Thumbnails of any sketches sent with the message. */
  images?: string[];
  /** Numbers the agent inferred rather than was told. */
  assumptions?: { field: string; value: unknown; basis: string; confidence: number }[];
}

export interface KernelStatus {
  state: "cold" | "booting" | "ready" | "failed";
  bootMs?: number;
  error?: string;
}

export interface ModelStatus {
  revision: number;
  source?: "server" | "local" | "preview" | "source";
  glbBytes: number;
  stepBytes: number;
  parseMs?: number;
  triangleCount?: number;
  summary?: ModelSummary;
  pickMs?: number;
}

interface PreviewInput {
  assemblyPromptTarget?: AssemblyPromptContext;
  selections: FaceSelection[];
  attachments: PreparedImage[];
  activeLedgerIds: string[];
  featurePromptTarget?: { ledgerId: string; featureId?: string };
  newPartName?: string;
}

const DEFAULT_LIMITS: Limits = {
  max_prompt_chars: 4000,
  max_patch_ops: 32,
  max_features: 64,
  build_timeout_s: 20,
  max_images: 4,
  max_image_bytes: 5_000_000,
};

let seq = 0;
let surfaceRequest = 0;
let previewRequest = 0;
const nextId = (): string => `i${++seq}`;
// A worker holds one shape: serialize complete import/query sequences.
let workerQueue: Promise<void> = Promise.resolve();

interface State {
  assemblyPromptCapabilities: AssemblyPromptCapabilitiesResponse | null;
  assemblyPromptDiscovery: string | null;
  assemblyPromptTarget: AssemblyPromptContext | null;
  assemblyPromptBlocked: boolean;
  assemblyPromptRequest: { id: string; session: string; revision: number; context: AssemblyPromptContext } | null;
  invalidateAssemblyPrompt: () => void;
  startAssemblyPrompt: (target: AssemblyPromptTarget | null) => void;
  submitAssemblyPrompt: (text: string) => boolean;
  assemblyCapabilities: AssemblyCapabilitiesResponse | null;
  assemblyDiscovery: string | null;
  assemblyRequest: string | null;
  assemblySelection: string | null;
  assemblyOwnerId: string | null;
  assemblyOwnerPath: string[];
  assemblyVersion: 1 | 2;
  editAssemblyOwner: (ledgerId: string, path: string[]) => void;
  discoverAssembly: () => void;
  selectAssembly: (path: string | null) => void;
  previewAssembly: (operations: AssemblyOperation[]) => void;
  primaryLedgerId: string | null;
  partCapabilities: PartCapabilitiesResponse | null;
  partDiscovery: string | null;
  partRequest: string | null;
  newPartName: string | null;
  selectedSourceId: string | null;
  sourceRequest: { id: string; ledgerId: string } | null;
  sourceView: PartViewReady | null;
  sourceLoading: boolean;
  sourceError: string | null;
  selectSource: (id: string | null, preserveInput?: boolean) => void;
  loadSourceView: (frame: PartViewReady) => Promise<void>;
  createPart: (name: string, operations: PartOperation[]) => void;
  startPartPrompt: (name: string | null) => void;
  submitPartPrompt: (text: string) => boolean;

  patternCapabilities: PatternCapabilitiesResponse | null;
  patternDiscovery: string | null;
  featurePromptAvailable: boolean;
  featurePromptDiscovery: string | null;
  featurePromptRequest: string | null;
  featurePromptTarget: { ledgerId: string; featureId?: string } | null;
  startFeaturePrompt: (ledgerId: string | null, featureId?: string) => void;
  submitFeaturePrompt: (text: string) => boolean;
  capabilities: CapabilitiesResponse | null;
  capabilityRequest: string | null;
  previewFeatureCommand: (ledgerId: string, operations: CommandOperation[]) => void;
  draft: SketchDraft | null;
  draftProposal: DraftProposal | null;
  promptSketch: (text: string, reply?: boolean) => void;
  decideSketchProposal: (action: "update" | "cancel") => void;
  draftEntityId: string | null;
  draftFocus: { field: string; request: number } | null;
  selectDraftEntity: (id: string, field?: string) => void;
  draftRequest: string | null;
  draftStarting: boolean;
  draftError: string | null;
  startDraft: (profileId?: string) => void;
  updateDraft: (operations: DraftOperation[]) => void;
  actOnDraft: (action: "finish" | "discard") => void;
  kernel: KernelStatus;
  connection: ConnectionState;
  connectionDetail: string;
  sessionId: string | null;
  sessionRevision: number;
  canUndo: boolean;
  canRedo: boolean;
  projectName: string;
  savedRevision: number | null;
  geometryGeneration: number;
  limits: Limits;
  agentsAvailable: boolean;
  ledgers: Record<string, Ledger>;
  model: ModelStatus | null;
  preview: PreviewReady | null;
  previewLoading: boolean;
  previewError: string | null;
  previewInput: PreviewInput | null;
  reviseInput: PreviewInput | null;
  loadPreview: (preview: PreviewReady) => Promise<void>;
  decidePreview: (action: "apply" | "cancel", revise?: boolean) => void;
  editParameter: (ledgerId: string, featureId: string, parameter: ParameterEdit["parameter"], value: number) => void;
  editNamedParameter: (ledgerId: string, parameter: string, value: number) => void;
  editSketchDimension: (ledgerId: string, featureId: string, constraintId: string, value: number) => void;
  /** Rises while a turn is in flight. */
  busy: boolean;
  phase: string | null;
  conversation: ConvItem[];
  selections: FaceSelection[];
  surfaceChecks: Record<string, SurfaceCheck>;
  surfaceChecking: string | null;
  checkSurface: (ledgerId: string, referenceId: string, select?: boolean) => Promise<void>;
  promptOpen: boolean;
  showGrid: boolean;
  showEdges: boolean;
  verify: { faceHash: number; queryMs: number; agrees: boolean } | null;

  /** Sketches staged for the next prompt. */
  attachments: PreparedImage[];
  attaching: boolean;

  bootKernel: () => Promise<void>;
  connect: () => void;
  onFrame: (frame: ServerFrame) => void;
  onGeometry: (payload: GeometryPayload) => Promise<void>;
  clearGeometry: () => void;
  loadStep: (step: ArrayBuffer, generation?: number) => Promise<void>;
  select: (sel: FaceSelection | FaceSelection[] | null) => void;
  setFaceFacts: (facts: FaceFacts) => void;
  openPrompt: (open: boolean) => void;
  submitPrompt: (text: string) => void;
  submitComposer: (text: string) => void;
  addFiles: (files: File[]) => Promise<void>;
  removeAttachment: (index: number) => void;
  clearAttachments: () => void;
  resetSession: () => void;
  undoSession: (revision?: number) => void;
  redoSession: () => void;
  setVerify: (v: State["verify"]) => void;
  toggleGrid: () => void;
  toggleEdges: () => void;
  say: (item: Omit<ConvItem, "id">) => void;
  activeLedgerIds: string[];
  setActiveLedgerIds: (ids: string[]) => void;
}

export const useStore = create<State>((set, get) => ({
  assemblyPromptCapabilities: null, assemblyPromptDiscovery: null, assemblyPromptTarget: null,
  assemblyPromptBlocked: false, assemblyPromptRequest: null,
  invalidateAssemblyPrompt() {
    set(s => ({ assemblyPromptCapabilities: null, assemblyPromptDiscovery: null, assemblyPromptRequest: null,
      assemblyPromptTarget: null, assemblyPromptBlocked: s.assemblyPromptBlocked || !!s.assemblyPromptTarget }));
  },
  startAssemblyPrompt(target) {
    const state = get(), cap = state.assemblyPromptCapabilities;
    if (state.busy || state.preview || state.draft || state.draftStarting || state.sourceLoading) return;
    if (!target) {
      get().clearAttachments();
      set({ assemblyPromptTarget: null, assemblyPromptBlocked: false });
      window.dispatchEvent(new CustomEvent("cadenza:revise", { detail: "" }));
      return;
    }
    const context = { ledgerId: state.assemblyOwnerId!, ownerPath: [...state.assemblyOwnerPath], target };
    if (!cap?.available || state.assemblyCapabilities?.version !== 2 || state.connection !== "open"
      || state.selectedSourceId || state.model?.source === "local" || !state.primaryLedgerId
      || !validAssemblyPrompt(state.ledgers, state.primaryLedgerId, context)) return;
    set({ assemblyPromptTarget: context, assemblyPromptBlocked: false, featurePromptTarget: null,
      newPartName: null, selections: [], promptOpen: false });
    window.dispatchEvent(new CustomEvent("cadenza:revise", { detail: "" }));
  },
  submitAssemblyPrompt(text) {
    const state = get(), cap = state.assemblyPromptCapabilities, context = state.assemblyPromptTarget, prompt = text.trim();
    if (!context || !cap?.available || state.assemblyCapabilities?.version !== 2 || !state.primaryLedgerId || !state.sessionId
      || state.connection !== "open" || state.busy || state.preview || state.draft || state.draftStarting
      || state.selectedSourceId || state.sourceLoading || state.sourceRequest || state.model?.source === "local"
      || (state.assemblyCapabilities.can_edit && state.model?.source !== "server")
      || cap.session_id !== state.sessionId || cap.revision !== state.sessionRevision || cap.ledger_id !== context.ledgerId
      || context.ledgerId !== state.assemblyOwnerId || JSON.stringify(context.ownerPath) !== JSON.stringify(state.assemblyOwnerPath)
      || JSON.stringify(cap.owner_path) !== JSON.stringify(context.ownerPath)
      || !validAssemblyPrompt(state.ledgers, state.primaryLedgerId, context)
      || !prompt || prompt.length > state.limits.max_prompt_chars || (state.attachments.length > 0 && !cap.vision_available)) return false;
    const id = `e3_${nextId()}`;
    set({ assemblyPromptRequest: { id, session: state.sessionId, revision: state.sessionRevision, context },
      busy: true, phase: "Interpreting assembly change", previewInput: { selections: [], attachments: state.attachments,
        activeLedgerIds: state.activeLedgerIds, assemblyPromptTarget: context } });
    socket.send({ type: "design.assembly.prompt", message_id: id, base_revision: state.sessionRevision,
      ledger_id: context.ledgerId, owner_path: context.ownerPath, scope: "assembly_definition", target: context.target,
      prompt, images: state.attachments.map(({ media_type, data, name }) => ({ media_type, data, name })) });
    get().say({ kind: "user", text: `Assembly: ${prompt}`, images: state.attachments.map(a => a.previewUrl) });
    return true;
  },
  assemblyCapabilities: null, assemblyDiscovery: null, assemblyRequest: null, assemblySelection: null,
  assemblyOwnerId: null, assemblyOwnerPath: [], assemblyVersion: 2,
  primaryLedgerId: null, partCapabilities: null, partDiscovery: null, partRequest: null, newPartName: null,
  selectedSourceId: null, sourceRequest: null, sourceView: null, sourceLoading: false, sourceError: null,
  capabilities: null, capabilityRequest: null,
  patternCapabilities: null, patternDiscovery: null,
  featurePromptAvailable: false, featurePromptDiscovery: null, featurePromptRequest: null, featurePromptTarget: null,
  draft: null, draftProposal: null, draftEntityId: null, draftFocus: null, draftRequest: null, draftStarting: false, draftError: null,
  kernel: { state: "cold" },
  connection: "idle",
  connectionDetail: "",
  sessionId: null,
  sessionRevision: 0,
  canUndo: false,
  canRedo: false,
  projectName: "Untitled Part",
  savedRevision: null,
  geometryGeneration: 0,
  limits: DEFAULT_LIMITS,
  agentsAvailable: true,
  ledgers: {},
  model: null,
  preview: null,
  previewLoading: false,
  previewError: null,
  previewInput: null,
  reviseInput: null,
  busy: false,
  phase: null,
  conversation: [],
  selections: [],
  surfaceChecks: {},
  surfaceChecking: null,
  promptOpen: false,
  showGrid: true,
  showEdges: true,
  verify: null,
  attachments: [],
  attaching: false,
  activeLedgerIds: [],
  setActiveLedgerIds: (ids) => set({ activeLedgerIds: ids }),

  discoverAssembly() {
    const state = get();
    if (!state.primaryLedgerId || state.connection !== "open") return;
    const valid = state.assemblyOwnerId && assemblyOwner(state.ledgers, state.primaryLedgerId, state.assemblyOwnerPath) === state.assemblyOwnerId;
    const ledgerId = valid ? state.assemblyOwnerId! : state.primaryLedgerId, path = valid ? state.assemblyOwnerPath : [];
    const id = nextId();
    set({ assemblyPromptCapabilities: null, assemblyPromptDiscovery: null });
    set({ assemblyCapabilities: null, assemblyDiscovery: id, assemblyOwnerId: ledgerId, assemblyOwnerPath: path });
    socket.send(state.assemblyVersion === 1 ? { type: "design.assembly.capabilities", message_id: id }
      : { type: "design.assembly.capabilities", message_id: id, version: 2, ledger_id: ledgerId, owner_path: path });
  },
  editAssemblyOwner(ledgerId, path) {
    const state = get();
    if (!state.primaryLedgerId || state.assemblyVersion !== 2 || state.busy || state.preview || state.draft
      || state.draftStarting || state.sourceLoading || state.sourceRequest || state.selectedSourceId
      || state.connection !== "open" || state.model?.source !== "server"
      || assemblyOwner(state.ledgers, state.primaryLedgerId, path) !== ledgerId) return;
    get().invalidateAssemblyPrompt();
    set({ assemblyOwnerId: ledgerId, assemblyOwnerPath: [...path], assemblySelection: null, selections: [], promptOpen: false });
    get().discoverAssembly();
  },
  selectAssembly(path) {
    const state = get();
    if (state.busy || state.preview || state.draft || state.draftStarting || state.sourceLoading
      || state.selectedSourceId || state.connection !== "open" || state.model?.source === "local") return;
    set({ assemblySelection: path, selections: [], promptOpen: false });
  },
  previewAssembly(operations) {
    const state = get(), cap = state.assemblyCapabilities;
    if (!cap || (!cap.can_edit && !cap.can_initialize) || cap.session_id !== state.sessionId
      || cap.revision !== state.sessionRevision || cap.ledger_id !== state.assemblyOwnerId
      || (cap.version === 2 && JSON.stringify(cap.owner_path) !== JSON.stringify(state.assemblyOwnerPath))
      || state.busy || state.preview || state.draft || state.draftStarting || state.selectedSourceId
      || state.sourceLoading || state.sourceRequest || state.connection !== "open" || state.model?.source === "local"
      || (cap.can_edit && state.model?.source !== "server")
      || !operations.length || operations.length > cap.max_operations || operations.some(op => !cap.operations.includes(op.op))) return;
    const id = `asm_${nextId()}`;
    set({ assemblyPromptTarget: null, assemblyPromptBlocked: false });
    set({ assemblyRequest: id, busy: true, phase: "Building assembly preview", featurePromptTarget: null,
      newPartName: null, previewInput: null, reviseInput: null });
    socket.send({ type: "design.assembly.command", message_id: id, base_revision: state.sessionRevision,
      ledger_id: cap.ledger_id, ...(cap.version === 2 ? { owner_path: state.assemblyOwnerPath } : {}), scope: "assembly_definition", operations });
  },

  selectSource(id, preserveInput = false) {
    const state = get();
    if (!state.partCapabilities || state.busy || state.preview || state.draft || state.draftStarting
      || state.connection !== "open" || (id && (!state.ledgers[id] || state.ledgers[id].assembly))) return;
    if (state.sourceRequest && id === state.selectedSourceId) return;
    get().invalidateAssemblyPrompt();
    get().clearGeometry();
    set({ selectedSourceId: id, sourceView: null, sourceError: null, assemblySelection: null,
      assemblyOwnerId: state.primaryLedgerId, assemblyOwnerPath: [], assemblyCapabilities: null, assemblyDiscovery: null,
      ...(preserveInput ? {} : { featurePromptTarget: null, newPartName: null }) });
    if (state.sourceRequest) { set({ sourceLoading: true, sourceRequest: { ...state.sourceRequest } }); return; }
    if (!id) {
      set({ sourceLoading: false });
      socket.send({ type: "ledger.request", message_id: nextId() });
      return;
    }
    const messageId = `view_${nextId()}`;
    set({ sourceLoading: true, sourceRequest: { id: messageId, ledgerId: id } });
    socket.send({ type: "design.part.view", message_id: messageId, base_revision: state.sessionRevision, ledger_id: id });
  },

  async loadSourceView(frame) {
    const state = get(), request = state.sourceRequest;
    if (!request || frame.message_id !== request.id || frame.ledger_id !== request.ledgerId
      || frame.session_id !== state.sessionId || frame.revision !== state.sessionRevision || state.connection !== "open") return;
    if (state.selectedSourceId !== frame.ledger_id) {
      set({ sourceRequest: null, sourceLoading: false });
      get().selectSource(state.selectedSourceId, true);
      return;
    }
    const current = () => get().sourceRequest === request && get().sessionId === frame.session_id
      && get().sessionRevision === frame.revision && get().selectedSourceId === frame.ledger_id
      && get().connection === "open" && !get().preview;
    try {
      const read = async (url: string) => {
        const response = await fetch(url);
        if (!response.ok) throw new Error("Source view expired. Select the part again.");
        return response.arrayBuffer();
      };
      if (!frame.empty) {
        if (!frame.glb_url) throw new Error("Source geometry is unavailable.");
        const [glb, step] = await Promise.all([read(frame.glb_url), frame.step_url ? read(frame.step_url) : undefined]);
        if (!current()) return;
        await get().onGeometry({ revision: frame.revision, glb, step, source: "source" });
      } else if (current()) get().clearGeometry();
      if (current()) set({ sourceView: frame, sourceError: null });
    } catch (error) {
      if (current()) set({ sourceError: error instanceof Error ? error.message : "Could not load source." });
    } finally {
      if (get().sourceRequest?.id === request.id) {
        const desired = get().selectedSourceId, replaced = get().sourceRequest !== request;
        set({ sourceRequest: null, sourceLoading: false });
        if (desired !== frame.ledger_id || replaced) get().selectSource(desired, true);
      }
    }
  },

  createPart(name, operations) {
    const state = get();
    if (!state.partCapabilities || state.sourceLoading || state.busy || state.preview || state.draft
      || state.draftStarting || state.connection !== "open" || !name.trim() || !operations.length) return;
    const messageId = `part_${nextId()}`;
    set({ partRequest: messageId, busy: true, phase: "Building new part preview", previewInput: null });
    socket.send({ type: "design.part.create", message_id: messageId, base_revision: state.sessionRevision,
      name: name.trim(), operations });
  },

  startPartPrompt(name) {
    const state = get();
    if (state.sourceLoading || state.busy || state.preview || state.draft || state.draftStarting) return;
    set({ newPartName: name?.trim() || null, featurePromptTarget: null, selections: [], assemblyPromptTarget: null, assemblyPromptBlocked: false });
    window.dispatchEvent(new CustomEvent("cadenza:revise", { detail: "" }));
  },

  submitPartPrompt(text) {
    const state = get(), prompt = text.trim();
    if (!state.partCapabilities || !state.newPartName || !state.agentsAvailable || state.sourceLoading
      || state.busy || state.preview || state.draft || state.draftStarting || state.connection !== "open"
      || !prompt || prompt.length > state.limits.max_prompt_chars) return false;
    const messageId = `part_${nextId()}`;
    set({ partRequest: messageId, busy: true, phase: "Interpreting new part",
      previewInput: { selections: [], attachments: state.attachments, activeLedgerIds: state.activeLedgerIds,
        newPartName: state.newPartName } });
    socket.send({ type: "design.part.prompt", message_id: messageId, base_revision: state.sessionRevision,
      name: state.newPartName, prompt, images: state.attachments.map(({ media_type, data, name }) => ({ media_type, data, name })) });
    get().say({ kind: "user", text: `New part ${state.newPartName}: ${prompt}`, images: state.attachments.map(a => a.previewUrl) });
    return true;
  },

  say: (item) => {
    if (item.kind === "error") {
      socket.sendLog("error", `UI Message: ${item.text}`);
    }
    set((s) => ({ conversation: [...s.conversation, { ...item, id: nextId() }] }));
  },

  async bootKernel() {
    if (get().kernel.state !== "cold") return;
    set({ kernel: { state: "booting" } });
    socket.sendLog("info", "Booting OCCT kernel");
    try {
      const info = await occt.boot();
      set({ kernel: { state: "ready", bootMs: Math.round(info.bootMs) } });
      socket.sendLog("info", "OCCT kernel booted successfully", { bootMs: info.bootMs });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      set({ kernel: { state: "failed", error: message } });
      socket.sendLog("error", "OCCT kernel boot failed", { error: message });
      // Not fatal: the server's spatial probe is authoritative anyway, and the
      // kernel is only a latency cache in front of it (ARCHITECTURE.md §4).
      get().say({
        kind: "status",
        text: `occt-wasm unavailable (${message}). Face picking falls back to the server.`,
      });
    }
  },

  connect() {
    socket.connect(
      {
        onState: (state, detail) => {
          if (state !== "open" && (get().draft || get().draftStarting)) {
            if (get().preview?.draft_id) {
              get().clearGeometry();
              set({ preview: null, previewLoading: false, previewError: null });
            }
            get().say({ kind: "status", text: "Connection changed. The unfinished sketch was discarded; your accepted design is preserved." });
            set({ draft: null, draftProposal: null, draftEntityId: null, draftFocus: null, draftRequest: null, draftStarting: false, draftError: null, busy: false, phase: null });
          }
          if (state !== "open") {
            if (get().assemblyPromptRequest) set({ busy: false, phase: null });
            get().invalidateAssemblyPrompt();
            surfaceRequest++;
            if (get().selectedSourceId) get().clearGeometry();
            set({ assemblyCapabilities: null, assemblyDiscovery: null, assemblyRequest: null, assemblySelection: null,
              assemblyOwnerId: null, assemblyOwnerPath: [], assemblyVersion: 2,
              partCapabilities: null, partDiscovery: null, partRequest: null, newPartName: null,
              selectedSourceId: null, sourceRequest: null, sourceView: null, sourceLoading: false, sourceError: null });
          }
          if (state !== "open") previewRequest++;
          set({ connection: state, connectionDetail: detail ?? "",
            ...(state !== "open" ? { surfaceChecks: {}, surfaceChecking: null, capabilities: null, capabilityRequest: null, patternCapabilities: null, patternDiscovery: null,
              featurePromptAvailable: false, featurePromptDiscovery: null, featurePromptRequest: null, featurePromptTarget: null } : {}) });
        },
        onFrame: (frame) => get().onFrame(frame),
        onGeometry: (payload) => void get().onGeometry(payload),
      },
      { sessionId: get().sessionId },
    );
  },

  onFrame(frame) {
    if (["agent.status", "agent.message", "design.preview", "error", "geometry.failed"].includes(frame.type)) {
      const id = "message_id" in frame ? frame.message_id : null;
      const state = get();
      const assemblyPrompt = state.assemblyPromptRequest;
      if (assemblyPrompt && frame.type !== "error" && id !== assemblyPrompt.id) return;
      if (id?.startsWith("e3cap_") && (id !== state.assemblyPromptDiscovery || state.connection !== "open")) return;
      if (id?.startsWith("e3_") && (!assemblyPrompt || id !== assemblyPrompt.id || state.connection !== "open"
        || assemblyPrompt.session !== state.sessionId || assemblyPrompt.revision !== state.sessionRevision
        || assemblyPrompt.context !== state.assemblyPromptTarget)) return;
      if (assemblyPrompt && id && id !== assemblyPrompt.id && ![state.assemblyPromptDiscovery, state.assemblyDiscovery,
        state.partDiscovery, state.featurePromptDiscovery, state.patternDiscovery, state.capabilityRequest].includes(id)) return;
      if (id?.startsWith("asm_") && (id !== state.assemblyRequest || state.connection !== "open")) return;
      if (id?.startsWith("part_") && (id !== state.partRequest || state.connection !== "open")) return;
      if (id?.startsWith("view_") && (id !== state.sourceRequest?.id || state.connection !== "open")) return;
      if (id?.startsWith("b3_") && (id !== state.featurePromptRequest || state.connection !== "open")) return;
      if (state.assemblyRequest && id && id !== state.assemblyRequest && id !== state.assemblyDiscovery
        && id !== state.partDiscovery && id !== state.featurePromptDiscovery && id !== state.patternDiscovery
        && id !== state.capabilityRequest && id !== state.assemblyPromptDiscovery) return;
      if (state.partRequest && id && id !== state.partRequest && id !== state.partDiscovery
        && id !== state.featurePromptDiscovery && id !== state.patternDiscovery && id !== state.capabilityRequest && id !== state.assemblyDiscovery && id !== state.assemblyPromptDiscovery) return;
      if (state.featurePromptRequest && id && id !== state.featurePromptRequest
        && id !== state.featurePromptDiscovery && id !== state.capabilityRequest && id !== state.patternDiscovery && id !== state.partDiscovery && id !== state.assemblyDiscovery && id !== state.assemblyPromptDiscovery) return;
    }
    switch (frame.type) {
      case "session.ready":
        get().invalidateAssemblyPrompt();
        set({ assemblyCapabilities: null, assemblyDiscovery: null, assemblyRequest: null, assemblySelection: null,
          assemblyOwnerId: null, assemblyOwnerPath: [], assemblyVersion: 2 });
        set({ primaryLedgerId: frame.primary_ledger_id ?? null, partCapabilities: null, partDiscovery: null,
          partRequest: null, newPartName: null, selectedSourceId: null, sourceRequest: null,
          sourceView: null, sourceLoading: false, sourceError: null });
        set({ capabilities: null, capabilityRequest: null, featurePromptAvailable: false, patternCapabilities: null, patternDiscovery: null,
          featurePromptDiscovery: null, featurePromptRequest: null, featurePromptTarget: null });
        if (get().draft) get().say({ kind: "status", text: "Session restored. The unfinished sketch was discarded; accepted geometry is preserved." });
        set({ draft: null, draftProposal: null, draftEntityId: null, draftFocus: null, draftRequest: null, draftStarting: false, draftError: null });
        previewRequest++;
        set({ preview: null, previewLoading: false, previewError: null, previewInput: null, reviseInput: null });
        get().clearGeometry();
        set({
          sessionId: frame.session_id,
          sessionRevision: frame.revision ?? 0,
          canUndo: frame.can_undo ?? false,
          canRedo: frame.can_redo ?? false,
          projectName: frame.project_name ?? "Untitled Part",
          savedRevision: frame.saved_revision ?? null,
          activeLedgerIds: Object.keys(frame.store),
          busy: false,
          ledgers: frame.store,
          // MERGED over the defaults, not substituted for them. A server one
          // deploy behind sends a `limits` object missing the newer keys, and
          // substituting wholesale left `max_images` undefined — which then
          // became `slice(0, NaN)` and silently discarded every upload.
          limits: { ...DEFAULT_LIMITS, ...(frame.limits ?? {}) },
          agentsAvailable: frame.agents_available !== false,
          phase: null,
        });
        {
          const messageId = nextId();
          set({ capabilityRequest: messageId });
          socket.send({ type: "design.capabilities", message_id: messageId });
        }
        get().discoverAssembly();
        if (frame.agents_available === false) {
          get().say({
            kind: "error",
            text: "No AI provider is configured on the server, so prompts will be refused. Set ANTHROPIC_API_KEY in cadenza/.env and restart it.",
          });
        }
        break;

      case "design.capabilities":
        if (get().connection !== "open" || frame.session_id !== get().sessionId
          || frame.message_id !== get().capabilityRequest) break;
        set({ capabilityRequest: null, capabilities: frame.version === 1 && frame.scope === "source"
          && frame.length_unit === "mm" && Array.isArray(frame.features)
          && frame.features.every(c => Array.isArray(c.dimensions) && Array.isArray(c.origin_axes)
            && Array.isArray(c.create_operations) && typeof c.suppress === "boolean" && typeof c.remove === "boolean") ? frame : null });
        if (get().capabilities) {
          const messageId = nextId();
          set({ featurePromptDiscovery: messageId });
          socket.send({ type: "design.prompt.capabilities", message_id: messageId });
          const partId = nextId();
          set({ partDiscovery: partId });
          socket.send({ type: "design.part.capabilities", message_id: partId });
          const patternId = nextId();
          set({ patternDiscovery: patternId });
          socket.send({ type: "design.pattern.capabilities", message_id: patternId });
        }
        break;

      case "design.prompt.capabilities":
        if (get().connection !== "open" || frame.session_id !== get().sessionId
          || frame.message_id !== get().featurePromptDiscovery) break;
        set({ featurePromptDiscovery: null, featurePromptAvailable: frame.version === 1
          && frame.scope === "source" && frame.max_operations === 32 });
        break;

      case "design.pattern.capabilities":
        if (get().connection !== "open" || frame.session_id !== get().sessionId || frame.message_id !== get().patternDiscovery) break;
        set({ patternDiscovery: null, patternCapabilities: frame.version === 1 && frame.scope === "source"
          && frame.radius_mode === "seed_position" && frame.count_min === 2 && frame.count_max === 32 && frame.copy_budget === 128
          && JSON.stringify(frame.operations) === JSON.stringify(["add_linear_pattern", "add_circular_pattern", "update_linear_pattern", "update_circular_pattern"])
          && JSON.stringify(frame.seed_kinds) === JSON.stringify(["box", "cylinder", "hole"])
          && JSON.stringify(frame.directions) === JSON.stringify(["+x", "-x", "+y", "-y", "+z", "-z"]) ? frame : null });
        break;

      case "design.assembly.prompt.capabilities":
        if (get().connection !== "open" || frame.message_id !== get().assemblyPromptDiscovery
          || frame.session_id !== get().sessionId || frame.revision !== get().sessionRevision
          || frame.ledger_id !== get().assemblyOwnerId || get().assemblyCapabilities?.version !== 2
          || JSON.stringify(frame.owner_path) !== JSON.stringify(get().assemblyOwnerPath)) break;
        set({ assemblyPromptDiscovery: null, assemblyPromptCapabilities: frame.version === 1 && frame.max_operations === 16
          && typeof frame.available === "boolean" && typeof frame.vision_available === "boolean"
          && JSON.stringify(frame.target_modes) === JSON.stringify(["definition", "instance", "attachment"]) ? frame : null });
        break;
      case "design.assembly.capabilities":
        if (get().connection !== "open" || frame.session_id !== get().sessionId || frame.revision !== get().sessionRevision
          || frame.ledger_id !== get().assemblyOwnerId || frame.message_id !== get().assemblyDiscovery
          || frame.version !== get().assemblyVersion
          || (frame.version === 2 && JSON.stringify(frame.owner_path) !== JSON.stringify(get().assemblyOwnerPath))) break;
        set({ assemblyDiscovery: null, assemblyCapabilities: frame.max_operations === 16
          && frame.max_components === 32 && frame.max_leaves === 128 && frame.max_depth === 8
          && typeof frame.can_initialize === "boolean" && typeof frame.can_edit === "boolean"
          && JSON.stringify(frame.operations) === JSON.stringify(["initialize_assembly", "add_instance", "duplicate_instance",
            "set_instance_placement", "rename_instance", "remove_instance", ...(frame.version === 2 ? ["add_rigid_joint", "remove_joint", "create_subassembly"] : [])])
          && (frame.version === 1 || (frame.max_subassemblies === 8 && frame.max_initial_children === 16)) ? frame : null });
        if (get().assemblyCapabilities?.version === 2) {
          const id = `e3cap_${nextId()}`; set({ assemblyPromptDiscovery: id, assemblyPromptCapabilities: null });
          socket.send({ type: "design.assembly.prompt.capabilities", message_id: id,
            base_revision: get().sessionRevision, ledger_id: frame.ledger_id, owner_path: get().assemblyOwnerPath });
        }
        break;
      case "design.part.capabilities":
        if (get().connection !== "open" || frame.session_id !== get().sessionId || frame.message_id !== get().partDiscovery) break;
        set({ partDiscovery: null, partCapabilities: frame.version === 1 && frame.source_view === true
          && frame.max_sources === 32 && frame.max_operations === 32
          && JSON.stringify(frame.starter_kinds) === JSON.stringify(["box", "cylinder"]) ? frame : null });
        break;
      case "design.part.view_ready":
        void get().loadSourceView(frame);
        break;
      case "agent.status":
        set({ busy: true, phase: frame.detail ? `${frame.phase}: ${frame.detail}` : frame.phase });
        if (frame.phase === "done") set({ busy: false, phase: null,
          ...(frame.message_id === get().assemblyPromptRequest?.id ? { assemblyPromptRequest: null } : {}),
          ...(frame.message_id === get().partRequest ? { partRequest: null } : {}),
          ...(frame.message_id === get().featurePromptRequest ? { featurePromptRequest: null } : {}) });
        break;

      case "agent.message":
        get().say({
          kind: "assistant",
          text: frame.text,
          assumptions: frame.assumptions?.length ? frame.assumptions : undefined,
        });
        break;

      case "ledger.updated":
        if (frame.revision < get().sessionRevision) break;
        get().invalidateAssemblyPrompt();
        if (frame.revision > get().sessionRevision) set({ busy: false, phase: null });
        set({ assemblyRequest: null, assemblySelection: null });
        set({ featurePromptTarget: null, featurePromptRequest: null, partRequest: null, newPartName: null,
          sourceRequest: null, sourceView: null, sourceLoading: !!(get().selectedSourceId && frame.store[get().selectedSourceId!]), sourceError: null,
          selectedSourceId: get().selectedSourceId && frame.store[get().selectedSourceId!] ? get().selectedSourceId : null });
        previewRequest++;
        set((s) => ({
          draft: null, draftProposal: null, draftEntityId: null, draftFocus: null, draftRequest: null, draftStarting: false, draftError: null,
          preview: null, previewLoading: false, previewError: null, previewInput: null, reviseInput: null,
          ledgers: frame.store,
          sessionRevision: frame.revision,
          canUndo: frame.can_undo,
          canRedo: frame.can_redo ?? false,
          geometryGeneration: s.geometryGeneration + 1,
          selections: [],
          surfaceChecks: {}, surfaceChecking: null,
          verify: null,
          activeLedgerIds: s.activeLedgerIds.filter(id => id in frame.store),
        }));
        get().discoverAssembly();
        window.dispatchEvent(new Event("cadenza:invalidate"));
        if (get().activeLedgerIds.length === 0) {
          set({ activeLedgerIds: Object.keys(frame.store) });
        }
        break;

      case "design.preview":
        void get().loadPreview(frame);
        break;

      case "sketch.proposal": {
        const state = get(), draft = state.draft;
        if (!draft || state.connection !== "open" || state.preview || frame.session_id !== state.sessionId
          || frame.message_id !== state.draftRequest || frame.base_revision !== state.sessionRevision
          || frame.draft_id !== draft.id || frame.draft_revision !== draft.revision
          || frame.selected_entity_id !== state.draftEntityId) break;
        set({ draftProposal: frame, draftRequest: null, busy: false, phase: null, draftError: null });
        break;
      }
      case "sketch.draft_state": {
        const state = get();
        if (state.connection !== "open" || frame.session_id !== state.sessionId || frame.draft.base_revision !== state.sessionRevision) break;
        const starting = state.draftStarting && frame.message_id === state.draftRequest;
        const updating = state.draft?.id === frame.draft.id && frame.draft.revision >= state.draft.revision
          && (!state.draftRequest || frame.message_id === state.draftRequest);
        if (!starting && !updating) break;

        const entities = frame.draft.definition.entities;
        const added = state.draft && frame.draft.revision > state.draft.revision
          ? entities.find(e => !state.draft!.definition.entities.some(old => old.id === e.id)) : undefined;
        const selected = added?.id ?? entities.find(e => e.id === state.draftEntityId)?.id ?? entities[0]?.id ?? null;
        set({ draft: frame.draft, draftProposal: null, draftEntityId: selected, draftFocus: null,
          draftRequest: null, draftStarting: false, busy: false, phase: null });
        break;
      }
      case "sketch.draft_closed":
        if (frame.session_id !== get().sessionId || frame.draft_id !== get().draft?.id
          || frame.message_id !== get().draftRequest) break;
        previewRequest++;
        get().clearGeometry();
        set({ draft: null, draftProposal: null, draftEntityId: null, draftFocus: null, draftRequest: null, draftStarting: false, draftError: null,
          preview: null, previewLoading: false, previewError: null, busy: false, phase: null });
        break;

      case "design.preview_closed":
        if (get().assemblyPromptRequest) break;
        if (get().preview?.preview_id === frame.preview_id) {
          previewRequest++;
          get().clearGeometry();
          set({ preview: null, previewLoading: false, previewError: null });
        }
        set({ busy: false, phase: null, partRequest: null, assemblyRequest: null });
        if (get().selectedSourceId) get().selectSource(get().selectedSourceId, true);
        break;

      case "geometry.ready":
        if (get().assemblyPromptRequest) break;
        if (frame.revision !== get().sessionRevision) break;
        if (get().selectedSourceId && !get().preview) {
          if (!get().busy && !get().sourceRequest && !get().sourceView) get().selectSource(get().selectedSourceId, true);
          break;
        }
        if (frame.empty || frame.stub) get().clearGeometry();
        if (frame.empty) set({ busy: false, phase: null });
        if (frame.warnings?.length) {
          for (const warning of frame.warnings) get().say({ kind: "status", text: warning });
        }
        if (frame.stub) {
          get().say({
            kind: "status",
            text: "The server built this without a geometry kernel, so there is no mesh to show.",
          });
          set({ busy: false, phase: null });
        }
        break;

      case "geometry.failed":
        // The ledger was rolled back; the viewport keeps showing what it has.
        set({ busy: false, phase: null });
        get().say({ kind: "error", text: `${frame.code}: ${frame.message}` });
        break;

      case "error":
        {
          const state = get();
          if (state.sourceRequest && frame.message_id === state.sourceRequest.id) {
            const desired = state.selectedSourceId;
            set({ sourceRequest: null, sourceLoading: false, sourceError: frame.message });
            if (desired && desired !== state.sourceRequest.ledgerId) get().selectSource(desired, true);
            break;
          }
          const discoveries = [
            { id: state.assemblyPromptDiscovery, type: "design.assembly.prompt.capabilities", reset: { assemblyPromptDiscovery: null, assemblyPromptCapabilities: null } },
            { id: state.assemblyDiscovery, type: "design.assembly.capabilities", reset: { assemblyDiscovery: null, assemblyCapabilities: null } },
            { id: state.partDiscovery, type: "design.part.capabilities", reset: { partDiscovery: null, partCapabilities: null } },
            { id: state.featurePromptDiscovery, type: "design.prompt.capabilities", reset: { featurePromptDiscovery: null, featurePromptAvailable: false } },
            { id: state.patternDiscovery, type: "design.pattern.capabilities", reset: { patternDiscovery: null, patternCapabilities: null } },
            { id: state.capabilityRequest, type: "design.capabilities", reset: { capabilityRequest: null, capabilities: null } },
          ].filter(d => d.id);
          const identified = discoveries.filter(d => frame.message_id === d.id);
          // Pydantic lists all EXPECTED tags after the rejected input tag.
          // Matching those names would disable unrelated optional capabilities.
          const rejectedType = frame.detail?.match(/(?:Input tag|Unknown(?: input tag)?)\s+['"]?([a-z][a-z0-9_.]+)/i)?.[1]
            ?? (/^design\.assembly\.capabilities\.(?:version|ledger_id|owner_path)\s*$/m.test(frame.detail ?? "") ? "design.assembly.capabilities" : undefined);
          const exact = !frame.message_id && frame.code === "BAD_MESSAGE"
            ? discoveries.filter(d => d.type === rejectedType) : [];
          if (rejectedType === "design.assembly.prompt.capabilities" && !state.assemblyPromptDiscovery) break;
          // An uncorrelated discovery rejection never owns a mutation's busy flag.
          const rejected = identified.length ? identified : exact.length ? exact
            : !frame.message_id && !rejectedType && frame.code === "BAD_MESSAGE" && (!state.busy || /capabilit/i.test(frame.detail ?? "")) ? discoveries : [];
          if (rejected.length) {
            rejected.forEach(d => set(d.reset));
            if ((identified.length || exact.length) && rejected.some(d => d.type === "design.assembly.capabilities")
              && state.assemblyVersion === 2 && state.assemblyOwnerPath.length === 0 && frame.code === "BAD_MESSAGE") {
              set({ assemblyVersion: 1 }); get().discoverAssembly();
            }
            break;
          }
        }
        if (frame.message_id === get().assemblyRequest) set({ assemblyRequest: null });
        if (frame.message_id === get().assemblyPromptRequest?.id) set({ assemblyPromptRequest: null });
        if (frame.message_id === get().partRequest) set({ partRequest: null });
        if (frame.message_id === get().featurePromptRequest) set({ featurePromptRequest: null });
        // Older servers reject discovery before parsing a request ID. This query
        // never owns the busy flag, including when a legacy edit is in flight.
        if (get().draftRequest && frame.message_id && frame.message_id !== get().draftRequest) break;
        set({ busy: false, phase: null });
        if (frame.message_id === get().draftRequest || (!frame.message_id && get().draftRequest && frame.code === "BAD_MESSAGE")) {
          set({ draftError: frame.message, draftProposal: null, draftStarting: false });
          if (!get().draft) {
            set({ draftRequest: null });
            if (frame.code === "REVISION_CONFLICT") socket.send({ type: "ledger.request", message_id: nextId() });
          }
        } else if (frame.code === "REVISION_CONFLICT") {
          socket.send({ type: "ledger.request", message_id: nextId() });
        }
        get().say({
          kind: "error",
          text: frame.detail ? `${frame.code}: ${frame.message} (${frame.detail})` : `${frame.code}: ${frame.message}`,
        });
        socket.sendLog("error", "Server request failed", { code: frame.code, revision: get().sessionRevision }, frame.message_id ?? undefined);
        break;

      default:
        break;
    }
  },

  clearGeometry() {
    set(s => ({ model: null, geometryGeneration: s.geometryGeneration + 1,
      selections: [], promptOpen: false, verify: null, surfaceChecks: {}, surfaceChecking: null }));
    window.dispatchEvent(new Event("cadenza:clear"));
    workerQueue = workerQueue.catch(() => {}).then(() => occt.clearModel());
  },

  async onGeometry({ revision, glb, step, source = "server" }) {
    if (source === "server" && get().assemblyPromptRequest) return;
    if (source === "server" && get().selectedSourceId) return;
    if (source === "local" && (get().preview || get().draft || get().draftStarting)) return;
    if (source === "local") get().invalidateAssemblyPrompt();
    if (source === "local") set({ assemblySelection: null, assemblyOwnerId: null, assemblyOwnerPath: [], assemblyCapabilities: null, assemblyDiscovery: null, featurePromptTarget: null, featurePromptRequest: null, selectedSourceId: null, sourceRequest: null, sourceView: null, sourceLoading: false, newPartName: null });
    if (source === "server" && revision !== get().sessionRevision) return;
    const generation = get().geometryGeneration + 1;
    // The viewport subscribes to this event rather than the store reading into
    // the scene — see components/Viewport.tsx.
    if (source === "server") socket.setShownRevision(revision);
    const revised = source === "server" || source === "source" ? get().reviseInput : null;

    set({
      geometryGeneration: generation,
      busy: false,
      phase: null,
      model: {
        revision,
        source,
        glbBytes: glb.byteLength,
        stepBytes: step?.byteLength ?? 0,
      },
      // The old selections point at faces of a model that no longer exists.
      selections: revised ? revised.selections.map(selection => ({ ...selection, faceHash: -1 })) : [],
      ...(revised ? { activeLedgerIds: revised.activeLedgerIds, reviseInput: null, newPartName: revised.newPartName ?? null,
        assemblyPromptTarget: revised.assemblyPromptTarget && get().primaryLedgerId
          && validAssemblyPrompt(get().ledgers, get().primaryLedgerId!, revised.assemblyPromptTarget)
          ? revised.assemblyPromptTarget : null,
        featurePromptTarget: revised.featurePromptTarget && get().ledgers[revised.featurePromptTarget.ledgerId]
          && (!revised.featurePromptTarget.featureId || get().ledgers[revised.featurePromptTarget.ledgerId].features
            .some(feature => feature.id === revised.featurePromptTarget!.featureId))
          ? revised.featurePromptTarget : null } : {}),
      surfaceChecks: {}, surfaceChecking: null,
      promptOpen: false,
      verify: null,
    });
    window.dispatchEvent(new CustomEvent("cadenza:glb", { detail: { glb, revision, generation } }));

    if (step && get().kernel.state === "ready") {
      await get().loadStep(step, generation);
    } else {
      workerQueue = workerQueue.catch(() => {}).then(() => occt.clearModel());
    }
  },

  async loadStep(step, generation = get().geometryGeneration) {
    const current = () => get().geometryGeneration === generation;
    workerQueue = workerQueue.catch(() => {}).then(async () => {
      if (!current()) return;
      try {
        const summary = await occt.loadStep(step);
        if (!current()) return;
        const t0 = performance.now();
        const mesh = await occt.getPickMesh();
        const wireframe = await occt.getWireframe(0.05);
        if (!current()) return;
        const pickMs = performance.now() - t0;
        set((s) => ({ model: s.model ? { ...s.model, summary, pickMs } : s.model }));
        window.dispatchEvent(new CustomEvent("cadenza:pickmesh", { detail: { mesh, wireframe, generation } }));
      } catch (err) {
        if (!current()) return;
        get().say({
          kind: "status",
          text: `STEP load failed: ${err instanceof Error ? err.message : String(err)}`,
        });
      }
    });
    await workerQueue;
  },

  select: (sel) => {
    if (get().selectedSourceId && sel) return;
    if (get().preview || get().draft || get().draftStarting) return;
    const arr = sel === null ? [] : Array.isArray(sel) ? sel : [sel];
    socket.sendLog("info", "User selection changed", { count: arr.length, modes: arr.map(a => a.mode) });
    set({ selections: arr, assemblySelection: arr.length === 1 ? arr[0].component_id ?? null : null, promptOpen: arr.length > 0, verify: null });
  },

  setFaceFacts: (facts) =>
    set((s) => {
      const idx = s.selections.findIndex((sel) => sel.faceHash === facts.faceHash);
      if (idx === -1) return {};
      const next = [...s.selections];
      next[idx] = { ...next[idx], info: facts };
      return { selections: next };
    }),

  openPrompt: (open) => set({ promptOpen: open }),

  async loadPreview(preview) {
    const start = get();
    if (preview.base_revision !== start.sessionRevision || start.connection !== "open") return;
    if (preview.draft_id && (start.draft?.id !== preview.draft_id || start.draft.revision !== preview.draft_revision
      || start.draftRequest !== preview.message_id)) return;
    const request = ++previewRequest;
    const current = () => request === previewRequest && get().sessionId === start.sessionId
      && get().sessionRevision === preview.base_revision && get().preview?.preview_id === preview.preview_id
      && get().connection === "open";
    set({ preview, previewLoading: true, previewError: null, selections: [], surfaceChecks: {}, surfaceChecking: null });
    try {
      const sourceDisplay = !!preview.display_ledger_id && !!get().partCapabilities;
      const glbUrl = sourceDisplay ? preview.source_glb_url : preview.glb_url;
      const stepUrl = sourceDisplay ? preview.source_step_url : preview.step_url;
      if (sourceDisplay ? preview.source_empty : preview.empty) {
        get().clearGeometry();
      } else {
        if (!glbUrl) throw new Error("Preview geometry is unavailable.");
        const read = async (url: string) => {
          const response = await fetch(url);
          if (!response.ok) throw new Error("Preview expired or could not be loaded. Cancel and try again.");
          return response.arrayBuffer();
        };
        const [glb, step] = await Promise.all([read(glbUrl), stepUrl ? read(stepUrl) : undefined]);
        if (!current()) return;
        await get().onGeometry({ revision: preview.base_revision, glb, step, source: "preview" });
      }
      if (current()) set({ previewLoading: false, busy: false, phase: null });
    } catch (cause) {
      if (current()) set({ previewLoading: false, busy: false,
        previewError: cause instanceof Error ? cause.message : "Could not load preview." });
    }
  },

  decidePreview(action, revise = false) {
    const state = get();
    const preview = state.preview;
    if (!preview || state.busy || state.connection !== "open") return;
    if (action === "apply" && (state.previewLoading || state.previewError)) return;
    previewRequest++;
    set({ partRequest: null, assemblyRequest: null, assemblyPromptRequest: null });
    if (action === "apply" && preview.display_ledger_id && state.partCapabilities) {
      set({ selectedSourceId: preview.display_ledger_id, sourceView: null });
    }
    const messageId = nextId();
    if (preview.draft_id) set({ draftRequest: messageId });
    socket.send({ type: "design.preview_decision", message_id: messageId, base_revision: state.sessionRevision,
      preview_id: preview.preview_id, action });
    set({ busy: true, phase: action === "apply" ? "Applying preview" : "Discarding preview" });
    if (revise) {
      set({ newPartName: state.previewInput?.newPartName ?? null, reviseInput: state.previewInput, attachments: state.previewInput?.attachments ?? state.attachments });
      window.dispatchEvent(new CustomEvent("cadenza:revise", { detail: preview.prompt }));
    }
  },


  selectDraftEntity(id, field) {
    if (!get().draft?.definition.entities.some(e => e.id === id) || get().preview || get().busy) return;
    set({ ...(id !== get().draftEntityId ? { draftProposal: null } : {}), draftEntityId: id, draftFocus: field ? { field, request: ++seq } : null });
  },

  promptSketch(text, reply = false) {
    const state = get(), draft = state.draft, proposal = state.draftProposal;
    const prompt = text.trim();
    if (!draft || state.busy || state.preview || !state.agentsAvailable || state.connection !== "open"
      || !prompt || prompt.length > state.limits.max_prompt_chars || (reply && !proposal?.question)) return;
    const messageId = nextId();
    set({ busy: true, phase: "Interpreting sketch request", draftRequest: messageId, draftError: null, draftProposal: null });
    socket.send({ type: "sketch.prompt", message_id: messageId, base_revision: draft.base_revision,
      draft_id: draft.id, draft_revision: draft.revision, selected_entity_id: state.draftEntityId,
      prompt, reply_to: reply ? proposal!.proposal_id : null });
  },

  decideSketchProposal(action) {
    const state = get(), draft = state.draft, proposal = state.draftProposal;
    if (!draft || !proposal || state.busy || state.preview || state.connection !== "open"
      || proposal.draft_id !== draft.id || proposal.draft_revision !== draft.revision
      || proposal.selected_entity_id !== state.draftEntityId || (action === "update" && proposal.question)) return;
    const messageId = nextId();
    set({ busy: true, phase: action === "update" ? "Updating sketch" : "Canceling sketch proposal", draftRequest: messageId, draftError: null });
    socket.send({ type: "sketch.proposal_decision", message_id: messageId, base_revision: draft.base_revision,
      draft_id: draft.id, draft_revision: draft.revision, selected_entity_id: state.draftEntityId,
      proposal_id: proposal.proposal_id, action });
  },

  startDraft(profileId) {
    const state = get();
    if (state.selectedSourceId || state.sourceLoading) return;
    if (state.sourceLoading || state.busy || state.draft || state.preview || state.connection !== "open" || state.model?.source === "local") return;
    const messageId = nextId();
    set({ busy: true, phase: "Starting sketch draft", draftRequest: messageId, draftStarting: true, draftError: null });
    socket.send({ type: "sketch.draft_start", message_id: messageId, base_revision: state.sessionRevision, profile_id: profileId ?? null });
  },

  updateDraft(operations) {
    const state = get(), draft = state.draft;
    if (!draft || state.busy || state.preview || state.connection !== "open") return;
    const messageId = nextId();
    set({ draftProposal: null, busy: true, phase: "Solving sketch draft", draftRequest: messageId, draftError: null });
    socket.send({ type: "sketch.draft_update", message_id: messageId, base_revision: draft.base_revision,
      draft_id: draft.id, draft_revision: draft.revision, operations });
  },

  actOnDraft(action) {
    const state = get(), draft = state.draft;
    if (!draft || state.busy || state.connection !== "open" || (action === "finish" && state.preview)) return;
    const messageId = nextId();
    set({ draftProposal: null, busy: true, phase: action === "finish" ? "Building sketch review" : "Discarding sketch draft",
      draftRequest: messageId, draftError: null });
    socket.send({ type: "sketch.draft_action", message_id: messageId, base_revision: draft.base_revision,
      draft_id: draft.id, draft_revision: draft.revision, action });
  },

  previewFeatureCommand(ledgerId, operations) {
    const state = get(), ledger = state.ledgers[ledgerId];
    if (!ledger || !state.capabilities || state.busy || state.draft || state.draftStarting || state.preview
      || state.connection !== "open" || state.model?.source === "local" || !operations.length || operations.length > 32) return;
    if (!operations.every(op => commandAvailable(ledger, state.capabilities!, op, state.patternCapabilities))) return;
    set({ busy: true, phase: "Building feature preview", previewInput: null, reviseInput: null });
    socket.send({ type: "design.command", message_id: nextId(), base_revision: state.sessionRevision,
      ledger_id: ledgerId, scope: "source", operations });
  },

  editParameter(ledgerId, featureId, parameter, value) {
    const state = get();
    if (state.sourceLoading || state.busy || state.draft || state.preview || state.connection !== "open" || state.model?.source === "local") return;
    if (!Number.isFinite(value) || value <= 0) return;
    socket.send({ type: "design.parameter", message_id: nextId(), base_revision: state.sessionRevision,
      ledger_id: ledgerId, feature_id: featureId, parameter, value });
    set({ busy: true, phase: "Building dimension preview", previewInput: null, reviseInput: null });
  },

  editNamedParameter(ledgerId, parameter, value) {
    const state = get();
    if (state.sourceLoading || state.busy || state.draft || state.preview || state.connection !== "open" || state.model?.source === "local") return;
    const driver = state.ledgers[ledgerId]?.relationships?.parameters[parameter];
    if (!driver || driver.expression !== null || !Number.isFinite(value) || Math.abs(value) > 1e9) return;
    socket.send({ type: "design.named_parameter", message_id: nextId(), base_revision: state.sessionRevision,
      ledger_id: ledgerId, parameter, value });
    set({ busy: true, phase: "Building parameter preview", previewInput: null, reviseInput: null });
  },

  editSketchDimension(ledgerId, featureId, constraintId, value) {
    const state = get();
    if (state.sourceLoading || state.busy || state.draft || state.preview || state.connection !== "open" || state.model?.source === "local") return;
    if (!Number.isFinite(value) || Math.abs(value) > 1e6) return;
    socket.send({ type: "design.sketch_dimension", message_id: nextId(), base_revision: state.sessionRevision,
      ledger_id: ledgerId, feature_id: featureId, constraint_id: constraintId, value });
    set({ busy: true, phase: "Solving sketch and building preview", previewInput: null, reviseInput: null });
  },

  async checkSurface(ledgerId, referenceId, select = false) {
    const start = get();
    if (start.preview || start.selectedSourceId) return;
    if (!start.sessionId || start.connection !== "open" || start.busy || start.surfaceChecking || start.model?.source === "local") return;
    if (select && start.model?.revision !== start.sessionRevision) return;
    const reference = start.ledgers[ledgerId]?.surface_references?.[referenceId];
    if (!reference) return;
    const key = `${ledgerId}:${referenceId}`;
    const request = ++surfaceRequest;
    const current = () => request === surfaceRequest && get().sessionId === start.sessionId && get().sessionRevision === start.sessionRevision
      && get().geometryGeneration === start.geometryGeneration && get().connection === "open"
      && !get().preview && get().model?.source !== "local" && get().ledgers[ledgerId]?.surface_references?.[referenceId] === reference;
    set({ surfaceChecking: key });
    try {
      const response = await fetch(`/api/sessions/${encodeURIComponent(start.sessionId)}/surface-references/resolve`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ledger_id: ledgerId, reference_id: referenceId, base_revision: start.sessionRevision }),
      });
      const data = await response.json();
      if (!current()) return;
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not check this surface.");
      if (data.session_id !== start.sessionId || data.revision !== start.sessionRevision) return;
      const result = data.result as SurfaceCheck;
      set(s => ({ surfaceChecks: { ...s.surfaceChecks, [key]: result } }));
      if (select && result.status === "resolved" && result.face && !get().busy && get().selections === start.selections) {
        get().select({ mode: "face", faceHash: -1, point: result.face.point, normal: result.face.normal,
          screen: { x: 0, y: 0 }, label: referenceId.replace(/^ref_/, "").replaceAll("_", " "),
          feature_id: reference.feature_id, component_id: reference.component_path.join("/") || undefined,
          saved_reference: { ledger_id: ledgerId, reference_id: referenceId } });
        set({ activeLedgerIds: [...new Set([ledgerId, reference.source_ledger_id])] });
      }
    } catch (cause) {
      if (current()) set(s => ({ surfaceChecks: { ...s.surfaceChecks,
        [key]: { status: "error", message: cause instanceof Error ? cause.message : "Could not check this surface." } } }));
    } finally {
      if (request === surfaceRequest) set({ surfaceChecking: null });
    }
  },

  startFeaturePrompt(ledgerId, featureId) {
    const state = get();
    if (state.sourceLoading || state.busy || state.preview || state.draft || state.draftStarting) return;
    if (ledgerId === null) { set({ featurePromptTarget: null }); return; }
    const ledger = state.ledgers[ledgerId], feature = ledger?.features.find(f => f.id === featureId);
    if (!state.featurePromptAvailable || !state.capabilities || !state.agentsAvailable || state.connection !== "open"
      || state.model?.source === "local" || !ledger || ledger.assembly
      || (featureId && (!feature || (isPattern(feature) ? !state.patternCapabilities || !!patternReason(feature, ledger)
        : !simpleFeature(feature) || !state.capabilities.features.some(c => c.kind === feature.kind))))) return;
    set({ newPartName: null, featurePromptTarget: { ledgerId, ...(featureId ? { featureId } : {}) }, selections: [],
      assemblyPromptTarget: null, assemblyPromptBlocked: false });
    window.dispatchEvent(new CustomEvent("cadenza:revise", { detail: "" }));
  },

  submitFeaturePrompt(text) {
    const state = get(), target = state.featurePromptTarget, prompt = text.trim();
    if (state.sourceLoading || !target || !state.featurePromptAvailable || !state.capabilities || !state.agentsAvailable
      || state.connection !== "open" || state.busy || state.preview || state.draft || state.draftStarting
      || state.model?.source === "local" || !prompt || prompt.length > state.limits.max_prompt_chars) return false;
    const ledger = state.ledgers[target.ledgerId], feature = ledger?.features.find(f => f.id === target.featureId);
    if (!ledger || ledger.assembly || (target.featureId && (!feature || (isPattern(feature) ? !state.patternCapabilities || !!patternReason(feature, ledger)
      : !simpleFeature(feature) || !state.capabilities.features.some(c => c.kind === feature.kind))))) return false;
    const messageId = `b3_${nextId()}`;
    set({ busy: true, phase: "sent", featurePromptRequest: messageId,
      previewInput: { selections: [], attachments: state.attachments, activeLedgerIds: state.activeLedgerIds,
        featurePromptTarget: target } });
    socket.send({ type: "design.prompt", message_id: messageId, base_revision: state.sessionRevision,
      ledger_id: target.ledgerId, feature_id: target.featureId ?? null, scope: "source", prompt,
      images: state.attachments.map(({ media_type, data, name }) => ({ media_type, data, name })) });
    get().say({ kind: "user", text: prompt, images: state.attachments.map(a => a.previewUrl) });
    return true;
  },

  /** Point & Speak: an edit anchored to the faces the user clicked. */
  submitPrompt(text) {
    if (get().selectedSourceId || get().sourceLoading) return;
    if (get().preview || get().draft || get().draftStarting) return;
    if (get().busy || get().model?.source === "local" || get().model?.revision !== get().sessionRevision) return;
    const sel = get().selections;
    if (sel.length === 0 || text.trim() === "") return;
    const invalid = sel.find(s => s.selection_error);
    if (invalid) { get().say({ kind: "error", text: invalid.selection_error! }); return; }

    socket.sendLog("info", "User submitted point-and-speak prompt", { text: text.trim(), targets: sel.length });

    socket.sendPrompt({
      prompt: text.trim(),
      targets: sel.map(s => ({ point: s.point, normal: s.normal, ...(s.saved_reference ? { saved_reference: s.saved_reference } : {}),
        ...(s.surface_reference ? { surface_reference: s.surface_reference } : {}) })),
      baseRevision: get().sessionRevision,
      activeLedgerIds: get().activeLedgerIds,
    });

    get().say({ kind: "user", text: text.trim(), point: sel[0].point });
    set({ promptOpen: false, busy: true, phase: "sent",
      previewInput: { selections: sel, attachments: [], activeLedgerIds: get().activeLedgerIds } });
  },

  /** The composer: a prompt with no click, optionally carrying sketches. */
  submitComposer(text) {
    if (get().assemblyPromptTarget) { get().submitAssemblyPrompt(text); return; }
    if (get().assemblyPromptBlocked) {
      get().say({ kind: "status", text: "The assembly target changed. Select an AI target again or clear assembly mode." }); return;
    }
    if (get().newPartName) { get().submitPartPrompt(text); return; }
    if (get().sourceLoading) return;
    if (get().selectedSourceId && !get().featurePromptTarget) {
      get().say({ kind: "status", text: "Choose Add with AI or select a feature to edit in this part." }); return;
    }
    if (get().featurePromptTarget) { get().submitFeaturePrompt(text); return; }
    if (get().preview || get().draft || get().draftStarting) return;
    if (get().busy) return;
    if (get().model?.source === "local") {
      get().say({ kind: "status", text: "This is a local preview. Reconnect to restore your editable server design, or start a new design." });
      return;
    }
    const { attachments, selections } = get();
    const invalid = selections.find(s => s.selection_error);
    if (invalid) { get().say({ kind: "error", text: invalid.selection_error! }); return; }
    const prompt = text.trim();
    // An image on its own is a complete instruction; text alone is too. Only
    // the empty case is refused.
    if (prompt === "" && attachments.length === 0) return;

    socket.sendLog("info", "User submitted composer prompt", { text: prompt, images: attachments.length, targets: selections.length });

    socket.sendPrompt({
      prompt: prompt || "Build the part shown in the attached drawing.",
      images: attachments.map(({ media_type, data, name }) => ({ media_type, data, name })),
      baseRevision: get().sessionRevision,
      ...(selections.length > 0 ? { targets: selections.map(s => ({ point: s.point, normal: s.normal,
        ...(s.saved_reference ? { saved_reference: s.saved_reference } : {}),
        ...(s.surface_reference ? { surface_reference: s.surface_reference } : {}) })) } : {}),
      activeLedgerIds: get().activeLedgerIds,
    });

    get().say({
      kind: "user",
      text: prompt || `(${attachments.length} sketch${attachments.length > 1 ? "es" : ""})`,
      images: attachments.map((a) => a.previewUrl),
      ...(selections.length > 0 ? { point: selections[0].point } : {}),
    });
    set({ busy: true, phase: "sent", attachments: [], selections: [], promptOpen: false,
      previewInput: { selections, attachments, activeLedgerIds: get().activeLedgerIds } });
  },

  async addFiles(files) {
    if (files.length === 0) return;
    const { limits, attachments } = get();

    // Never trust these to be numbers. They arrive from the server, and a
    // non-number here used to poison `slice` into returning nothing at all —
    // an upload that vanished with no thumbnail and no error.
    const maxImages =
      Number.isFinite(limits.max_images) && limits.max_images > 0
        ? limits.max_images
        : DEFAULT_LIMITS.max_images;
    const maxBytes =
      Number.isFinite(limits.max_image_bytes) && limits.max_image_bytes > 0
        ? limits.max_image_bytes
        : DEFAULT_LIMITS.max_image_bytes;

    const room = maxImages - attachments.length;
    if (room <= 0) {
      get().say({ kind: "error", text: `At most ${maxImages} images per prompt.` });
      return;
    }
    if (files.length > room) {
      get().say({
        kind: "status",
        text: `Taking the first ${room} of ${files.length} images (limit ${maxImages}).`,
      });
    }

    set({ attaching: true });
    const prepared: PreparedImage[] = [];
    for (const file of files.slice(0, room)) {
      try {
        prepared.push(await prepareImage(file, maxBytes));
      } catch (err) {
        get().say({
          kind: "error",
          text: err instanceof Error ? err.message : String(err),
        });
      }
    }
    // Silence is the one outcome that must not happen: if every file fell out,
    // say so rather than leaving the user staring at an unchanged composer.
    if (prepared.length === 0) {
      get().say({
        kind: "error",
        text: `Could not read ${files.length === 1 ? "that image" : "any of those images"}.`,
      });
    }
    set((s) => ({ attachments: [...s.attachments, ...prepared], attaching: false }));
  },

  removeAttachment: (index) =>
    set((s) => {
      const next = [...s.attachments];
      const [gone] = next.splice(index, 1);
      if (gone) URL.revokeObjectURL(gone.previewUrl);
      return { attachments: next };
    }),

  clearAttachments: () =>
    set((s) => {
      for (const a of s.attachments) URL.revokeObjectURL(a.previewUrl);
      return { attachments: [] };
    }),

  resetSession() {
    if (get().sourceLoading || get().busy || get().draft) return;
    get().clearAttachments();
    set({ conversation: [], busy: true, phase: "resetting" });
    socket.reset();
  },

  undoSession(revision) {
    if (get().sourceLoading || get().busy || get().draft || get().preview || !get().canUndo) return;
    socket.undo(revision, get().sessionRevision);
    set({ busy: true, phase: "undoing" });
  },

  redoSession() {
    if (get().sourceLoading || get().busy || get().draft || get().preview || !get().canRedo) return;
    socket.send({ type: "session.redo", message_id: crypto.randomUUID(), base_revision: get().sessionRevision });
    set({ busy: true, phase: "redoing" });
  },

  setVerify: (v) => set({ verify: v }),

  toggleGrid: () => set((s) => ({ showGrid: !s.showGrid })),
  toggleEdges: () => set((s) => ({ showEdges: !s.showEdges })),
}));
