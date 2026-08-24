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
import type { FaceSelection, Ledger, Limits, ServerFrame, Vec3 } from "./types";

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
  glbBytes: number;
  stepBytes: number;
  parseMs?: number;
  triangleCount?: number;
  summary?: ModelSummary;
  pickMs?: number;
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
const nextId = (): string => `i${++seq}`;

interface State {
  kernel: KernelStatus;
  connection: ConnectionState;
  connectionDetail: string;
  sessionId: string | null;
  limits: Limits;
  agentsAvailable: boolean;
  ledger: Ledger | null;
  model: ModelStatus | null;
  /** Rises while a turn is in flight. */
  busy: boolean;
  phase: string | null;
  conversation: ConvItem[];
  selection: FaceSelection | null;
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
  select: (sel: FaceSelection | null) => void;
  setFaceFacts: (facts: FaceFacts) => void;
  openPrompt: (open: boolean) => void;
  submitPrompt: (text: string) => void;
  submitComposer: (text: string) => void;
  addFiles: (files: File[]) => Promise<void>;
  removeAttachment: (index: number) => void;
  clearAttachments: () => void;
  resetSession: () => void;
  setVerify: (v: State["verify"]) => void;
  toggleGrid: () => void;
  toggleEdges: () => void;
  say: (item: Omit<ConvItem, "id">) => void;
}

export const useStore = create<State>((set, get) => ({
  kernel: { state: "cold" },
  connection: "idle",
  connectionDetail: "",
  sessionId: null,
  limits: DEFAULT_LIMITS,
  agentsAvailable: true,
  ledger: null,
  model: null,
  busy: false,
  phase: null,
  conversation: [],
  selection: null,
  promptOpen: false,
  showGrid: true,
  showEdges: true,
  verify: null,
  attachments: [],
  attaching: false,

  say: (item) =>
    set((s) => ({ conversation: [...s.conversation, { ...item, id: nextId() }] })),

  async bootKernel() {
    if (get().kernel.state !== "cold") return;
    set({ kernel: { state: "booting" } });
    try {
      const info = await occt.boot();
      set({ kernel: { state: "ready", bootMs: Math.round(info.bootMs) } });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      set({ kernel: { state: "failed", error: message } });
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
        onState: (state, detail) => set({ connection: state, connectionDetail: detail ?? "" }),
        onFrame: (frame) => get().onFrame(frame),
        onGeometry: (payload) => void get().onGeometry(payload),
      },
      { sessionId: get().sessionId },
    );
  },

  onFrame(frame) {
    switch (frame.type) {
      case "session.ready":
        set({
          sessionId: frame.session_id,
          ledger: frame.ledger,
          // MERGED over the defaults, not substituted for them. A server one
          // deploy behind sends a `limits` object missing the newer keys, and
          // substituting wholesale left `max_images` undefined — which then
          // became `slice(0, NaN)` and silently discarded every upload.
          limits: { ...DEFAULT_LIMITS, ...(frame.limits ?? {}) },
          agentsAvailable: frame.agents_available !== false,
          phase: null,
        });
        if (frame.agents_available === false) {
          get().say({
            kind: "error",
            text: "No AI provider is configured on the server, so prompts will be refused. Set ANTHROPIC_API_KEY in cadenza/.env and restart it.",
          });
        }
        break;

      case "agent.status":
        set({ busy: true, phase: frame.detail ? `${frame.phase}: ${frame.detail}` : frame.phase });
        if (frame.phase === "done") set({ busy: false, phase: null });
        break;

      case "agent.message":
        get().say({
          kind: "assistant",
          text: frame.text,
          assumptions: frame.assumptions?.length ? frame.assumptions : undefined,
        });
        break;

      case "ledger.updated":
        set({ ledger: frame.ledger });
        break;

      case "geometry.ready":
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
        set({ busy: false, phase: null });
        get().say({
          kind: "error",
          text: frame.detail ? `${frame.code}: ${frame.message} (${frame.detail})` : `${frame.code}: ${frame.message}`,
        });
        break;

      default:
        break;
    }
  },

  async onGeometry({ revision, glb, step }) {
    // The viewport subscribes to this event rather than the store reading into
    // the scene — see components/Viewport.tsx.
    window.dispatchEvent(new CustomEvent("cadenza:glb", { detail: { glb, revision } }));
    socket.setShownRevision(revision);

    set((s) => ({
      busy: false,
      phase: null,
      model: {
        ...(s.model ?? {}),
        revision,
        glbBytes: glb.byteLength,
        stepBytes: step?.byteLength ?? 0,
      },
      // The old selection points at a face of a model that no longer exists.
      selection: null,
      promptOpen: false,
      verify: null,
    }));

    if (step && get().kernel.state === "ready") {
      try {
        const summary = await occt.loadStep(step);
        const t0 = performance.now();
        const mesh = await occt.getPickMesh();
        const pickMs = performance.now() - t0;
        set((s) => ({ model: s.model ? { ...s.model, summary, pickMs } : s.model }));
        window.dispatchEvent(new CustomEvent("cadenza:pickmesh", { detail: mesh }));
      } catch (err) {
        get().say({
          kind: "status",
          text: `STEP load failed: ${err instanceof Error ? err.message : String(err)}`,
        });
      }
    }
  },

  select: (sel) => set({ selection: sel, promptOpen: sel !== null, verify: null }),

  setFaceFacts: (facts) =>
    set((s) =>
      s.selection && s.selection.faceHash === facts.faceHash
        ? { selection: { ...s.selection, info: facts } }
        : {},
    ),

  openPrompt: (open) => set({ promptOpen: open }),

  /** Point & Speak: an edit anchored to the face the user clicked. */
  submitPrompt(text) {
    const sel = get().selection;
    if (!sel || text.trim() === "") return;

    socket.sendPrompt({
      prompt: text.trim(),
      target: { point: sel.point, normal: sel.normal },
      baseRevision: get().model?.revision,
    });

    get().say({ kind: "user", text: text.trim(), point: sel.point });
    set({ promptOpen: false, busy: true, phase: "sent" });
  },

  /** The composer: a prompt with no click, optionally carrying sketches. */
  submitComposer(text) {
    const { attachments, selection } = get();
    const prompt = text.trim();
    // An image on its own is a complete instruction; text alone is too. Only
    // the empty case is refused.
    if (prompt === "" && attachments.length === 0) return;

    socket.sendPrompt({
      prompt: prompt || "Build the part shown in the attached drawing.",
      images: attachments.map(({ media_type, data, name }) => ({ media_type, data, name })),
      baseRevision: get().model?.revision,
      ...(selection ? { target: { point: selection.point, normal: selection.normal } } : {}),
    });

    get().say({
      kind: "user",
      text: prompt || `(${attachments.length} sketch${attachments.length > 1 ? "es" : ""})`,
      images: attachments.map((a) => a.previewUrl),
      ...(selection ? { point: selection.point } : {}),
    });
    set({ busy: true, phase: "sent", attachments: [], selection: null, promptOpen: false });
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
    get().clearAttachments();
    socket.reset();
    set({ conversation: [], ledger: null, model: null, selection: null, busy: false, phase: null });
  },

  setVerify: (v) => set({ verify: v }),

  toggleGrid: () => set((s) => ({ showGrid: !s.showGrid })),
  toggleEdges: () => set((s) => ({ showEdges: !s.showEdges })),
}));
