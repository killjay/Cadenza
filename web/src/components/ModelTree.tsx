import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useStore } from "../lib/store";
import { partUsage } from "../lib/assembly";
import { requestNumericDraftDiscard } from "../lib/featureCommands";
import { PartsWorkspace } from "./PartsWorkspace";
import { AssemblyPartPicker } from "./AssemblyPartPicker";
import type { PartPickerTarget } from "./AssemblyPartPicker";
import type { Inspection } from "./PropertiesPanel";
import type { Ledger } from "../lib/types";

export function ModelTree({ inspection, inspect, inspectAssembly }: {
  inspection: Inspection | null; inspect: (target: Inspection) => void; inspectAssembly: (path?: string) => void;
}) {
  const state = useStore();
  const [picker, setPicker] = useState<PartPickerTarget | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const { rows, unplaced } = partUsage(state.ledgers, state.primaryLedgerId ?? "");
  const primary = state.primaryLedgerId ? state.ledgers[state.primaryLedgerId] : null;
  const blocked = state.busy || !!state.preview || !!state.draft || state.draftStarting || state.sourceLoading
    || !!state.sourceRequest || state.connection !== "open" || state.model?.source === "local";
  const openPicker = (target: PartPickerTarget) => {
    if (blocked || !requestNumericDraftDiscard(window, true)) return;
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setPicker(target);
  };
  const closePicker = () => { setPicker(null); requestAnimationFrame(() => opener.current?.focus()); };
  useEffect(() => {
    const add = (event: Event) => openPicker((event as CustomEvent<PartPickerTarget>).detail);
    const create = () => { if (primary?.assembly) openPicker({ ownerId: primary.project_id, ownerPath: [], create: true }); };
    window.addEventListener("cadenza:add-part", add);
    window.addEventListener("cadenza:new-part", create);
    return () => { window.removeEventListener("cadenza:add-part", add); window.removeEventListener("cadenza:new-part", create); };
  });
  useEffect(() => { setPicker(null); }, [state.sessionId]);
  const features = (source: Ledger) => <div className="my-1 ml-2 border-l border-hairline pl-2">
    <button className="my-1 text-accent" disabled={blocked} aria-pressed={inspection?.ledgerId === source.project_id && !inspection.featureId}
      onClick={() => inspect({ ledgerId: source.project_id })}>Properties: {source.metadata.name || "Part"}</button>
    {inspection?.ledgerId === source.project_id && source.features.map(feature => <button key={feature.id} disabled={blocked}
      aria-pressed={inspection.featureId === feature.id}
      className="my-1 block w-full rounded px-2 py-1 text-left text-11 aria-pressed:bg-wash-2 aria-pressed:text-accent disabled:opacity-40"
      onClick={() => inspect({ ledgerId: source.project_id, featureId: feature.id })}>
      {feature.name || feature.kind}{feature.suppressed ? " (suppressed)" : ""}</button>)}
  </div>;
  const branches = (parent: string): ReactNode => <ul className={parent ? "ml-3 border-l border-hairline pl-2" : "space-y-1"}>
    {rows.filter(row => row.ownerPath.join("/") === parent).map(row => {
      const active = state.selectedSourceId === row.source?.project_id
        && (!state.partCapabilities?.source_context || state.selectedSourcePath?.join("/") === row.path);
      const button = <button disabled={blocked || (!row.source?.assembly && !!row.source && !state.partCapabilities)}
        aria-label={`Select ${row.component.name || "part"}`} aria-pressed={row.source?.assembly ? state.assemblySelection === row.path : active}
        className="min-w-0 flex-1 break-words rounded px-2 py-1 text-left aria-pressed:bg-wash-2 aria-pressed:text-accent disabled:opacity-40"
        onClick={() => row.source && !row.source.assembly
          ? state.selectSource(active ? null : row.source.project_id, false, row.path.split("/"))
          : inspectAssembly(row.path)}>{row.component.name || "Instance"}</button>;
      return <li key={row.path}>{row.source?.assembly ? <details>
        <summary className="cursor-pointer break-words">{row.component.name || "Subassembly"}</summary>
        <div className="flex">{button}</div>{branches(row.path)}
      </details> : <div className="flex items-center gap-1">{button}</div>}</li>;
    })}
  </ul>;
  return <div className="flex min-h-0 flex-1 flex-col">
    <div className="flex items-center justify-between gap-2 border-b border-hairline p-2 text-12">
      <button className="min-w-0 break-words text-left font-semibold" disabled={blocked}
        onClick={() => {
          if (!primary) return;
          if (!primary.assembly) { inspect({ ledgerId: primary.project_id }); return; }
          if (!state.selectedSourceId && state.assemblyOwnerId !== primary.project_id) {
            state.editAssemblyOwner(primary.project_id, []);
            if (useStore.getState().assemblyOwnerId !== primary.project_id) return;
          }
          inspectAssembly();
        }}>
        {primary?.metadata.name || "Project"}</button>
      <button className="shrink-0 text-accent disabled:opacity-40" disabled={blocked || (!state.assemblyCapabilities && !state.selectedSourceId) || !primary}
        onClick={() => primary && openPicker({ ownerId: primary.project_id, ownerPath: [] })}>+ Add part</button>
    </div>
    <div className="min-h-0 flex-1 overflow-auto p-2 text-11" aria-label="Project structure">
      {primary?.assembly ? <>
        {branches("")}
        {state.selectedSourceId && <div className="my-2 border-t border-hairline pt-2">
          <button disabled={blocked} className="text-accent disabled:opacity-40" onClick={() => state.selectSource(null)}>Project view</button>
          {state.selectedSourcePath && <button disabled={blocked} className="ml-3 text-accent disabled:opacity-40"
            onClick={() => inspectAssembly(state.selectedSourcePath!.join("/"))}>Placement and attachments</button>}
          <p>Select faces or features to edit. Properties shows whether geometry is shared.</p>
          {state.sourceView?.context_glb_url && !state.sourceContextWarning && <label className="mt-1 block">
            <input type="checkbox" checked={state.showContext} onChange={state.toggleContext} /> Show faded assembly</label>}
        </div>}
        {inspection && state.ledgers[inspection.ledgerId] && !state.ledgers[inspection.ledgerId].assembly
          && <details open className="my-2"><summary>Part features</summary>{features(state.ledgers[inspection.ledgerId])}</details>}
        <details className="mt-3 border-t border-hairline pt-2"><summary className="cursor-pointer">Unplaced parts ({unplaced.length})</summary>
          {!unplaced.length && <p className="my-2 text-ink-3">All source parts are placed in this assembly.</p>}
          {unplaced.map(source => <div key={source.project_id} className="my-2 space-y-1">
            <button disabled={blocked || !state.partCapabilities} aria-pressed={state.selectedSourceId === source.project_id}
              className="block break-words text-left text-accent disabled:opacity-40"
              onClick={() => state.selectSource(state.selectedSourceId === source.project_id ? null : source.project_id)}>{source.metadata.name}</button>
            <button disabled={blocked} className="text-accent disabled:opacity-40" aria-label={`Add ${source.metadata.name} to assembly`}
              onClick={() => openPicker({ ownerId: primary.project_id, ownerPath: [], sourceId: source.project_id })}>+ Add to assembly</button>
          </div>)}
        </details>
      </> : <>
        <PartsWorkspace renderFeatures={features} />
        {primary && !primary.features.length && <button className="text-accent" disabled={blocked} onClick={() => inspect({ ledgerId: primary.project_id })}>Project part properties</button>}
        <button className="my-2 text-accent" disabled={blocked} onClick={() => inspectAssembly()}>Assembly properties</button>
        {!state.partCapabilities && Object.values(state.ledgers).filter(ledger => !ledger.assembly).map(source => <div key={source.project_id}>{features(source)}</div>)}
      </>}
      {state.sourceLoading && <p role="status">Loading selected part?</p>}
      {state.sourceError && <p role="alert" className="text-red">{state.sourceError}</p>}
      {state.selectedSourceId && state.sourceContextWarning && <p role="status">{state.sourceContextWarning}</p>}
    </div>
    <AssemblyPartPicker target={picker} close={closePicker} />
  </div>;
}
