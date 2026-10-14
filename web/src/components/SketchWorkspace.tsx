import { useState } from "react";
import { useStore } from "../lib/store";
import { SketchCanvas } from "./SketchCanvas";
import { SketchPrompt } from "./SketchPrompt";
import type { SketchTool } from "../lib/sketchView";

export function SketchWorkspace() {
  const state = useStore(), draft = state.draft!;
  const [tool, setTool] = useState<SketchTool>("select");
  const active = !state.preview;
  const disabled = state.busy || state.connection !== "open";
  const populated = draft.definition.entities.length > 0;
  const profile = state.ledgers[draft.ledger_id]?.features.find(f => f.id === draft.profile_id);
  return <section aria-label="Sketch workspace" className={`${active ? "flex" : "hidden"} absolute inset-0 z-10 min-h-0 flex-col bg-canvas`}>
    <div className="flex flex-wrap items-center gap-3 border-b border-hairline bg-chrome p-3 text-12">
      <strong>Sketch · local coordinates (mm)</strong>
      {(["select", "rectangle", "circle", "opening"] as const).map(value => <button key={value} aria-pressed={tool === value}
        className="rounded border border-hairline px-2 py-1 aria-pressed:text-accent disabled:opacity-40"
        disabled={disabled || (value === "opening" ? !populated || draft.definition.entities.length >= 16 : value !== "select" && populated)}
        onClick={() => setTool(value)}>{value === "select" ? "Select" : value === "rectangle" ? "Draw rectangle" : value === "circle" ? "Draw circle" : "Draw opening"}</button>)}
      <button onClick={() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "f" }))}>Fit sketch</button>
      <span className="text-ink-4">{profile ? `Plane origin (${profile.placement.origin.join(", ")}), rotation (${profile.placement.rotation_deg.join(", ")})°` : "XY plane"}</span>
    </div>
    <SketchCanvas active={active} tool={tool} setTool={setTool} />
    <SketchPrompt />
    <p className="border-t border-hairline bg-chrome px-3 py-2 text-11 text-ink-3">Temporary draft · add explicit dimensions and constraints before Finish · accepted revision {draft.base_revision}</p>
  </section>;
}
