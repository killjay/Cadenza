import { useState } from "react";
import { useStore } from "../lib/store";
import { creationCommand, profileTargets, sourceNotice } from "../lib/featureCommands";
import type { Ledger } from "../lib/types";

export function FeatureCreate({ ledger }: { ledger: Ledger }) {
  const state = useStore();
  const [selectedKind, setKind] = useState("box"), [through, setThrough] = useState(true), [error, setError] = useState("");
  if (ledger.assembly) return null;
  const choices = state.capabilities?.features.filter(c => c.create_operations.length) ?? [];
  const capability = choices.find(c => c.kind === selectedKind) ?? choices[0];
  const kind = capability?.kind ?? selectedKind;
  const disabled = state.busy || !!state.draft || state.draftStarting || !!state.preview
    || state.connection !== "open" || state.model?.source === "local";
  const profiles = profileTargets(ledger);
  return <details className="my-2 text-11">
    <summary className="cursor-pointer text-accent">Add feature</summary>
    {!choices.length ? <p className="text-ink-4">Feature creation is unavailable on this connection.</p> : <>
      <p className="my-1 text-10 text-ink-4">{sourceNotice}</p>
      <label>Feature type<select aria-label="Feature type" value={kind} disabled={disabled}
        className="my-1 w-full rounded border border-hairline bg-canvas p-1"
        onChange={event => { setKind(event.target.value); setThrough(true); setError(""); }}>
        {choices.map(c => <option key={c.kind} value={c.kind}>{c.kind}</option>)}
      </select></label>
      {capability && <form key={kind} onSubmit={event => {
        event.preventDefault();
        try { const op = creationCommand(new FormData(event.currentTarget), capability, ledger);
          setError(""); state.previewFeatureCommand(ledger.project_id, [op]); }
        catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
      }}>
        <fieldset disabled={disabled} className="space-y-1">
          <label className="block">Feature name<input name="name" aria-label="Feature name" required maxLength={100} defaultValue={`New ${kind}`}
            className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1" /></label>
          <label className="block">Operation<select name="operation" aria-label="Feature operation"
            className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1">
            {capability.create_operations.map(op => <option key={op} value={op}>{op === "add" ? "Add material" : "Subtract material"}</option>)}
          </select></label>
          {kind === "hole" && <label className="block">Hole extent<select name="through" aria-label="Hole extent" value={through ? "through" : "blind"}
            className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1" onChange={event => setThrough(event.target.value === "through")}>
            <option value="through">Through from opening</option><option value="blind">Blind depth</option>
          </select></label>}
          {capability.dimensions.filter(field => !(kind === "hole" && through && field === "depth")).map(field =>
            <label key={field} className="block">{field} (mm)<input name={field} type="number" aria-label={`New ${kind} ${field} (mm)`}
              required min="0" step="any" defaultValue={10} className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1" /></label>)}
          {kind === "extrude" ? <>
            <label className="block">Source profile<select name="target_feature" aria-label="Source profile" required defaultValue=""
              className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1">
              <option value="" disabled>Select a profile</option>
              {profiles.map(f => <option key={f.id} value={f.id}>{f.name || f.kind} ({f.id})</option>)}
            </select></label>
            <p className="text-10 text-ink-4">Extrudes along the profile normal with no extra translation or rotation.</p>
            {!profiles.length && <p className="text-10 text-ink-4">Create a profile using the sketch controls first.</p>}
          </> : <>
            <p className="text-10 text-ink-4">Source-local position (mm). Rotation: 0°, 0°, 0°.
              {kind === "hole" ? " Hole cuts along local -Z from its opening. Enter the opening height Z, including for through holes." : " Centered in X/Y; the base starts at Z."}</p>
            <div className="grid grid-cols-3 gap-1">{["x", "y", "z"].map(axis => <label key={axis} className="min-w-0">{axis}
              <input name={axis} aria-label={`New ${kind} origin ${axis} (mm)`} type="number" required min={-1e9} max={1e9} step="any" defaultValue={kind === "hole" && axis === "z" ? "" : 0}
                className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1" /></label>)}</div>
          </>}
          <button disabled={disabled || (kind === "extrude" && !profiles.length)} className="py-1 text-accent disabled:opacity-40">Preview feature</button>
        </fieldset>
        {error && <p role="alert" className="text-red">{error}</p>}
      </form>}
    </>}
  </details>;
}
