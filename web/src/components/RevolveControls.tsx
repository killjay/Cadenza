import { useEffect, useState } from "react";
import { useStore } from "../lib/store";
import type { CommandOperation, Ledger, RevolveFeature } from "../lib/types";
import { readRevolve, revolveReason } from "../lib/revolve";
import { NumericInput } from "./NumericInput";

const defaultRows = [["0", "0"], ["10", "0"], ["10", "5"], ["5", "5"], ["5", "20"], ["0", "20"]];
const inputClass = "w-full min-w-0 rounded border border-hairline bg-canvas p-1";

export function RevolveControls({ ledger, feature, newPart }: { ledger?: Ledger; feature?: RevolveFeature;
  newPart?: { name: string; onPreview: (op: Extract<CommandOperation, { op: "add_revolve" }>) => void } }) {
  const state = useStore();
  const initialRows = feature ? feature.parameters.vertices.map(row => row.map(String)) : defaultRows;
  const initialAngle = String(feature?.parameters.angle ?? 360);
  const [rows, setRows] = useState(initialRows), [angle, setAngle] = useState(initialAngle);
  const [name, setName] = useState("Turned feature"), [operation, setOperation] = useState<"add" | "subtract">("add");
  const [origin, setOrigin] = useState(["0", "0", "0"]), [rotation, setRotation] = useState(["90", "0", "0"]);
  const [error, setError] = useState(""), [dirty, setDirty] = useState(false);
  const reset = () => {
    setRows(initialRows); setAngle(initialAngle); setOrigin(["0", "0", "0"]); setRotation(["90", "0", "0"]);
    setName("Turned feature"); setOperation("add"); setDirty(false); setError("");
  };
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: Event) => {
      const decision = (event as CustomEvent<{ discard?: boolean }>).detail;
      decision.discard ??= window.confirm("Discard unsent revolve edits and change target?");
      if (decision.discard) reset(); else event.preventDefault();
    };
    window.addEventListener("cadenza:discard-numeric-edits", guard);
    return () => window.removeEventListener("cadenza:discard-numeric-edits", guard);
  });
  const unavailable = !state.capabilities?.revolve || (!!newPart && state.partCapabilities?.version !== 2);
  const reason = feature ? revolveReason(feature) : null;
  const bound = feature && ledger?.relationships?.bindings.some(b => b.feature_id === feature.id);
  const blocked = state.busy || !!state.preview || !!state.draft || state.draftStarting || state.sourceLoading
    || state.connection !== "open" || state.model?.source === "local" || !!state.faceAttachment;
  const disabled = blocked || unavailable || !!reason || !!feature?.suppressed || !!bound || (!!newPart && !newPart.name.trim());
  let points: [number, number][] = [];
  try { points = readRevolve(rows, angle).vertices; } catch { /* Incomplete drafts have no profile thumbnail. */ }
  const maxR = Math.max(1, ...points.map(p => p[0]));
  const minY = Math.min(0, ...points.map(p => p[1])), maxY = Math.max(1, ...points.map(p => p[1]));
  const plot = points.map(([r, y]) => `${20+160*r/maxR},${180-160*(y-minY)/(maxY-minY)}`).join(" ");
  return <details className="my-2 text-11" open={feature || newPart ? true : undefined}>
    <summary className="cursor-pointer text-accent">{feature ? "Revolve settings" : newPart ? "Revolve shape" : "Add revolve"}</summary>
    <p className="my-2">Profile: radius X and axial Y (mm), rotated about local +Y. Angle is in degrees.
      Closure is automatic; use distinct corners without crossings or redundant straight-line corners.</p>
    {unavailable && <p>Update the backend to use revolve controls. Existing geometry remains available.</p>}
    {reason && <p>{reason}</p>}
    {bound && <p>This feature has parameter bindings. Edit its driving parameters instead.</p>}
    {feature && <p>Placement retained: {feature.placement.origin.join(", ")} mm; rotation {feature.placement.rotation_deg.join(", ")}°.</p>}
    {feature && ledger && <button className="text-accent disabled:opacity-40" disabled={disabled || !state.agentsAvailable || !state.featurePromptAvailable}
      onClick={() => state.startFeaturePrompt(ledger.project_id, feature.id)}>Edit with AI</button>}
    <form aria-label={feature ? "Edit revolve" : "Create revolve"} onSubmit={event => {
      event.preventDefault(); if (disabled) return;
      try {
        const settings = readRevolve(rows, angle);
        const vector = (values: string[]) => values.map(text => {
          const value = Number(text);
          if (!text.trim() || !Number.isFinite(value) || Math.abs(value) > 1e9) throw Error("Enter finite placement values within ±1,000,000,000.");
          return value;
        }) as [number, number, number];
        const title = (newPart?.name ?? name).trim();
        if (!feature && (!title || title.length > 100)) throw Error("Enter a feature name of 1–100 characters.");
        setError("");
        if (feature && ledger) state.previewFeatureCommand(ledger.project_id, [{ op: "update_revolve", feature_id: feature.id, ...settings }]);
        else {
          const op: Extract<CommandOperation, { op: "add_revolve" }> = { op: "add_revolve", name: title, operation: newPart ? "add" : operation, ...settings,
            placement: { origin: vector(origin), rotation_deg: vector(rotation), relative_to: null } };
          if (newPart) newPart.onPreview(op); else if (ledger) state.previewFeatureCommand(ledger.project_id, [op]);
        }
      } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    }}>
      <fieldset disabled={disabled} className="space-y-2" onChange={() => setDirty(true)}>
        {!feature && !newPart && <>
          <label className="block">Feature name<input aria-label="Revolve name" value={name} maxLength={100}
            className={inputClass} onChange={e => setName(e.target.value)} /></label>
          <label className="block">Operation<select aria-label="Revolve operation" value={operation} className={inputClass}
            onChange={e => setOperation(e.target.value as "add" | "subtract")}>
            <option value="add">Add material</option><option value="subtract">Subtract material</option>
          </select></label>
        </>}
        <label className="block">Angle (degrees)<NumericInput aria-label="Revolve angle (degrees)" required min="0" max="360"
          step="any" value={angle} onChange={e => setAngle(e.target.value)} className={inputClass} /></label>
        <table className="w-full"><thead><tr><th>Radius X (mm)</th><th>Axial Y (mm)</th><th>Corner</th></tr></thead>
          <tbody>{rows.map((row, index) => <tr key={index}>{row.map((value, axis) => <td key={axis}>
            <NumericInput aria-label={`Corner ${index+1} ${axis ? "axial Y" : "radius X"} (mm)`} required step="any"
              min={axis ? -1e6 : 0} max={1e6} value={value} className={inputClass}
              onChange={e => setRows(rows.map((p, i) => i === index ? p.map((v, a) => a === axis ? e.target.value : v) : p))} />
          </td>)}<td><button type="button" aria-label={`Remove corner ${index+1}`} disabled={rows.length <= 3}
            className="text-accent disabled:opacity-40" onClick={() => { setRows(rows.filter((_, i) => i !== index)); setDirty(true); }}>Remove</button></td></tr>)}</tbody>
        </table>
        <button type="button" disabled={rows.length >= 64} className="text-accent disabled:opacity-40"
          onClick={() => { setRows([...rows, ["", ""]]); setDirty(true); }}>Add corner</button>
        {points.length > 0 && <figure><svg role="img" aria-label="Revolve profile preview, independently scaled axes" viewBox="0 0 200 200" className="h-40 w-full">
          <path d="M20 10V185H190" fill="none" stroke="currentColor" strokeDasharray="4" />
          <polygon points={plot} fill="currentColor" fillOpacity=".15" stroke="currentColor" />
        </svg><figcaption>Profile preview; axes scaled independently. Dashed vertical line is the rotation axis.</figcaption></figure>}
        {!feature && <>
          <p>Source-local placement. Default X rotation 90° points the revolve axis toward source +Z.</p>
          {[["Origin", origin, setOrigin, "mm"], ["Rotation", rotation, setRotation, "degrees"]].map(([label, values, setter, unit]) => {
            const fields = values as string[], update = setter as typeof setOrigin;
            return <div key={String(label)} className="grid grid-cols-3 gap-1">{["X", "Y", "Z"].map((axis, i) =>
              <label key={axis}>{String(label)} {axis}<NumericInput aria-label={`Revolve ${label} ${axis} (${unit})`}
                required step="any" value={fields[i]} className={inputClass}
                onChange={e => update(fields.map((v, j) => i === j ? e.target.value : v))} /></label>)}</div>;
          })}
        </>}
        <div className="flex gap-3"><button className="text-accent" disabled={!!feature && !dirty}>Preview revolve</button>
          <button type="button" className="text-accent" onClick={reset}>Reset</button></div>
      </fieldset>
      {error && <p role="alert" className="text-red">{error}</p>}
    </form>
    {feature && ledger && !unavailable && !reason && <div className="my-2 flex gap-3">
      <button className="text-accent disabled:opacity-40" disabled={blocked} onClick={() => state.previewFeatureCommand(ledger.project_id,
        [{ op: "set_suppressed", feature_id: feature.id, suppressed: !feature.suppressed }])}>{feature.suppressed ? "Preview restore" : "Preview suppress"}</button>
      <button className="text-red disabled:opacity-40" disabled={blocked} onClick={() => state.previewFeatureCommand(ledger.project_id,
        [{ op: "remove_feature", feature_id: feature.id }])}>Preview removal</button>
    </div>}
  </details>;
}
