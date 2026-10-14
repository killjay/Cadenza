import { useRef } from "react";
import { useStore } from "../lib/store";
import { ProjectControls } from "./ProjectControls";

/**
 * Top chrome. In the prototype it doubles as the spike's control panel: load a
 * local GLB/STEP pair, or point at the fixtures, without a backend.
 */
export function TitleBar() {
  const sourceView = useStore(s => s.sourceView);
  const sourceLoading = useStore(s => s.sourceLoading);
  const model = useStore((s) => s.model);
  const showGrid = useStore((s) => s.showGrid);
  const showEdges = useStore((s) => s.showEdges);
  const toggleGrid = useStore((s) => s.toggleGrid);
  const toggleEdges = useStore((s) => s.toggleEdges);
  const say = useStore((s) => s.say);
  const undoSession = useStore((s) => s.undoSession);
  const sessionId = useStore((s) => s.sessionId);
  const busy = useStore((s) => s.busy);
  const preview = useStore((s) => s.preview);
  const draft = useStore((s) => s.draft);
  const canUndo = useStore((s) => s.canUndo);
  const canRedo = useStore((s) => s.canRedo);
  const redoSession = useStore((s) => s.redoSession);

  const glbInput = useRef<HTMLInputElement>(null);
  const stepInput = useRef<HTMLInputElement>(null);

  async function loadFixture(): Promise<void> {
    const [glb, step] = await Promise.all([
      fetch("/samples/plate.glb").then((r) => r.arrayBuffer()),
      fetch("/samples/plate.step").then((r) => r.arrayBuffer()),
    ]);
    void useStore.getState().onGeometry({ revision: 0, glb, step, source: "local" });
    // Say so out loud. A fixture that arrives in the viewport unannounced is
    // indistinguishable from a part the server just built, and that ambiguity
    // has already cost one debugging session.
    say({ kind: "status", text: "Loaded the local fixture plate — not a server build." });
  }

  /** These load a MODEL, not a drawing. Handing them a PNG used to throw deep
      inside GLTFLoader with nothing shown to the user — and "Open GLB…" is the
      most upload-shaped button on screen, so it is exactly where someone tries
      to put their sketch first. */
  function rejectIfImage(file: File, expected: string): boolean {
    if (!file.type.startsWith("image/")) return false;
    say({
      kind: "error",
      text: `${file.name} is an image, not a ${expected} model. To build a part from a drawing, attach it with the 📎 sketch button in this pane instead.`,
    });
    return true;
  }

  async function onGlbPicked(file: File): Promise<void> {
    if (rejectIfImage(file, "GLB")) return;
    const glb = await file.arrayBuffer();
    await useStore.getState().onGeometry({ revision: 0, glb, source: "local" });
    say({ kind: "status", text: `Loaded ${file.name} (${(glb.byteLength / 1024).toFixed(0)} KB).` });
  }

  async function onStepPicked(file: File): Promise<void> {
    if (rejectIfImage(file, "STEP")) return;
    const step = await file.arrayBuffer();
    if (useStore.getState().model?.source !== "local") {
      say({ kind: "status", text: "Open the matching GLB as a local preview before attaching its STEP file." });
      return;
    }
    await useStore.getState().loadStep(step);
  }

  const btn =
    "rounded-sm px-2 py-[3px] text-11 text-ink-3 hover:bg-wash hover:text-ink-2 transition-colors";
  const btnOn = "rounded-sm px-2 py-[3px] text-11 bg-wash-2 text-accent";

  return (
    <header className="flex items-center gap-1 border-b border-hairline bg-chrome px-2">
      <span className="mr-2 select-none text-12 font-medium tracking-tight text-ink">
        CADenza
        <span className="ml-1.5 rounded-sm bg-wash px-1 py-px text-9 text-ink-4">prototype</span>
      </span>

      <ProjectControls />
      <button className={btn} disabled={!!draft || !!preview || busy} onClick={() => void loadFixture()}>
        Load fixture
      </button>
      <button className={btn} disabled={!!draft || !!preview || busy} onClick={() => glbInput.current?.click()}>
        Open GLB…
      </button>
      <button className={btn} disabled={!!draft || !!preview || busy} onClick={() => stepInput.current?.click()}>
        Open STEP…
      </button>

      <div className="mx-1 h-3.5 w-px bg-hairline-strong" />

      <button className={showGrid ? btnOn : btn} onClick={toggleGrid} title="G">
        Grid
      </button>
      <button className={showEdges ? btnOn : btn} onClick={toggleEdges} title="E" disabled={!!draft && !preview}>
        Edges
      </button>
      <button
        className={btn}
        title="F"
        onClick={() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "f" }))}
      >
        Fit
      </button>

      <div className="mx-1 h-3.5 w-px bg-hairline-strong" />

      <button className={btn} onClick={() => undoSession()} disabled={sourceLoading || busy || !!draft || !!preview || !canUndo}>
        Undo
      </button>
      <button className={btn} onClick={redoSession} disabled={sourceLoading || busy || !!draft || !!preview || !canRedo}>Redo</button>
      <button className={btn} onClick={() => { if (sessionId && model) window.open(model.source === "source" ? sourceView?.glb_url ?? undefined : `/sessions/${sessionId}/artifacts/${model.revision}/model.glb`) }} disabled={sourceLoading || !!preview || (model?.source === "source" && !sourceView) || !sessionId || !model || model.source === "local" || model.source === "preview"}>
        Download GLB
      </button>
      <button className={btn} onClick={() => { if (sessionId && model) window.open(model.source === "source" ? sourceView?.step_url ?? undefined : `/sessions/${sessionId}/artifacts/${model.revision}/model.step`) }} disabled={sourceLoading || !!preview || (model?.source === "source" && !sourceView) || !sessionId || !model || model.source === "local" || model.source === "preview"}>
        Download STEP
      </button>

      <div className="flex-1" />
      {preview && <span className="text-11 text-accent">Preview — not applied</span>}

      {model && (
        <span className="font-mono text-10 text-ink-4">
          {model.source === "local" ? "Local preview" : `rev ${model.revision}`}
          {model.triangleCount ? ` · ${model.triangleCount.toLocaleString()} tris` : ""}
        </span>
      )}

      <input
        ref={glbInput}
        type="file"
        accept=".glb,.gltf,model/gltf-binary"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void onGlbPicked(f);
          e.target.value = "";
        }}
      />
      <input
        ref={stepInput}
        type="file"
        accept=".step,.stp"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void onStepPicked(f);
          e.target.value = "";
        }}
      />
    </header>
  );
}
