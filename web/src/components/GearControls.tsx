import { useEffect, useState } from "react";
import { useStore } from "../lib/store";
import { gearReason, readGear } from "../lib/gear";
import type { CommandOperation, Feature, Ledger } from "../lib/types";
import { NumericInput } from "./NumericInput";

type AddGear = Extract<CommandOperation, { op: "add_gear" }>;
const defaults = { module: "1", teeth: "24", height: "5", pressure_angle_deg: "20", shift: "0" };
const inputClass = "w-full min-w-0 rounded border border-hairline bg-canvas p-1";

export function GearControls({ ledger, feature, newPart }: { ledger?: Ledger; feature?: Extract<Feature, { kind: "gear" }>;
  newPart?: { name: string; onPreview: (op: AddGear) => void } }) {
  const state = useStore();
  const initial = feature ? Object.fromEntries(Object.entries(feature.parameters).map(([k, v]) => [k, String(v)])) : defaults;
  const [fields, setFields] = useState<Record<string, string>>(initial);
  const [name, setName] = useState("Spur gear"), [origin, setOrigin] = useState(["0", "0", "0"]), [rotation, setRotation] = useState("0");
  const [dirty, setDirty] = useState(false), [error, setError] = useState("");
  const reset = () => { setFields(initial); setName("Spur gear"); setOrigin(["0", "0", "0"]); setRotation("0"); setDirty(false); setError(""); };
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: Event) => {
      const decision = (event as CustomEvent<{ discard?: boolean }>).detail;
      decision.discard ??= window.confirm("Discard unsent gear edits and change target?");
      if (decision.discard) reset(); else event.preventDefault();
    };
    window.addEventListener("cadenza:discard-numeric-edits", guard);
    return () => window.removeEventListener("cadenza:discard-numeric-edits", guard);
  });
  const reason = feature ? gearReason(feature, ledger)
    : ledger?.features.some(f => f.kind === "gear" && !f.suppressed)
      ? "This part already has an active gear. Edit it, or create another independent part." : null;
  const bound = feature && ledger?.relationships?.bindings.some(b => b.feature_id === feature.id);
  const blocked = state.busy || !!state.preview || !!state.draft || state.draftStarting || state.sourceLoading
    || state.connection !== "open" || state.model?.source === "local" || !!state.faceAttachment;
  const unavailable = !state.gearCapabilities || (!!newPart && state.partCapabilities?.version !== 2);
  const disabled = blocked || unavailable || !!reason || !!bound || !!feature?.suppressed || (!!newPart && !newPart.name.trim());
  let derived = "";
  try {
    const g = readGear(fields);
    derived = `Pitch diameter ${g.module*g.teeth} mm; outside diameter ${g.module*(g.teeth+2+2*g.shift)} mm; root diameter ${g.module*(g.teeth-2.5+2*g.shift)} mm.`;
  } catch { /* Incomplete drafts do not show derived values. */ }
  const field = (key: string, label: string, min: number, max: number, step = "any") => <label key={key} className="block">{label}
    <NumericInput aria-label={label} required min={min} max={max} step={step} value={fields[key]} className={inputClass}
      onChange={e => setFields({ ...fields, [key]: e.target.value })} /></label>;
  return <details className="my-2 text-11" open={feature || newPart ? true : undefined}>
    <summary className="cursor-pointer text-accent">{feature ? "Gear settings" : newPart ? "Gear shape" : "Add spur gear"}</summary>
    <p className="my-2">External spur gear, axis +Z, base at the placement origin. One gear per source; hubs and holes stay inside the root body.</p>
    <p>Radial root relief is approximate. Generated undercut and manufacturing tolerances are not modeled; a valid build does not prove gear engagement.</p>
    {unavailable && <p>Gear authoring is unavailable on this connection.</p>}
    {reason && <p>{reason}</p>}{bound && <p>Edit the driving parameters for this bound gear.</p>}
    {feature && <p>Placement retained: {feature.placement.origin.join(", ")} mm; rotation {feature.placement.rotation_deg.join(", ")} degrees.</p>}
    {feature && ledger && <button className="text-accent disabled:opacity-40" disabled={disabled || !state.agentsAvailable || !state.featurePromptAvailable}
      onClick={() => state.startFeaturePrompt(ledger.project_id, feature.id)}>Edit with AI</button>}
    <form aria-label={feature ? "Edit gear" : "Create gear"} onSubmit={event => {
      event.preventDefault(); if (disabled) return;
      try {
        const settings = readGear(fields);
        const position = (text: string) => {
          if (!text.trim() || !Number.isFinite(Number(text)) || Math.abs(Number(text)) > 1e9) throw Error("Enter finite placement values within +/-1,000,000,000.");
          return Number(text);
        };
        const title = (newPart?.name ?? name).trim();
        if (!feature && (!title || title.length > 100)) throw Error("Enter a name of 1 to 100 characters.");
        setError("");
        if (feature && ledger) state.previewFeatureCommand(ledger.project_id, [{ op: "update_gear", feature_id: feature.id, ...settings }]);
        else {
          const op: AddGear = { op: "add_gear", name: title, operation: "add", ...settings,
            placement: { origin: origin.map(position) as [number, number, number], rotation_deg: [0, 0, position(rotation)], relative_to: null } };
          if (newPart) newPart.onPreview(op); else if (ledger) state.previewFeatureCommand(ledger.project_id, [op]);
        }
      } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    }}>
      <fieldset disabled={disabled} className="space-y-2" onChange={() => setDirty(true)}>
        {!feature && !newPart && <label className="block">Feature name<input aria-label="Gear name" value={name} required maxLength={100} className={inputClass} onChange={e => setName(e.target.value)} /></label>}
        {field("module", "Module (mm)", 1e-7, 1000)}{field("teeth", "Teeth", 3, 128, "1")}{field("height", "Face width (mm)", 1e-7, 1e6)}
        {derived && <p>{derived}</p>}
        <details><summary className="cursor-pointer">Additional settings</summary>
          {field("pressure_angle_deg", "Pressure angle (degrees)", 5, 45)}{field("shift", "Profile shift", -1, 1)}
          {!feature && <>
            <div className="grid grid-cols-3 gap-1">{["X", "Y", "Z"].map((axis, i) => <label key={axis}>Origin {axis}
              <NumericInput aria-label={`Gear origin ${axis} (mm)`} required step="any" value={origin[i]} className={inputClass}
                onChange={e => setOrigin(origin.map((v, j) => j === i ? e.target.value : v))} /></label>)}</div>
            <label>Rotation Z (degrees)<NumericInput aria-label="Gear rotation Z (degrees)" required step="any" value={rotation} className={inputClass} onChange={e => setRotation(e.target.value)} /></label>
          </>}
        </details>
        <div className="flex gap-3"><button className="text-accent" disabled={!!feature && !dirty}>Preview gear</button>
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
