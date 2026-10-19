import { useStore } from "../lib/store";
import { AssemblyEditor } from "./AssemblyEditor";
import { FeatureEditor } from "./FeatureEditor";
import { FeatureCreate } from "./FeatureCreate";
import { PatternControls } from "./PatternControls";
import { ParameterEditor } from "./ParameterEditor";
import { SketchEditor } from "./SketchEditor";
import { SavedSurfaces } from "./SavedSurfaces";
import { SourceRename } from "./SourceRename";
import { MakeIndependent, IndependentResult } from "./MakeIndependent";
import { partUsage } from "../lib/assembly";
import { OccurrenceProperties } from "./OccurrenceProperties";

export interface Inspection { ledgerId: string; featureId?: string; }

export function PropertiesPanel({ inspection, assembly }: { inspection: Inspection | null; assembly: boolean }) {
  const state = useStore();
  const { uses, rows } = partUsage(state.ledgers, state.primaryLedgerId ?? "");
  const occurrence = rows.find(row => row.path === state.selectedSourcePath?.join("/"));
  const blocked = state.busy || !!state.preview || !!state.draft || state.draftStarting
    || state.connection !== "open" || state.model?.source === "local" || state.sourceLoading;
  const promptDisabled = blocked || !!state.faceAttachment || !state.featurePromptAvailable || !state.capabilities || !state.agentsAvailable;
  if (state.faceAttachment) return <p className="p-3 text-11">Finish or cancel Attach faces in the viewport before editing this part.</p>;
  return <div className="min-h-0 flex-1 overflow-y-auto p-3 text-11" aria-label="Model properties">
    <IndependentResult />
    {!assembly && occurrence && inspection?.ledgerId === occurrence.source?.project_id
      && <OccurrenceProperties path={occurrence.path} />}
    {!!state.selections.length && <details open className="mb-3 rounded border border-hairline p-2">
      <summary>Selected surface</summary>
      {state.selections.map((selection, index) => <div key={index} className="mt-2 break-words">
        {selection.source && <p className="text-ink-3">Coordinates relative to this part.</p>}
        <p>{selection.label || selection.info?.surfaceType || "Surface"}</p>
        <p>Position (mm): {selection.point.map(value => value.toFixed(1)).join(", ")}</p>
        <p>Normal: {selection.normal.map(value => value.toFixed(1)).join(", ")}</p>
        {selection.info?.radius != null && <p>Radius: {selection.info.radius.toFixed(2)} mm</p>}
      </div>)}
      {state.selections.length === 1 && state.selections[0]?.source && <div className="mt-2 space-y-2">
        <p className="text-ink-3">Face actions edit this source and all its reused instances. Review the proposal before Apply.</p>
        <button className="mr-3 text-accent disabled:opacity-40" disabled={promptDisabled || !state.sourceFaceAvailable
          || state.selections[0].mode !== "face" || !state.selections[0].surface_reference || !!state.selections[0].selection_error}
          onClick={() => state.startFacePrompt("edit_feature")}>Edit this feature</button>
        <button className="text-accent disabled:opacity-40" disabled={promptDisabled || !state.sourceFaceAvailable
          || state.selections[0].mode !== "face" || !state.selections[0].surface_reference || !!state.selections[0].selection_error}
          onClick={() => state.startFacePrompt("add_on_face")}>Add on this face</button>
        {!state.sourceFaceAvailable && <p>Face editing is unavailable on this server.</p>}
        {state.selections[0].selection_error && <p>{state.selections[0].selection_error}</p>}
        {(!state.selections[0].surface_reference || state.selections[0].mode !== "face") && !state.selections[0].selection_error
          && <p>Select one supported face, or use the feature controls below.</p>}
      </div>}
    </details>}
    <div hidden={!assembly}><AssemblyEditor showTree={false} /></div>
    {!assembly && !inspection && <p className="text-ink-3">Choose a part or feature in the model tree to inspect it.</p>}
    {Object.values(state.ledgers).filter(ledger => !ledger.assembly).map(ledger => <section
      key={`${state.sessionId}:${ledger.project_id}`} hidden={assembly || inspection?.ledgerId !== ledger.project_id}>
      <h2 className="mb-2 break-words text-13 font-semibold">Geometry: {ledger.metadata.name || "Untitled part"}</h2>
      {state.ledgers[state.primaryLedgerId ?? ""]?.assembly && <p className="mb-2 text-ink-3">
        {(uses.get(ledger.project_id) ?? 0) > 0 ? `Used in ${uses.get(ledger.project_id)} placed instance(s).` : "Not placed in the project assembly."}
      </p>}
      <p className="mb-2 text-ink-3">{(uses.get(ledger.project_id) ?? 0) > 1
        ? "Shared geometry: edits affect every listed use. Choose Make independent to edit only the selected part."
        : "Geometry edits affect this part and any future uses of this source."}</p>
      {(uses.get(ledger.project_id) ?? 0) > 1 && <details className="mb-2"><summary>Parts sharing this geometry ({uses.get(ledger.project_id)})</summary>
        <ul>{rows.filter(row => row.source?.project_id === ledger.project_id).map(row => <li key={row.path}>{row.labels.join(" / ")}</li>)}</ul>
      </details>}
      {state.selectedSourceId === ledger.project_id && state.ledgers[state.primaryLedgerId ?? ""]?.assembly
        && <MakeIndependent path={state.selectedSourcePath?.join("/") ?? null} />}
      <fieldset disabled={state.sourceLoading}>
        <div hidden={!!inspection?.featureId}>
          {state.partCapabilities?.rename_source && ledger.features.length > 0 && <details className="mb-2">
            <summary>Source name (all uses)</summary><SourceRename key={`${state.sessionRevision}:${ledger.project_id}`} source={ledger} disabled={blocked} />
          </details>}
          <ParameterEditor ledger={ledger} />
          <button disabled={promptDisabled} className="my-2 text-accent disabled:opacity-40"
            onClick={() => state.startFeaturePrompt(ledger.project_id)}>Add with AI</button>
          <FeatureCreate key={`${state.sessionId}:${state.sessionRevision}:${ledger.project_id}`} ledger={ledger} />
          <PatternControls key={`pattern:${state.sessionId}:${state.sessionRevision}:${ledger.project_id}`} ledger={ledger} />
          {!ledger.features.length && <p className="my-2 text-ink-3">No features yet.</p>}
        </div>
        {ledger.features.map(feature => <div key={feature.id} hidden={inspection?.featureId !== feature.id}
          id={`feature-${ledger.project_id}-${feature.id}`} className="mt-2 rounded border border-hairline p-2">
          <h3 className="font-semibold">{feature.name || feature.kind}{feature.suppressed ? " (suppressed)" : ""}</h3>
          <FeatureEditor ledgerId={ledger.project_id} feature={feature} />
          {feature.kind === "profile" && <SketchEditor ledgerId={ledger.project_id} feature={feature} />}
        </div>)}
      </fieldset>
    </section>)}
    {!state.selectedSourceId && <details className="mt-4 border-t border-hairline pt-2"><summary>Saved surfaces</summary><SavedSurfaces /></details>}
    <details className="mt-4 border-t border-hairline pt-2"><summary>AI context</summary>
      <p className="my-2 text-ink-3">Sources available to general modelling. These checkboxes do not change the displayed model.</p>
      {Object.values(state.ledgers).map(ledger => <label key={ledger.project_id} className="block break-words">
        <input type="checkbox" checked={state.activeLedgerIds.includes(ledger.project_id)} onChange={event =>
          state.setActiveLedgerIds(event.target.checked ? [...state.activeLedgerIds, ledger.project_id]
            : state.activeLedgerIds.filter(id => id !== ledger.project_id))} /> {ledger.metadata.name || "Untitled part"}
      </label>)}
    </details>
    {state.kernel.state !== "ready" && <p className="mt-3 text-ink-4">{state.kernel.state === "failed" ? state.kernel.error : `Geometry viewer: ${state.kernel.state}`}</p>}
  </div>;
}
