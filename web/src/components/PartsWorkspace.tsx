import { useState } from "react";
import { useStore } from "../lib/store";

export function PartsWorkspace() {
  const state = useStore();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"box" | "cylinder">("box");
  if (!state.partCapabilities) return null;
  const blocked = state.busy || !!state.preview || !!state.draft || state.draftStarting
    || state.connection !== "open" || state.model?.source === "local";
  const sources = Object.values(state.ledgers).filter(l => !l.assembly && (l.features.length || l.project_id !== state.primaryLedgerId));
  return <section className="mb-3 rounded border border-hairline p-2 text-11" aria-label="Parts workspace">
    <div className="mb-2 font-semibold">Parts workspace</div>
    <button className="text-accent disabled:opacity-40" disabled={blocked} onClick={() => state.selectSource(null)}>
      Project view{state.primaryLedgerId && !state.ledgers[state.primaryLedgerId]?.features.length
        && !state.ledgers[state.primaryLedgerId]?.assembly ? " (empty)" : ""}
    </button>
    <div className="my-2 flex flex-col gap-1">
      {sources.map(source => <button key={source.project_id} disabled={blocked}
        aria-pressed={state.selectedSourceId === source.project_id}
        className="rounded border border-hairline px-2 py-1 text-left disabled:opacity-40 aria-pressed:border-accent"
        onClick={() => state.selectSource(source.project_id)}>
        View {source.metadata.name} <span className="text-ink-4">({source.project_id.slice(-6)})</span>
      </button>)}
    </div>
    {state.selectedSourceId && <p className="my-1 text-ink-3">Source-local view: {state.ledgers[state.selectedSourceId]?.metadata.name}.
      Edit features below; source changes affect all instances. Surface selection and sketch workspace require Project view.</p>}
    {state.sourceLoading && <p role="status">Loading selected part…</p>}
    {state.sourceError && <p role="alert" className="text-red">{state.sourceError}</p>}
    <details className="mt-2">
      <summary>New Part</summary>
      <form className="mt-2 space-y-2" onSubmit={event => {
        event.preventDefault();
        const data = new FormData(event.currentTarget);
        const placement = { origin: [0, 0, 0] as [number, number, number], rotation_deg: [0, 0, 0] as [number, number, number], relative_to: null };
        const height = Number(data.get("height"));
        state.createPart(name, [kind === "box"
          ? { op: "add_feature", kind, name: "Stock", operation: "add", placement,
            parameters: { length: Number(data.get("length")), width: Number(data.get("width")), height } }
          : { op: "add_feature", kind, name: "Stock", operation: "add", placement,
            parameters: { diameter: Number(data.get("diameter")), height } }]);
      }}>
        <fieldset disabled={blocked || state.sourceLoading} className="space-y-2">
          <label className="block">Part name <input aria-label="New part name" required maxLength={100} value={name}
            onChange={e => setName(e.target.value)} className="w-full rounded border border-hairline bg-chrome p-1" /></label>
          <label className="block">Starting shape <select aria-label="Starting shape" value={kind}
            onChange={e => setKind(e.target.value as "box" | "cylinder")} className="ml-2 bg-chrome">
            <option value="box">Box</option><option value="cylinder">Cylinder</option>
          </select></label>
          {(kind === "box" ? ["length", "width", "height"] : ["diameter", "height"]).map(field =>
            <label key={field} className="block">{field} (mm) <input aria-label={`New part ${field}`} name={field}
              type="number" min="0.000001" step="any" required defaultValue={field === "height" ? 5 : 30}
              className="w-full rounded border border-hairline bg-chrome p-1" /></label>)}
          <p className="text-ink-4">Centred in X/Y, base at Z=0. Position can be edited after Apply.</p>
          <button className="text-accent disabled:opacity-40" type="submit" disabled={!name.trim()}>Preview new part</button>
          <button className="ml-3 text-accent disabled:opacity-40" type="button" disabled={!name.trim() || !state.agentsAvailable}
            onClick={() => state.startPartPrompt(name)}>Create with AI</button>
        </fieldset>
      </form>
    </details>
  </section>;
}
