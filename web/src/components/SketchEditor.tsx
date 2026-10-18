import { NumericInput } from "./NumericInput";
import { useState, useEffect, useRef } from "react";
import { useStore } from "../lib/store";
import type { ProfileFeature } from "../lib/types";

export function SketchEditor({ ledgerId, feature }: { ledgerId: string; feature: ProfileFeature }) {
  const state = useStore();
  const [selected, select] = useState(feature.parameters.entities[0].id);
  const panel = useRef<HTMLDetailsElement>(null);
  const selection = state.selections.length === 1 ? state.selections[0] : undefined;
  const identity = selection?.surface_reference ?? (selection?.saved_reference
    ? state.ledgers[selection.saved_reference.ledger_id]?.surface_references?.[selection.saved_reference.reference_id] : undefined);
  const selectedProfile = !selection?.selection_error && identity?.source_ledger_id === ledgerId && !identity.source_part_id && identity.profile_id === feature.id;
  useEffect(() => {
    if (!selectedProfile) return;
    if (identity?.entity_id) select(identity.entity_id);
    if (panel.current) { panel.current.open = true; panel.current.scrollIntoView({ block: "nearest" }); }
  }, [selectedProfile, identity]);
  const solution = feature.parameters.solution;
  if (!solution || feature.suppressed) return null;
  const entity = solution.entities.find(e => e.id === selected) ?? solution.entities[0];
  const outer = solution.entities[0];
  const width = outer.width ?? outer.diameter ?? 1, height = outer.height ?? outer.diameter ?? 1;
  const pad = Math.max(width, height) * 0.1;
  const disabled = state.busy || !!state.draft || !!state.preview || state.connection !== "open" || state.model?.source === "local";
  const relationships = state.ledgers[ledgerId]?.relationships;
  return <details ref={panel} className="mt-1 text-11">
    <summary className="cursor-pointer text-accent">Edit sketch</summary>
    {Object.keys(state.ledgers).length === 1 && !state.ledgers[ledgerId]?.assembly && <button
      className="my-2 text-accent disabled:opacity-40" disabled={disabled || !!state.selectedSourceId}
      onClick={() => state.startDraft(feature.id)}>Edit as draft</button>}
    {selectedProfile && <p className="my-1 text-accent">{identity?.entity_id
      ? `Selected ${identity.entity_id} · ${identity.role.replaceAll("_", " ")}`
      : "Extrusion cap selected. Choose a sketch entity below, or edit extrusion amount."}</p>}
    {selectedProfile && !!identity?.component_path.length && <p className="text-yellow">Editing these source dimensions changes every instance of this part.</p>}
    <p className="my-1" role="status">{solution.status === "fully_constrained" ? "Fully constrained"
      : solution.status === "conflicting" ? "Conflicting constraints" : `Underconstrained: ${solution.degrees_of_freedom} freedoms remaining`}</p>
    {!!solution.conflicts.length && <p className="text-red">Conflicts: {solution.conflicts.join(", ")}</p>}
    {!!solution.free_parameters.length && <p className="break-words text-ink-3">Still movable: {solution.free_parameters.join(", ")}</p>}
    {!!solution.redundant.length && <p className="break-words text-ink-3">Redundant: {solution.redundant.join(", ")}</p>}
    <svg role="img" aria-label="Accepted sketch profile" className="my-2 h-36 w-full rounded bg-canvas"
      viewBox={`${outer.x - width / 2 - pad} ${-outer.y - height / 2 - pad} ${width + 2 * pad} ${height + 2 * pad}`}>
      <g transform="scale(1 -1)">
        {solution.entities.map(e => {
          const props = { fill: e.operation === "subtract" ? "#111418" : "#475569", stroke: e.id === entity.id ? "#38bdf8" : "#cbd5e1",
            strokeWidth: Math.max(width, height) / 160 };
          return e.kind === "rectangle" ? <rect key={e.id} {...props} x={e.x - e.width! / 2} y={e.y - e.height! / 2} width={e.width!} height={e.height!} />
            : <circle key={e.id} {...props} cx={e.x} cy={e.y} r={e.diameter! / 2} />;
        })}
      </g>
    </svg>
    <label>Sketch entity<select aria-label={`${feature.name || "Sketch"} entity`} value={entity.id} disabled={disabled}
      onChange={event => select(event.target.value)} className="my-1 w-full rounded bg-canvas p-1">
      {solution.entities.map(e => <option key={e.id} value={e.id}>{e.id} ({e.kind})</option>)}
    </select></label>
    <p className="text-10 text-ink-3">Centre ({entity.x}, {entity.y}) mm · {entity.kind === "rectangle"
      ? `${entity.width} × ${entity.height} mm` : `Diameter ${entity.diameter} mm`}</p>
    {feature.parameters.constraints.map(constraint => {
      if (constraint.kind === "coincident") return <p key={constraint.id} className="my-1 break-words text-10 text-ink-3">
        Shared centre: {constraint.entity_a} = {constraint.entity_b}</p>;
      if (constraint.entity_id !== entity.id) return null;
      const binding = relationships?.bindings.find(b => b.feature_id === feature.id && b.constraint_id === constraint.id);
      const driver = binding && relationships?.parameters[binding.expression.trim()];
      const derived = !!binding && (!driver || driver.expression !== null);
      return <form key={`${state.sessionRevision}:${constraint.id}`} className="my-1 flex items-end gap-1" onSubmit={event => {
        event.preventDefault();
        state.editSketchDimension(ledgerId, feature.id, constraint.id, Number(new FormData(event.currentTarget).get("value")));
      }}>
        <label className="min-w-0 flex-1">{constraint.parameter} (mm)
          {binding && <span className="block break-words text-10 text-ink-3">Driven by {binding.expression}</span>}
          <NumericInput name="value" required step="any" min={constraint.parameter === "x" || constraint.parameter === "y" ? -1e6 : 0.000001}
            max={1e6} defaultValue={constraint.value} disabled={disabled || derived}
            aria-label={`${feature.name || "Sketch"} ${entity.id} ${constraint.parameter} (mm)`}
            className="mt-0.5 w-full rounded border border-hairline bg-canvas p-1" />
        </label><button disabled={disabled || derived} className="p-1 text-accent disabled:opacity-40">Preview</button>
      </form>;
    })}
    <p className="mt-2 text-10 text-ink-4">Accepted sketch in its local plane. Use prompts to add constraints; edit named drivers for derived dimensions.</p>
  </details>;
}
