import { useState } from "react";
import { useStore } from "../lib/store";
import { assemblyRows } from "../lib/assembly";
import type { AssemblyOperation } from "../lib/types";

import { PlacementFields, readPlacement, readName, inputClass, buttonClass, zero } from "./AssemblyFields";
import { AssemblyJointEditor } from "./AssemblyJointEditor";
import { SubassemblyCreate } from "./SubassemblyCreate";

export function AssemblyEditor() {
  const state = useStore();
  const [error, setError] = useState<string | null>(null);
  const cap = state.assemblyCapabilities;
  if (!cap) return null;
  const owner = state.ledgers[cap.ledger_id], assembly = owner?.assembly;
  const rows = assemblyRows(state.ledgers, state.primaryLedgerId ?? cap.ledger_id);
  const selected = rows.find(row => row.path === state.assemblySelection);
  const component = selected?.component;
  const direct = selected?.ownerId === cap.ledger_id && JSON.stringify(selected.ownerPath) === JSON.stringify(state.assemblyOwnerPath);
  const incident = component && direct ? assembly?.joints.filter(j => j.component_a === component.id || j.component_b === component.id) ?? [] : [];
  const jointed = incident.length > 0;
  const blocked = state.busy || !!state.preview || !!state.draft || state.draftStarting || state.sourceLoading
    || !!state.sourceRequest || state.connection !== "open" || state.model?.source === "local";
  const unavailable = blocked || !!state.selectedSourceId || (!cap.can_edit && !cap.can_initialize)
    || (cap.can_edit && state.model?.source !== "server");
  const sources = Object.values(state.ledgers).filter(l => l.project_id !== cap.ledger_id &&
    (l.assembly ? cap.version === 2 : l.features.length));
  const run = (action: () => AssemblyOperation[]) => {
    try { setError(null); state.previewAssembly(action()); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Check the instance fields."); }
  };
  const ownerKey = `${state.sessionId}:${state.sessionRevision}:${cap.ledger_id}:${state.assemblyOwnerPath.join("/")}`;
  const key = `${ownerKey}:${state.assemblySelection}:${state.selectedSourceId}`;
  return <section aria-label="Assembly workspace" className="mb-3 rounded border border-hairline p-2 text-11">
    <h2 className="font-semibold">Assembly workspace</h2>
    <button className={buttonClass} disabled={unavailable || !state.assemblyPromptCapabilities?.available}
      onClick={() => state.startAssemblyPrompt({ kind: "definition" })}>Add to assembly with AI</button>
    {state.assemblyPromptCapabilities?.reason && <p>{state.assemblyPromptCapabilities.reason}</p>}
    <p>Editing {owner?.metadata.name ?? cap.ledger_id} · coordinates in this definition’s frame.</p>
    {state.assemblyOwnerPath.length > 0 && <p className="my-2">Shared definition: changes affect all {rows.filter(r => r.source?.project_id === cap.ledger_id).length} occurrences.
      The viewport shows the complete project. <button className={buttonClass} disabled={blocked}
        onClick={() => state.editAssemblyOwner(state.primaryLedgerId!, [])}>Back to project assembly</button></p>}
    {cap.reason && <p className="my-1 text-ink-4">{cap.reason}</p>}
    {state.selectedSourceId && <p className="my-1">Editing a shared source. <button className={buttonClass}
      disabled={blocked} onClick={() => state.selectSource(null)}>Return to assembly</button></p>}
    <div aria-label="Assembly instances" className="my-2 flex flex-col gap-1">
      {rows.map(row => <button key={row.path} title={row.path} disabled={unavailable} aria-pressed={selected?.path === row.path}
        className="break-words rounded border border-hairline p-1 text-left aria-pressed:border-accent disabled:opacity-40"
        onClick={() => state.selectAssembly(row.path)}>{row.labels.join(" / ")}
        <span className="text-ink-4"> · {row.source?.metadata.name ?? "Embedded part"} ({row.component.id.slice(-6)})</span></button>)}
    </div>
    {selected && component && <div key={key} aria-label="Instance inspector" className="space-y-2 border-t border-hairline pt-2">
      <p className="break-words">{selected.labels.join(" / ")}<br /><span className="text-ink-4">{selected.path}</span></p>
      {!direct && <p>This occurrence belongs to another definition. {cap.version === 2 && <button className={buttonClass} disabled={blocked}
        onClick={() => state.editAssemblyOwner(selected.ownerId, selected.ownerPath)}>Edit containing definition</button>}</p>}
      {selected.source?.assembly && cap.version === 2 && <p><button className={buttonClass} disabled={unavailable}
        onClick={() => state.editAssemblyOwner(selected.source!.project_id, selected.path.split("/"))}>Edit shared subassembly</button>
        {direct && <span> · Place this instance using the parent-frame controls below.</span>}</p>}
      {jointed && <p>This instance has an attachment. Placement and removal require attachment editing.</p>}
      {component.placement.relative_to && <p>Relative placement: placement and duplication are unavailable.</p>}
      {selected.source && !selected.source.assembly ? <p>Shared source: {selected.source.metadata.name}.
        Changes affect all {rows.filter(r => r.source?.project_id === selected.source?.project_id).length} instances.
        <button className={`ml-2 ${buttonClass}`} disabled={unavailable || !state.partCapabilities}
          onClick={() => state.selectSource(selected.source!.project_id)}>Edit shared source</button></p>
        : <p>Source geometry editing is unavailable for this definition.</p>}
      {direct && <>
        <form onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget);
          state.startAssemblyPrompt({ kind: "instance", component_id: component.id,
            detach_joint_ids: data.getAll("ai_detach").map(String) }); }}>
          <fieldset disabled={unavailable || !state.assemblyPromptCapabilities?.available}>
            {incident.length > 0 && <p>Allow AI to detach only the checked attachments. Detaching can move attached descendants.</p>}
            {incident.map(j => <label key={j.id} className="block break-all"><input type="checkbox" name="ai_detach" value={j.id} /> {j.id}</label>)}
            <button className={buttonClass}>Edit instance with AI</button>
          </fieldset>
        </form>
        <form onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget);
          run(() => [{ op: "rename_instance", component_id: component.id, name: readName(data) }]); }}>
          <fieldset disabled={unavailable}><label>Instance name <input name="name" aria-label="Instance name" required maxLength={100}
            defaultValue={component.name} className={inputClass} /></label><button className={buttonClass}>Preview rename</button></fieldset>
        </form>
        <form onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget);
          run(() => {
            if (jointed && !data.has("detach")) throw new Error("Explicitly select the listed attachments to detach before moving.");
            if (incident.length + 1 > cap.max_operations) throw new Error("Too many attachments for one request. Detach them in reviewed batches first.");
            return [...incident.map(j => ({ op: "remove_joint" as const, joint_id: j.id })),
              { op: "set_instance_placement", component_id: component.id, placement: readPlacement(data) }];
          }); }}>
          <fieldset disabled={unavailable || (jointed && cap.version === 1) || !!component.placement.relative_to}>
            {jointed && <label className="block"><input type="checkbox" name="detach" /> Detach these attachments and use this placement
              <span className="block break-all">{incident.map(j => j.id).join(", ")}</span>
              <span className="block">Attached descendants may move when the remaining links solve again.</span></label>}
            <PlacementFields placement={component.placement} /><button className={buttonClass}>Preview placement</button>
          </fieldset>
        </form>
        <details><summary>Repeat instance</summary><form onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget);
          run(() => [{ op: "duplicate_instance", component_id: component.id, name: readName(data), placement: readPlacement(data) }]); }}>
          <fieldset disabled={unavailable || !!component.placement.relative_to || (cap.version === 1 && !!selected.source?.assembly)}>
            <label>New instance name <input name="name" aria-label="Duplicate instance name" required maxLength={100} className={inputClass} /></label>
            <PlacementFields placement={component.placement} /><p>Choose a separate position to avoid overlapping solids.</p>
            <button className={buttonClass}>Preview duplicate</button>
          </fieldset>
        </form></details>
        <button className={buttonClass} disabled={unavailable || jointed || assembly?.components.length === 1}
          onClick={() => run(() => [{ op: "remove_instance", component_id: component.id }])}>Preview removal</button>
        <p className="text-ink-4">Removal keeps the shared source part.</p>
      </>}
    </div>}
    {(cap.can_initialize || cap.can_edit) && <details key={ownerKey} className="mt-2">
      <summary>{cap.can_initialize ? "Start assembly" : "Add instance"}</summary>
      <form onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget);
        run(() => [...(cap.can_initialize ? [{ op: "initialize_assembly" as const, name: readName(data, "assembly_name") }] : []),
          { op: "add_instance", source_ledger_id: String(data.get("source")), name: readName(data), placement: readPlacement(data) }]); }}>
        <fieldset disabled={unavailable || !sources.length || (assembly?.components.length ?? 0) >= cap.max_components} className="space-y-2">
          {cap.can_initialize && <label className="block">Assembly name <input aria-label="Assembly name" name="assembly_name" required maxLength={100} className={inputClass} /></label>}
          <label className="block">Source definition <select aria-label="Instance source" name="source" required className={inputClass}>
            {sources.map(l => <option key={l.project_id} value={l.project_id}>{l.metadata.name} · {l.assembly ? "Assembly" : "Part"} ({l.project_id.slice(-6)})</option>)}
          </select></label>
          <label className="block">Instance name <input aria-label="New instance name" name="name" required maxLength={100} className={inputClass} /></label>
          <PlacementFields placement={zero} /><button className={buttonClass}>Preview instance</button>
        </fieldset>
      </form>
      {!sources.length && <p>Create a source in Parts workspace first.</p>}
    </details>}
    {cap.version === 2 && <div key={`extended:${ownerKey}`}>
      {assembly && <AssemblyJointEditor assembly={assembly} disabled={unavailable} run={run}
        prompt={state.assemblyPromptCapabilities?.available ? state.startAssemblyPrompt : undefined} />}
      <SubassemblyCreate sources={sources} initialize={cap.can_initialize} run={run}
        disabled={unavailable || (assembly?.components.length ?? 0) >= cap.max_components
          || Object.values(state.ledgers).filter(l => l.assembly && l.project_id !== state.primaryLedgerId).length >= cap.max_subassemblies} />
    </div>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
