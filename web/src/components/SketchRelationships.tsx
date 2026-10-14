import { useState } from "react";
import { useStore } from "../lib/store";
import { edgeMarginOperations } from "../lib/sketchRelationships";

const field = "w-full rounded border border-hairline bg-canvas p-1";
const button = "text-accent disabled:opacity-40";

export function SketchRelationships({ entityId }: { entityId: string }) {
  const state = useStore(), draft = state.draft!;
  const [error, setError] = useState("");
  const disabled = state.busy || state.connection !== "open" || !!state.preview;
  const dimensions = draft.definition.constraints.filter(c => c.kind === "dimension" && c.entity_id === entityId
    && !draft.relationships.bindings.some(b => b.feature_id === draft.profile_id && b.constraint_id === c.id));
  const entities = draft.solution?.entities ?? draft.definition.entities;
  const canMargin = entities[0]?.kind === "rectangle" && entities.some(e => e.id === entityId && e.operation === "subtract");
  return <details className="space-y-2 rounded border border-hairline p-2">
    <summary>Dimension relationships</summary>
    <p>Relationships persist after Apply. Shared drivers can affect other dimensions and features.</p>
    {error && <p role="alert" className="text-red">{error}</p>}
    {!!dimensions.length && <>
      <form className="space-y-1" onSubmit={event => {
        event.preventDefault(); setError(""); const data = new FormData(event.currentTarget);
        const constraint = dimensions.find(c => c.id === data.get("dimension"));
        if (constraint?.kind !== "dimension") return;
        const name = String(data.get("name"));
        state.updateDraft([{ op: "add_driver", parameter: name, value: constraint.value },
          { op: "bind_dimension", constraint_id: constraint.id, expression: name }]);
      }}>
        <label>Name a driving dimension<select aria-label="Dimension to name" className={field} name="dimension" disabled={disabled}>
          {dimensions.map(c => <option key={c.id} value={c.id}>{c.kind === "dimension" ? c.parameter : ""} ({c.id.slice(-6)})</option>)}
        </select></label>
        <label>New driver name<input aria-label="New dimension driver name" className={field} name="name" required pattern="[a-z][a-z0-9_]{0,47}" maxLength={48} disabled={disabled} /></label>
        <button className={button} disabled={disabled}>Name dimension</button>
      </form>
      <form className="space-y-1" onSubmit={event => {
        event.preventDefault(); setError(""); const data = new FormData(event.currentTarget);
        state.updateDraft([{ op: "bind_dimension", constraint_id: String(data.get("dimension")), expression: String(data.get("expression")) }]);
      }}>
        <label>Bind a dimension<select aria-label="Dimension to bind" className={field} name="dimension" disabled={disabled}>
          {dimensions.map(c => <option key={c.id} value={c.id}>{c.kind === "dimension" ? c.parameter : ""} ({c.id.slice(-6)})</option>)}
        </select></label>
        <label>Expression (mm)<input aria-label="Dimension expression" className={field} name="expression" required maxLength={256} placeholder="plate_width / 2" disabled={disabled} /></label>
        <p>Use named drivers, parentheses and + − * /. Bound dimensions cannot be detached here.</p>
        <button className={button} disabled={disabled}>Bind dimension</button>
      </form>
    </>}
    {canMargin && <form className="space-y-1" onSubmit={event => {
      event.preventDefault(); const data = new FormData(event.currentTarget);
      try {
        const operations = edgeMarginOperations(draft, entityId,
          String(data.get("edge")) as "left" | "right" | "top" | "bottom", String(data.get("name")), Number(data.get("distance")));
        setError(""); state.updateDraft(operations);
      } catch (cause) { setError((cause as Error).message); }
    }}>
      <p>Opening centre to edge: the other coordinate stays centred on the outer rectangle. This measures to the centre, not the opening rim.</p>
      <label>Outer edge<select aria-label="Margin edge" className={field} name="edge" disabled={disabled}>
        {(["left", "right", "bottom", "top"] as const).map(edge => <option key={edge}>{edge}</option>)}
      </select></label>
      <label>Distance (mm)<input aria-label="Centre-to-edge distance" className={field} name="distance" type="number" step="any" min={0.000001} max={1e6} defaultValue={15} required disabled={disabled} /></label>
      <label>New margin driver<input aria-label="Margin driver name" className={field} name="name" pattern="[a-z][a-z0-9_]{0,47}" maxLength={48} required disabled={disabled} /></label>
      <button className={button} disabled={disabled}>Keep centre distance</button>
    </form>}
    {!dimensions.length && !canMargin && <p>Add an unbound dimension to author a relationship.</p>}
  </details>;
}
