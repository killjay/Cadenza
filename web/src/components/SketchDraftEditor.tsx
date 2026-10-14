import { useEffect } from "react";
import { useStore } from "../lib/store";
import type { DraftVariable } from "../lib/types";
import { SketchRelationships } from "./SketchRelationships";

const field = "w-full rounded border border-hairline bg-canvas p-1";
const button = "text-accent disabled:opacity-40";

export function SketchDraftEditor() {
  const state = useStore(), draft = state.draft;
  const selected = state.draftEntityId;
  const select = state.selectDraftEntity;
  useEffect(() => {
    if (!state.draftFocus) return;
    const element = document.getElementById("draft-" + state.draftFocus.field);
    if (!element) return;
    const details = element.closest("details");
    if (details) details.open = true;
    element.scrollIntoView({ block: "nearest" });
    const input = element.querySelector("input:not(:disabled)") as HTMLInputElement | null;
    (input ?? element).focus();
  }, [state.draftFocus]);
  useEffect(() => {
    if (!draft) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [!!draft]);
  if (!draft) {
    const ledgers = Object.values(state.ledgers);
    if (ledgers.length !== 1 || ledgers[0].assembly || ledgers[0].features.length) return null;
    return <div className="border-t border-hairline p-2 text-11">
      <button className={button} disabled={state.busy || !!state.preview || state.connection !== "open" || state.model?.source === "local"}
        onClick={() => state.startDraft()}>New sketch</button>
      {state.draftError && <p role="alert">{state.draftError}</p>}
    </div>;
  }
  if (state.preview) return null;
  const disabled = state.busy || state.connection !== "open";
  const entities = draft.solution?.entities ?? draft.definition.entities;
  const entity = entities.find(e => e.id === selected) ?? entities[0];
  const constraints = draft.definition.constraints;
  const variables: DraftVariable[] = entity?.kind === "rectangle" ? ["x", "y", "width", "height"] : ["x", "y", "diameter"];
  const linked = new Set(entity ? [entity.id] : []);
  for (let pass = 0; pass < entities.length; pass++) for (const c of constraints) {
    if (c.kind === "coincident" && (linked.has(c.entity_a) || linked.has(c.entity_b))) {
      linked.add(c.entity_a); linked.add(c.entity_b);
    }
  }
  const free = variables.filter(parameter => !constraints.some(c => c.kind === "dimension" && c.parameter === parameter
    && (parameter === "x" || parameter === "y" ? linked.has(c.entity_id) : c.entity_id === entity?.id)));
  const finishable = draft.solution?.status === "fully_constrained" && !draft.diagnostics.length
    && (!draft.is_new || draft.extrusion_amount !== null);
  return <section aria-label="Sketch draft" className="flex min-h-0 flex-1 flex-col border-t border-accent p-2.5 text-11">
    <h2 className="font-semibold text-accent">Sketch draft - not applied</h2>
    <p className="my-1 text-ink-3">The saved project remains at accepted revision {draft.base_revision}.</p>
    {state.busy && <p role="status" className="my-1 text-accent">{state.phase ?? "Updating sketch"}</p>}
    <p role="status" className="my-1">{draft.solution ? `${draft.solution.status.replaceAll("_", " ")} - ${draft.solution.degrees_of_freedom} freedoms remaining` : "Empty sketch"}</p>
    <div className="min-h-0 space-y-2 overflow-auto pr-1">
      {draft.diagnostics.map((message, i) => <p key={i} className="text-yellow">{message}</p>)}
      {!!draft.solution?.redundant.length && <p>Redundant constraints: {draft.solution.redundant.join(", ")}</p>}
      {state.draftError && <p role="alert" className="break-words text-red">{state.draftError}</p>}
      {!!draft.solution?.conflicts.length && <div className="space-y-1"><p>Conflicting constraints:</p>
        {draft.solution.conflicts.map(id => {
          const c = constraints.find(item => item.id === id);
          if (!c) return null;
          const owner = c.kind === "dimension" ? c.entity_id : c.entity_a;
          const label = entities.findIndex(e => e.id === owner);
          return <button key={id} className="block text-red" onClick={() => select(owner, `constraint-${id}`)}>
            Locate {label === 0 ? "Outer" : `Opening ${label}`} {c.kind === "dimension" ? `${c.parameter} = ${c.value} mm` : "shared centre"}
          </button>;
        })}
      </div>}
      {!!draft.solution?.free_parameters.length && <p className="break-words text-ink-3">
        Free variables: {draft.solution.free_parameters.map(variable => {
          const [id, parameter] = variable.split(".");
          const index = entities.findIndex(e => e.id === id);
          return `${index === 0 ? "Outer" : "Opening " + index}.${parameter}`;
        }).join(", ")}
      </p>}
      <div className="flex flex-wrap gap-3">
        {!entities.length ? <>
          <button className={button} disabled={disabled} onClick={() => state.updateDraft([{ op: "add_entity", entity: { kind: "rectangle", width: 100, height: 60 } }])}>Add rectangle</button>
          <button className={button} disabled={disabled} onClick={() => state.updateDraft([{ op: "add_entity", entity: { kind: "circle", diameter: 100 } }])}>Add outer circle</button>
        </> : <button className={button} disabled={disabled || entities.length >= 16}
          onClick={() => state.updateDraft([{ op: "add_entity", entity: { kind: "circle", operation: "subtract", diameter: 20 } }])}>Add opening</button>}
      </div>
      {entity && <>
        <label>Draft entity<select aria-label="Draft entity" className={field} disabled={disabled} value={entity.id} onChange={e => select(e.target.value)}>
          {entities.map((e, i) => <option key={e.id} value={e.id}>{i === 0 ? "Outer" : `Opening ${i}`} ({e.kind})</option>)}
        </select></label>
        <p className="break-all text-10 text-ink-4">{entity.id}</p>
        <button className={button} disabled={disabled} onClick={() => state.updateDraft([{ op: "remove_entity", entity_id: entity.id }])}>Remove entity</button>
        {!!free.length && <div><p>Free values (mm)</p>{free.map(parameter => <form id={`draft-seed-${parameter}`} tabIndex={-1} key={`${draft.revision}:${entity.id}:${parameter}`} className="my-1 flex items-end gap-2"
          onSubmit={event => { event.preventDefault(); state.updateDraft([{ op: "set_seed", entity_id: entity.id, parameter, value: Number(new FormData(event.currentTarget).get("value")) }]); }}>
          <label className="min-w-0 flex-1">{parameter}<input className={field} aria-label={`Free ${parameter}`} type="number" step="any" required name="value" defaultValue={entity[parameter] ?? 0} disabled={disabled} /></label>
          <button className={button} disabled={disabled}>Set</button>
        </form>)}</div>}
        <form key={`dimension:${entity.id}:${draft.revision}`} className="space-y-1 rounded border border-hairline p-2" onSubmit={event => {
          event.preventDefault(); const data = new FormData(event.currentTarget);
          state.updateDraft([{ op: "add_dimension", entity_id: entity.id, parameter: String(data.get("parameter")) as DraftVariable, value: Number(data.get("value")) }]);
        }}>
          <label>Dimension<select aria-label="New dimension variable" name="parameter" className={field} disabled={disabled}>
            {variables.map(p => <option key={p} value={p}>{p}</option>)}
          </select></label>
          <label>Value (mm)<input aria-label="New dimension value" className={field} name="value" type="number" step="any" defaultValue={0} required disabled={disabled} /></label>
          <button className={button} disabled={disabled}>Add dimension</button>
        </form>
        <button className={button} disabled={disabled || !free.length} onClick={() => state.updateDraft(free.map(parameter => ({
          op: "add_dimension", entity_id: entity.id, parameter, value: entity[parameter] ?? 0,
        })))}>Fix remaining values</button>
        {entities.length > 1 && <form className="space-y-1" onSubmit={event => {
          event.preventDefault(); state.updateDraft([{ op: "add_centre", entity_a: entity.id, entity_b: String(new FormData(event.currentTarget).get("other")) }]);
        }}><label>Share centre with<select aria-label="Shared centre entity" name="other" className={field} disabled={disabled}>
          {entities.filter(e => e.id !== entity.id).map(e => <option key={e.id} value={e.id}>{e.id === entities[0].id ? "Outer" : "Opening"} {e.id.slice(-6)}</option>)}
        </select></label><button className={button} disabled={disabled}>Add shared centre</button></form>}
      </>}
      <h3 className="font-semibold">Constraints</h3>
      {entity && <SketchRelationships key={entity.id} entityId={entity.id} />}
      {!constraints.length && <p>No constraints yet.</p>}
      {constraints.filter(c => c.kind === "dimension" ? c.entity_id === entity?.id : c.entity_a === entity?.id || c.entity_b === entity?.id).map(c => {
        const binding = draft.relationships.bindings.find(b => b.feature_id === draft.profile_id && b.constraint_id === c.id);
        const driver = binding && draft.relationships.parameters[binding.expression.trim()];
        const derived = !!binding && (!driver || driver.expression !== null);
        return <div id={`draft-constraint-${c.id}`} tabIndex={-1} key={`${c.id}:${draft.revision}`} className="space-y-1 rounded border border-hairline p-2">
          <p className="break-all text-10 text-ink-4">{c.id}</p>
          {binding && <p className="break-words">Driven by {binding.expression}. Editing this driver can affect other features.</p>}
          {c.kind === "dimension" ? <form className="flex items-end gap-2" onSubmit={event => {
            event.preventDefault(); state.updateDraft([{ op: "set_dimension", constraint_id: c.id, value: Number(new FormData(event.currentTarget).get("value")) }]);
          }}><label className="min-w-0 flex-1">{c.parameter} (mm)<input aria-label={`Draft dimension ${c.id}`} className={field} name="value" type="number" step="any" required defaultValue={c.value} disabled={disabled || derived} /></label>
            <button className={button} disabled={disabled || derived}>Update</button></form> : <p className="break-all">Shared centre: {c.entity_a} = {c.entity_b}</p>}
          <button aria-label={`Remove ${c.id}`} className={button} disabled={disabled || !!binding}
            title={binding ? "Bound dimensions cannot be removed; edit the driver" : undefined}
            onClick={() => state.updateDraft([{ op: "remove_constraint", constraint_id: c.id }])}>Remove constraint</button>
        </div>;
      })}
      {!!Object.keys(draft.relationships.parameters).length && <details><summary>Named drivers</summary>
        <p>Changing a driver may affect other dimensions and features in the review.</p>
        {Object.entries(draft.relationships.parameters).map(([name, driver]) => <form key={`${draft.revision}:${name}`} className="my-1 flex items-end gap-2" onSubmit={event => {
          event.preventDefault(); state.updateDraft([{ op: "set_driver", parameter: name, value: Number(new FormData(event.currentTarget).get("value")) }]);
        }}><label className="min-w-0 flex-1">{name} ({driver.unit}){driver.expression ? <span className="block">Derived: {driver.expression}</span>
          : <input aria-label={`Draft driver ${name}`} className={field} type="number" step="any" required name="value" defaultValue={driver.value ?? 0} disabled={disabled} />}</label>
          {!driver.expression && <button className={button} disabled={disabled}>Update driver</button>}
        </form>)}
      </details>}
      {draft.is_new && <form key={`extrusion:${draft.revision}`} className="flex items-end gap-2" onSubmit={event => {
        event.preventDefault(); state.updateDraft([{ op: "set_extrusion", amount: Number(new FormData(event.currentTarget).get("value")) }]);
      }}><label className="min-w-0 flex-1">Extrusion amount (mm)<input aria-label="Draft extrusion amount" className={field} name="value" type="number" min={0.00001} max={1e6} step="any" required defaultValue={draft.extrusion_amount ?? ""} disabled={disabled} /></label>
        <button className={button} disabled={disabled}>Set extrusion</button></form>}
    </div>
    <div className="mt-2 flex shrink-0 gap-4 border-t border-hairline pt-2">
      <button className={button} disabled={disabled || !finishable} onClick={() => state.actOnDraft("finish")}>Finish sketch</button>
      <button disabled={disabled} onClick={() => state.actOnDraft("discard")}>Discard draft</button>
    </div>
  </section>;
}
