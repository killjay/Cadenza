export type Checkpoint = {
  sequence: number; step: string; phase: string; prompt: string; target: string; detail: string;
  before: string | null; proposal: string | null; accepted: string | null;
};
export type Manifest = {
  version: 1; run_id: string; sequence: number; updated: string;
  status: "running" | "completed" | "failed" | "interrupted";
  checkpoints: Checkpoint[]; observer_error: string | null; estimated_usd: number | null;
};
export function validRun(value: string): boolean { return /^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$/.test(value); }
export function parseManifest(value: unknown, run: string): Manifest {
  const m = value as Manifest;
  if (!m || m.version !== 1 || m.run_id !== run || !Number.isSafeInteger(m.sequence) || m.sequence < 0 ||
      !["running", "completed", "failed", "interrupted"].includes(m.status) ||
      typeof m.updated !== "string" || !Number.isFinite(Date.parse(m.updated)) ||
      !Array.isArray(m.checkpoints) || m.checkpoints.length > 2000 ||
      !(m.observer_error === null || typeof m.observer_error === "string") ||
      !(m.estimated_usd === null || (Number.isFinite(m.estimated_usd) && m.estimated_usd >= 0))) throw new Error("Invalid viewer manifest");
  let sequence = 0;
  for (const c of m.checkpoints) {
    if (!c || !Number.isSafeInteger(c.sequence) || c.sequence <= sequence || c.sequence > m.sequence ||
        ![c.step, c.phase, c.prompt, c.target, c.detail].every(v => typeof v === "string") ||
        ![c.before, c.proposal, c.accepted].every(v => v === null || (typeof v === "string" && /^[0-9]+\.glb$/.test(v))))
      throw new Error("Invalid viewer checkpoint");
    sequence = c.sequence;
  }
  return m;
}
export class ViewerRequestError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function fetchManifest(run: string, signal: AbortSignal): Promise<Manifest> {
  if (!validRun(run)) throw new Error("Invalid run identifier");
  const response = await fetch(`/api/evaluations/${run}`, { signal, cache: "no-store" });
  if (!response.ok) throw new ViewerRequestError(response.status, response.status === 404 ? "Waiting for run; check viewer configuration and run ID" : `Viewer unavailable (${response.status})`);
  const text = await response.text();
  if (text.length > 2 * 1024 * 1024) throw new Error("Viewer manifest too large");
  return parseManifest(JSON.parse(text), run);
}

export type ViewerLocation = { kind: "editor" } | { kind: "error"; message: string } | { kind: "viewer"; runs: string[] };
export function viewerLocation(params: URLSearchParams): ViewerLocation {
  if (!params.has("evaluation") && !params.has("evaluations")) return { kind: "editor" };
  const single = params.getAll("evaluation"), batch = params.getAll("evaluations");
  if (single.length + batch.length !== 1) return { kind: "error", message: "Use one evaluation or evaluations parameter." };
  const runs = batch.length ? batch[0].split(",") : single;
  if (!runs.length || runs.length > 12 || new Set(runs).size !== runs.length || !runs.every(validRun))
    return { kind: "error", message: "Supply 1 to 12 distinct valid run IDs." };
  return { kind: "viewer", runs };
}
export type RunState = { manifest?: Manifest; error?: string; waiting?: boolean };
export type RunStates = Record<string, RunState | undefined>;
export type Selection = { run: string; sequence?: number };
export type ModelPhase = "before" | "proposal" | "accepted";
export function autoPhase(checkpoint?: Checkpoint): ModelPhase {
  return checkpoint?.proposal ? "proposal" : checkpoint?.accepted ? "accepted" : "before";
}
export function mergeManifest(previous: RunState | undefined, next: Manifest): RunState {
  return { manifest: previous?.manifest && previous.manifest.sequence > next.sequence ? previous.manifest : next };
}
export function activeSelection(runs: string[], states: RunStates, manual: Selection | null): Selection {
  if (manual) return manual;
  const run = [...runs].reverse().find(id => states[id]?.manifest?.checkpoints.length) ?? runs[0];
  return { run, sequence: states[run]?.manifest?.checkpoints.at(-1)?.sequence };
}
export function recordedSteps(runs: string[], states: RunStates): (Selection & { step: string })[] {
  return runs.flatMap(run => {
    const steps = new Map<string, Checkpoint>();
    for (const c of states[run]?.manifest?.checkpoints ?? []) steps.set(c.step, c);
    return [...steps.values()].map(c => ({ run, step: c.step, sequence: c.sequence }));
  });
}
export function recordedDecision(manifest: Manifest | undefined, checkpoint: Checkpoint | undefined): string | undefined {
  return manifest?.checkpoints.filter(c => c.step === checkpoint?.step && c.sequence <= checkpoint.sequence && c.phase === "decision").at(-1)?.detail;
}
export function batchStatus(runs: string[], states: RunStates): string {
  if (runs.some(run => states[run]?.error)) return "Batch observation error";
  if (runs.some(run => states[run]?.manifest?.status === "running")) return "Batch running";
  if (runs.some(run => !states[run]?.manifest)) return "Batch waiting for remaining runs";
  return runs.every(run => states[run]?.manifest?.status === "completed") ? "Batch completed" : "Batch finished with failures or interruption";
}

/** One bounded polling cycle at a time; terminal runs are retained without refetching. */
export function watchRuns(runs: string[], update: (states: RunStates) => void): () => void {
  const states: RunStates = {};
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  async function poll() {
    for (const run of runs) {
      if (stopped) return;
      const previous = states[run];
      if (previous?.manifest && previous.manifest.status !== "running") continue;
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 10000);
      try {
        const next = await fetchManifest(run, controller.signal);
        if (stopped) return;
        states[run] = mergeManifest(previous, next);
      } catch (error) {
        if (stopped) return;
        states[run] = error instanceof ViewerRequestError && error.status === 404 && !previous?.manifest
          ? { waiting: true }
          : { ...previous, error: `${error instanceof Error ? error.message : "Connection lost"}. Last view may be stale.` };
      } finally { clearTimeout(timeout); }
      update({ ...states });
    }
    if (!stopped && runs.some(run => !states[run]?.manifest || states[run]?.manifest?.status === "running"))
      timer = setTimeout(poll, 1000);
  }
  void poll();
  return () => { stopped = true; controller?.abort(); clearTimeout(timer); };
}
