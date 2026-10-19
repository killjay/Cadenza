import { useEffect, useState } from "react";
import { useStore } from "../lib/store";
import { assemblyRows } from "../lib/assembly";
import { inputClass, buttonClass } from "./AssemblyFields";

export function OccurrenceProperties({ path }: { path: string }) {
  const state = useStore();
  const rows = assemblyRows(state.ledgers, state.primaryLedgerId ?? "");
  const row = rows.find(item => item.path === path);
  useEffect(() => {
    state.discoverViewedAssembly();
  }, [path, state.sessionId, state.sessionRevision, state.sourceLoading, state.sourceRequest, state.model?.source,
    state.busy, state.preview, state.independenceDraft]);
  if (!row) return null;
  const cap = state.assemblyCapabilities;
  const blocked = state.busy || !!state.preview || !!state.draft || state.draftStarting || state.sourceLoading || !!state.sourceRequest
    || state.connection !== "open" || !!state.faceAttachment || !!state.independenceDraft || state.model?.source !== "source";
  const supported = cap?.can_edit && cap.operations.includes("rename_instance") && cap.ledger_id === row.ownerId
    && JSON.stringify(state.assemblyOwnerPath) === JSON.stringify(row.ownerPath)
    && cap.session_id === state.sessionId && cap.revision === state.sessionRevision;
  const ownerUses = rows.filter(item => item.source?.project_id === row.ownerId).length;
  return <section aria-label="Selected assembly part" className="mb-3 rounded border border-hairline p-2">
    <h2 className="break-words text-13 font-semibold">{row.component.name}</h2>
    <p className="my-1 text-ink-3">{row.labels.join(" / ")}</p>
    {ownerUses > 1 && <p className="my-2">This containing assembly is reused {ownerUses} times. Renaming this child changes its name in each use. Make independent first to change only this occurrence.</p>}
    <OccurrenceName key={`${state.sessionId}:${state.sessionRevision}:${path}`} current={row.component.name ?? ""}
      names={state.ledgers[row.ownerId].assembly!.components.filter(c => c.id !== row.component.id).map(c => c.name ?? "")}
      disabled={blocked || !supported} submit={name => state.previewAssembly([{ op: "rename_instance", component_id: row.component.id, name }])} />
    {!supported && <p role="status">{state.assemblyDiscovery ? "Checking name editing support…" : "Name editing is unavailable in this view on this server."}</p>}
    <p className="mt-2 text-ink-3">Position in containing assembly (mm): {row.component.placement.origin.join(", ")}. Use Placement and attachments to change positioning.</p>
  </section>;
}

function OccurrenceName({ current, names, disabled, submit }: { current: string; names: string[]; disabled: boolean; submit: (name: string) => void }) {
  const [name, setName] = useState(current);
  const duplicate = names.some(other => other.trim().toLocaleLowerCase() === name.trim().toLocaleLowerCase());
  return <form onSubmit={event => { event.preventDefault(); if (!disabled && !duplicate && name.trim()) submit(name.trim()); }}>
    <label>Name in assembly<input className={inputClass} aria-label="Name in assembly" maxLength={100} required
      disabled={disabled} value={name} onChange={event => setName(event.target.value)} /></label>
    {duplicate && <p role="alert">Another part in this assembly has that name.</p>}
    <button className={buttonClass} disabled={disabled || duplicate || !name.trim() || name.trim() === current}>Preview name change</button>
    <p className="mt-1 text-ink-3">Changes this assembly label. Geometry and the source name stay unchanged.</p>
  </form>;
}
