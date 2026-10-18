import { useEffect, useState } from "react";
import { useStore } from "../lib/store";
import { featureEditCommands, fieldBinding, readNumber, simpleFeature, sourceNotice } from "../lib/featureCommands";
import type { FeatureEditField } from "../lib/featureCommands";
import type { Feature, ParameterEdit } from "../lib/types";
import { isPattern } from "../lib/featureCommands";
import { PatternControls } from "./PatternControls";
import { NumericInput } from "./NumericInput";

function useDraftGuard(dirty: boolean, reset: () => void) {
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: Event) => {
      const decision = (event as CustomEvent<{ discard?: boolean }>).detail;
      decision.discard ??= window.confirm("Discard unsent edits and change target? Choose Cancel to keep editing.");
      if (decision.discard) reset(); else event.preventDefault();
    };
    window.addEventListener("cadenza:discard-numeric-edits", guard);
    return () => window.removeEventListener("cadenza:discard-numeric-edits", guard);
  }, [dirty, reset]);
}

function FeatureFields({ ledgerId, featureId, fields, disabled }: {
  ledgerId: string; featureId: string; fields: FeatureEditField[]; disabled: boolean;
}) {
  const revision = useStore(s => s.sessionRevision);
  const [baseRevision, setBaseRevision] = useState(revision);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const dirty = Object.keys(draft).length > 0;
  const reset = () => { setDraft({}); setError(""); setNotice(""); };
  useDraftGuard(dirty, reset);
  useEffect(() => {
    if (revision === baseRevision) return;
    setDraft({}); setError(""); setBaseRevision(revision);
    if (dirty) setNotice("The design changed. Numeric edits were reset to the current values.");
  }, [revision, baseRevision, dirty]);
  return <form className="space-y-2" onSubmit={event => {
    event.preventDefault();
    if (disabled || revision !== baseRevision) return;
    try {
      const operations = featureEditCommands(featureId, fields, draft);
      if (!operations.length) { setError("Change a value before requesting a preview."); return; }
      setError(""); useStore.getState().previewFeatureCommand(ledgerId, operations);
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  }}>
    {fields.map(field => <label key={field.key} className="block">{field.label}
      <NumericInput aria-label={field.label} required step="any"
        min={field.operation.op === "set_origin" ? -1e9 : 0} max={field.operation.op === "set_origin" ? 1e9 : undefined}
        disabled={disabled || field.derived} value={draft[field.key] ?? String(field.value ?? "")}
        onChange={event => setDraft(previous => ({ ...previous, [field.key]: event.target.value }))}
        className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1" />
    </label>)}
    {notice && <p role="status">{notice}</p>}
    {error && <p role="alert" className="text-red">{error}</p>}
    <div className="sticky bottom-0 z-10 border-t border-hairline bg-chrome py-2">
      <p className="mb-1 text-ink-3">{dirty ? "Unsent changes. Preview before applying." : "Edit values, then preview the changes."}</p>
      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={disabled || !dirty || revision !== baseRevision}
          className="rounded border border-accent px-2 py-1 text-accent disabled:opacity-40">Preview changes</button>
        <button type="button" disabled={disabled || !dirty} onClick={reset} className="px-2 py-1 disabled:opacity-40">Reset</button>
      </div>
    </div>
  </form>;
}

function NumericField({ label, value, signed, disabled, submit }: {
  label: string; value: number | null; signed?: boolean; disabled: boolean; submit: (value: number) => void;
}) {
  const [error, setError] = useState("");
  const [text, setText] = useState(String(value ?? ""));
  useDraftGuard(text !== String(value ?? ""), () => { setText(String(value ?? "")); setError(""); });
  return <form className="my-1" onSubmit={event => {
    event.preventDefault();
    try { const number = readNumber(new FormData(event.currentTarget).get("value"), signed); setError(""); submit(number); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  }}>
    <div className="flex items-end gap-1">
      <label className="min-w-0 flex-1">{label}<NumericInput name="value" required step="any"
        min={signed ? -1e9 : 0} max={signed ? 1e9 : undefined} aria-label={label} value={text} onChange={event => setText(event.target.value)}
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
  const fields: FeatureEditField[] = dimensions.map(parameter => ({ key: parameter,
    label: `${title} ${parameter} (mm)`, value: parameters[parameter], derived: !!fieldBinding(ledger, feature.id, parameter).derived,
    operation: { op: "set_dimension", parameter } }));
  if (promoted && feature.placement.relative_to === null) capability.origin_axes.forEach(axis => fields.push({ key: axis,
    label: `${title} origin ${axis} (mm)`, value: feature.placement.origin[axis === "x" ? 0 : axis === "y" ? 1 : 2],
    derived: !!fieldBinding(ledger, feature.id, axis).derived, operation: { op: "set_origin", axis } }));
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
    {!feature.suppressed && promoted && <>
      {fields.map(field => <div key={field.key}>{bindingNote(field.key)}</div>)}
      <FeatureFields key={`${state.sessionId}:${feature.id}`} ledgerId={ledgerId} featureId={feature.id} fields={fields} disabled={disabled} />
      {feature.placement.relative_to !== null && <p>Position uses a relative placement; direct movement is unavailable.</p>}
    </>}
    {!feature.suppressed && !promoted && dimensions.map(parameter => <div key={`${state.sessionId}:${state.sessionRevision}:${parameter}`}>
      {bindingNote(parameter)}
      <NumericField label={`${title} ${parameter} (mm)`} value={parameters[parameter]} disabled={disabled || fieldBinding(ledger, feature.id, parameter).derived}
        submit={value => state.capabilities
          ? state.previewFeatureCommand(ledgerId, [{ op: "set_dimension", feature_id: feature.id, parameter, value }])
          : state.editParameter(ledgerId, feature.id, parameter, value)} />
    </div>)}
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
