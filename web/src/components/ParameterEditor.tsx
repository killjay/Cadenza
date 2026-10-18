import { NumericInput } from "./NumericInput";
import { useStore } from "../lib/store";
import type { Ledger } from "../lib/types";

export function ParameterEditor({ ledger }: { ledger: Ledger }) {
  const state = useStore();
  const relationships = ledger.relationships;
  if (!relationships || !Object.keys(relationships.parameters).length) return null;
  const disabled = state.busy || !!state.draft || !!state.preview || state.connection !== "open" || state.model?.source === "local";
  return <details className="mb-2 rounded border border-hairline p-2 text-11" open>
    <summary className="cursor-pointer font-medium">Parameters</summary>
    <p className="my-1 text-10 text-ink-3">Driving values update dependent dimensions and positions. Review the preview before Apply.</p>
    {Object.entries(relationships.parameters).map(([name, parameter]) => parameter.expression !== null
      ? <p key={name} className="my-1 break-words text-ink-3">{name} = {parameter.expression} ({parameter.unit})</p>
      : <form key={`${state.sessionRevision}:${name}`} className="my-1 flex items-end gap-1" onSubmit={event => {
          event.preventDefault();
          state.editNamedParameter(ledger.project_id, name, Number(new FormData(event.currentTarget).get("value")));
        }}>
        <label className="min-w-0 flex-1 break-words">{name} ({parameter.unit})
          <NumericInput name="value" required min={-1e9} max={1e9} step="any"
            aria-label={`${name} (${parameter.unit})`} defaultValue={parameter.value ?? ""} disabled={disabled}
            className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1" />
        </label>
        <button disabled={disabled} className="p-1 text-accent disabled:opacity-40">Preview</button>
      </form>)}
    {!!relationships.bindings.length && <details className="mt-2 text-10 text-ink-3">
      <summary className="cursor-pointer">Relationships ({relationships.bindings.length})</summary>
      {relationships.bindings.map(binding => <p key={`${binding.feature_id}:${binding.target}:${binding.constraint_id ?? ""}`} className="my-1 break-words">
        {ledger.features.find(f => f.id === binding.feature_id)?.name || binding.feature_id}.{binding.constraint_id || binding.target} = {binding.expression}
      </p>)}
      {relationships.constraints.map((constraint, i) => <p key={i} className="my-1 break-words">
        {constraint.message}: {constraint.left} {constraint.operator} {constraint.right}
      </p>)}
    </details>}
    <p className="mt-2 text-10 text-ink-4">Use a prompt to add or change formulas. Expressions are read-only here.</p>
  </details>;
}
