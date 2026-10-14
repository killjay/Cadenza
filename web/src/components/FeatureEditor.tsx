import { useState } from "react";
import { useStore } from "../lib/store";
import { fieldBinding, readNumber, simpleFeature, sourceNotice } from "../lib/featureCommands";
import type { Feature, ParameterEdit } from "../lib/types";
import { isPattern } from "../lib/featureCommands";
import { PatternControls } from "./PatternControls";

function NumericField({ label, value, signed, disabled, submit }: {
  label: string; value: number | null; signed?: boolean; disabled: boolean; submit: (value: number) => void;
}) {
  const [error, setError] = useState("");
  return <form className="my-1" onSubmit={event => {
    event.preventDefault();
    try { const number = readNumber(new FormData(event.currentTarget).get("value"), signed); setError(""); submit(number); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  }}>
    <div className="flex items-end gap-1">
      <label className="min-w-0 flex-1">{label}<input name="value" type="number" required step="any"
        min={signed ? -1e9 : 0} max={signed ? 1e9 : undefined} aria-label={label} defaultValue={value ?? ""}
        disabled={disabled} className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1" /></label>
      <button disabled={disabled} className="p-1 text-accent disabled:opacity-40">Preview</button>
    </div>
    {error && <p role="alert" className="text-red">{error}</p>}
  </form>;
}

export function FeatureEditor({ ledgerId, feature }: { ledgerId: string; feature: Feature }) {
  const state = useStore(), ledger = state.ledgers[ledgerId];
  if (!ledger || ledger.assembly) return null;
  if (isPattern(feature)) return <PatternControls key={`${state.sessionId}:${state.sessionRevision}:${feature.id}`} ledger={ledger} feature={feature} />;
  const capability = state.capabilities?.features.find(c => c.kind === feature.kind);
  // Compatibility only: older servers do not advertise a command capability table.
  const legacy: ParameterEdit["parameter"][] = feature.kind === "box" ? ["length", "width", "height"]
    : feature.kind === "cylinder" ? ["diameter", "height"] : feature.kind === "extrude" ? ["amount"]
    : feature.kind === "hole" ? ["diameter", "depth"] : [];
  const promoted = !!capability && simpleFeature(feature);
  const dimensions = (state.capabilities ? promoted ? capability.dimensions : [] : legacy)
    .filter(field => !(feature.kind === "hole" && feature.parameters.through && field === "depth"));
  if (!dimensions.length && !promoted) return legacy.length ? <p className="text-10 text-ink-4">Direct controls are unavailable for this feature mode.</p> : null;
  const disabled = state.busy || !!state.draft || state.draftStarting || !!state.preview
    || state.connection !== "open" || state.model?.source === "local";
  const parameters = feature.parameters as unknown as Record<string, number | null>;
  const title = feature.name || feature.kind;
  const bindingNote = (field: string) => {
    const binding = fieldBinding(ledger, feature.id, field);
    return binding.expression && <p className="mt-1 break-words text-10 text-ink-3">{field} = {binding.expression}
      {binding.derived ? " · Edit the driving values under Parameters." : " · Updates its named parameter."}</p>;
  };
  return <details className="mt-1 text-11">
    <summary className="cursor-pointer text-ink-3">{feature.suppressed ? "Suppressed feature controls" : "Edit feature"}</summary>
    <p className="my-1 text-10 text-ink-4">{sourceNotice} Dimensions and source-local positions are in mm.</p>
    {promoted && <button disabled={disabled || !state.featurePromptAvailable || !state.agentsAvailable}
      className="text-accent disabled:opacity-40" onClick={() => state.startFeaturePrompt(ledgerId, feature.id)}>Edit with AI</button>}
    {!state.capabilities && <p className="text-10 text-ink-4">Additional controls unavailable on this connection. Legacy dimension editing is available.</p>}
    {!feature.suppressed && dimensions.map(parameter => <div key={`${state.sessionId}:${state.sessionRevision}:${parameter}`}>
      {bindingNote(parameter)}
      <NumericField label={`${title} ${parameter} (mm)`} value={parameters[parameter]} disabled={disabled || fieldBinding(ledger, feature.id, parameter).derived}
        submit={value => state.capabilities
          ? state.previewFeatureCommand(ledgerId, [{ op: "set_dimension", feature_id: feature.id, parameter, value }])
          : state.editParameter(ledgerId, feature.id, parameter, value)} />
    </div>)}
    {promoted && !feature.suppressed && capability.origin_axes.length > 0 && (
      feature.placement.relative_to !== null ? <p className="text-10 text-ink-4">Position uses a relative placement; direct movement is unavailable.</p>
      : capability.origin_axes.map(axis => <div key={`${state.sessionId}:${state.sessionRevision}:${axis}`}>
        {bindingNote(axis)}
        <NumericField label={`${title} origin ${axis} (mm)`} value={feature.placement.origin[axis === "x" ? 0 : axis === "y" ? 1 : 2]}
          signed disabled={disabled || fieldBinding(ledger, feature.id, axis).derived}
          submit={value => state.previewFeatureCommand(ledgerId, [{ op: "set_origin", feature_id: feature.id, axis, value }])} />
      </div>))}
    {promoted && <div className="mt-2 flex flex-wrap gap-3">
      {capability.suppress && <button disabled={disabled} className="text-accent disabled:opacity-40"
        onClick={() => state.previewFeatureCommand(ledgerId, [{ op: "set_suppressed", feature_id: feature.id, suppressed: !feature.suppressed }])}>
        {feature.suppressed ? "Preview restore" : "Preview suppress"}</button>}
      {capability.remove && <button disabled={disabled} className="text-red disabled:opacity-40"
        onClick={() => state.previewFeatureCommand(ledgerId, [{ op: "remove_feature", feature_id: feature.id }])}>Preview removal</button>}
      <p className="text-10 text-ink-4">Dependents are never removed automatically. Apply or cancel after reviewing.</p>
    </div>}
  </details>;
}
