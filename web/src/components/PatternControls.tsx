import { useState } from "react";
import { useStore } from "../lib/store";
import { patternCommand, patternDirections, patternRadius, patternReason, patternSeed, sourceNotice } from "../lib/featureCommands";
import type { PatternFeature } from "../lib/featureCommands";
import type { Ledger } from "../lib/types";

export function PatternControls({ ledger, feature }: { ledger: Ledger; feature?: PatternFeature }) {
  const state = useStore();
  const [kind, setKind] = useState(feature?.kind ?? "linear_pattern");
  const [seedId, setSeedId] = useState(feature?.parameters.target_feature ?? "");
  const [error, setError] = useState("");
  if (ledger.assembly || !state.patternCapabilities) return null;
  const seed = ledger.features.find(f => f.id === seedId);
  const reason = feature ? patternReason(feature, ledger) : null;
  const disabled = state.busy || !!state.draft || state.draftStarting || !!state.preview
    || state.connection !== "open" || state.model?.source === "local";
  const field = (name: string, label: string, initial?: number, integer = false) => <label className="block">{label}
    <input name={name} aria-label={label} type="number" required step={integer ? 1 : "any"}
      min={integer ? 2 : -1e9} max={integer ? 32 : 1e9} defaultValue={initial ?? ""}
      className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1" />
  </label>;
  return <details className="my-2 text-11">
    <summary className="cursor-pointer text-accent">{feature ? "Edit pattern" : "Add pattern"}</summary>
    <p className="my-1 text-10 text-ink-4">{sourceNotice} Count includes the seed. Copies follow changes to their seed.</p>
    {reason ? <p className="text-ink-3">{reason}</p> : <>
      {feature && <button disabled={disabled || !state.featurePromptAvailable || !state.agentsAvailable}
        className="text-accent disabled:opacity-40" onClick={() => state.startFeaturePrompt(ledger.project_id, feature.id)}>Edit with AI</button>}
      <form onSubmit={event => {
        event.preventDefault();
        try { const op = patternCommand(new FormData(event.currentTarget), ledger, feature);
          setError(""); state.previewFeatureCommand(ledger.project_id, [op]); }
        catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
      }}>
        <fieldset disabled={disabled || feature?.suppressed} className="space-y-1">
          {!feature && <>
            <label className="block">Pattern name<input name="name" aria-label="Pattern name" required maxLength={100}
              defaultValue="New pattern" className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1" /></label>
            <label className="block">Pattern type<select name="kind" aria-label="Pattern type" value={kind}
              onChange={e => setKind(e.target.value as typeof kind)} className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1">
              <option value="linear_pattern">Linear</option><option value="circular_pattern">Circular</option>
            </select></label>
            <label className="block">Pattern seed<select name="target_feature" aria-label="Pattern seed" required value={seedId}
              onChange={e => setSeedId(e.target.value)} className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1">
              <option value="" disabled>Select a seed</option>
              {ledger.features.filter(patternSeed).map(f => <option key={f.id} value={f.id}>{f.name || f.kind} ({f.id})</option>)}
            </select></label>
          </>}
          {seed && <p className="text-10 text-ink-3">Seed: {seed.name || seed.kind}. Copies {seed.operation === "add" ? "add" : "subtract"} material.
            {seed.kind === "hole" && ` Diameter ${seed.parameters.diameter} mm; ${seed.parameters.through ? "through" : `depth ${seed.parameters.depth} mm`}; opening Z ${seed.placement.origin[2]} mm.`}
            {seed.kind === "box" && ` ${seed.parameters.length} × ${seed.parameters.width} × ${seed.parameters.height} mm.`}
            {seed.kind === "cylinder" && ` Diameter ${seed.parameters.diameter} mm; height ${seed.parameters.height} mm.`}</p>}
          {field("count", "Total instances, including seed", feature?.parameters.count, true)}
          {kind === "linear_pattern" ? <>
            {field("spacing", "Pattern spacing (mm)", feature?.kind === "linear_pattern" ? feature.parameters.spacing : undefined)}
            <label className="block">Pattern direction<select name="direction" aria-label="Pattern direction" required defaultValue={feature?.kind === "linear_pattern"
              ? Object.entries(patternDirections).find(([, d]) => d.every((v, i) => v === feature.parameters.axis[i]))?.[0] : ""}
              className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1">
              <option value="" disabled>Select direction</option>
              {Object.keys(patternDirections).filter(d => seed?.kind !== "hole" || !d.endsWith("z")).map(d => <option key={d}>{d}</option>)}
            </select></label>
          </> : <>
            {field("center_x", "Pattern center X (mm)", feature?.placement.origin[0])}
            {field("center_y", "Pattern center Y (mm)", feature?.placement.origin[1])}
            <p className="text-10 text-ink-3">Full 360° about source Z. Radius comes from the seed position and center.
              {feature && seed && ` Current radius: ${Number(patternRadius(feature, seed).toFixed(6))} mm (read-only).`}</p>
          </>}
          <button className="py-1 text-accent disabled:opacity-40" disabled={!seed || disabled || feature?.suppressed}>Preview pattern</button>
        </fieldset>
        {error && <p role="alert" className="text-red">{error}</p>}
      </form>
      {seed && kind === "circular_pattern" && <button disabled={disabled} className="text-accent disabled:opacity-40" onClick={() => {
        const panel = document.getElementById(`feature-${ledger.project_id}-${seed.id}`);
        panel?.querySelectorAll("details").forEach(d => { d.open = true; });
        panel?.scrollIntoView({ behavior: "smooth", block: "center" });
        panel?.querySelector<HTMLInputElement>(`input[aria-label*="origin x"]`)?.focus();
      }}>Select seed to change radius</button>}
      {feature && <div className="mt-2 flex flex-wrap gap-3">
        <button disabled={disabled} className="text-accent disabled:opacity-40" onClick={() => state.previewFeatureCommand(ledger.project_id,
          [{ op: "set_suppressed", feature_id: feature.id, suppressed: !feature.suppressed }])}>{feature.suppressed ? "Preview restore" : "Preview suppress"}</button>
        <button disabled={disabled} className="text-red disabled:opacity-40" onClick={() => state.previewFeatureCommand(ledger.project_id,
          [{ op: "remove_feature", feature_id: feature.id }])}>Preview removal</button>
      </div>}
    </>}
  </details>;
}
