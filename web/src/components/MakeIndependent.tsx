import { useState } from "react";
import { useStore } from "../lib/store";
import { assemblyRows } from "../lib/assembly";
import { inputClass, buttonClass } from "./AssemblyFields";

export function MakeIndependent({ path }: { path: string | null }) {
  const state = useStore();
  const row = assemblyRows(state.ledgers, state.primaryLedgerId ?? "").find(r => r.path === path);
  const draft = state.independenceDraft;
  const open = !!draft && draft.path === path && draft.session === state.sessionId && draft.revision === state.sessionRevision;
  const blocked = state.busy || !!state.preview || !!state.draft || state.draftStarting || state.sourceLoading
    || !!state.sourceRequest || state.connection !== "open" || state.model?.source === "local" || !!state.faceAttachment;
  if (!row?.source || row.source.assembly) return <p className="my-2 text-ink-3">Select a placed part in the assembly tree to make one instance independent.</p>;
  return <div className="my-2 rounded border border-hairline p-2">
    {open ? <IndependentForm key={`${draft.session}:${draft.revision}:${draft.path}`}
      sourceName={row.source.metadata.name} label={row.labels.join(" / ")} parents={row.ownerPath.length} blocked={blocked} />
      : <button className={buttonClass} disabled={blocked} onClick={() => state.startIndependence(row.path)}>Make independent</button>}
  </div>;
}

function IndependentForm({ sourceName, label, parents, blocked }: { sourceName: string; label: string; parents: number; blocked: boolean }) {
  const state = useStore();
  const [name, setName] = useState(`${sourceName.slice(0, 88)} custom`);
  const duplicate = Object.values(state.ledgers).some(l => !l.assembly && l.metadata.name.trim().toLowerCase() === name.trim().toLowerCase());
  const cap = state.assemblyCapabilities;
  const available = cap?.version === 4 && cap.can_edit;
  return <form aria-label="Make independent" onSubmit={event => { event.preventDefault(); state.previewIndependent(name); }}>
    <p className="font-semibold">Make {label} independent</p>
    <p className="my-2">Copy {sourceName} for this instance. The original stays available. Position and attachments are retained; later edits can still move attached parts.</p>
    {parents > 0 && <p>{parents} parent assembly definition(s) will also be copied to keep other branches unchanged.</p>}
    <label>New part name <input className={inputClass} aria-label="Independent part name" value={name}
      required maxLength={100} disabled={blocked} onChange={event => setName(event.target.value)} /></label>
    {duplicate && <p role="alert">Choose a name different from existing source parts.</p>}
    {!available && <p role="status">{state.assemblyDiscovery ? "Checking server support…" : cap?.reason || "Make independent requires an updated server."}</p>}
    <div className="mt-2 flex flex-wrap gap-2">
      <button className={buttonClass} disabled={blocked || !available || duplicate || !name.trim()}>Preview independent part</button>
      <button type="button" className={buttonClass} disabled={blocked} onClick={state.cancelIndependence}>Cancel</button>
    </div>
  </form>;
}

export function IndependentResult() {
  const state = useStore(), result = state.independentResult;
  if (!result) return null;
  return <div role="status" className="mb-3 rounded border border-accent p-2">
    <p>{result.name} is independent. The original source is retained.</p>
    <p>Choose a fresh feature or face action to edit it; previous AI requests are not resubmitted.</p>
    <button className={buttonClass} disabled={state.busy || !!state.preview || !!state.draft || state.draftStarting || state.sourceLoading || state.connection !== "open"}
      onClick={() => state.selectSource(result.new_source_id, false, result.component_path)}>Edit independent part</button>
  </div>;
}
