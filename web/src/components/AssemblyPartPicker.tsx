import { useEffect, useRef, useState } from "react";
import { useStore } from "../lib/store";
import { assemblyOwner, partUsage } from "../lib/assembly";
import { PlacementFields, readName, readPlacement, inputClass, buttonClass, zero } from "./AssemblyFields";
import { PartCreate } from "./PartCreate";

export interface PartPickerTarget { ownerId: string; ownerPath: string[]; sourceId?: string; create?: boolean; }

export function AssemblyPartPicker({ target, close }: { target: PartPickerTarget | null; close: () => void }) {
  const state = useStore();
  const dialog = useRef<HTMLDialogElement>(null);
  const lastTarget = useRef(target);
  if (target) lastTarget.current = target;
  const formTarget = target ?? lastTarget.current;
  const [search, setSearch] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [create, setCreate] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cap = state.assemblyCapabilities;
  const validOwner = !!target && !!state.primaryLedgerId
    && assemblyOwner(state.ledgers, state.primaryLedgerId, target.ownerPath) === target.ownerId;
  const owner = target ? state.ledgers[target.ownerId] : undefined;
  const ready = validOwner && cap?.ledger_id === target?.ownerId
    && JSON.stringify(state.assemblyOwnerPath) === JSON.stringify(target?.ownerPath);
  const blocked = state.busy || !!state.preview || !!state.draft || state.draftStarting || state.sourceLoading
    || !!state.sourceRequest || state.connection !== "open" || state.model?.source === "local";
  const unavailable = blocked || !!state.selectedSourceId || !ready || !cap || (!cap.can_edit && !cap.can_initialize)
    || (cap.can_edit && state.model?.source !== "server");
  const sources = Object.values(state.ledgers).filter(source => source.project_id !== target?.ownerId
    && (source.assembly ? (cap?.version ?? 1) >= 2 : source.features.length > 0));
  const filtered = sources.filter(source => source.metadata.name.toLocaleLowerCase().includes(search.toLocaleLowerCase()));
  const { uses } = partUsage(state.ledgers, state.primaryLedgerId ?? "");

  useEffect(() => {
    if (!target) { dialog.current?.close(); return; }
    setSearch(""); setSourceId(target.sourceId ?? ""); setCreate(!!target.create); setError(null);
    dialog.current?.showModal();
  }, [target]);
  useEffect(() => {
    if (target) dialog.current?.querySelector<HTMLInputElement>(create ? '[aria-label="New part name"]' : '[type="search"]')?.focus();
  }, [create, target]);
  useEffect(() => {
    if (!target || !validOwner || ready || blocked || state.selectedSourceId || state.model?.source !== "server") return;
    state.editAssemblyOwner(target.ownerId, target.ownerPath);
  }, [target, validOwner, ready, blocked, state.selectedSourceId, state.model?.source]);
  useEffect(() => {
    if (target && (state.preview || state.busy || state.newPartName)) close();
  }, [state.preview, state.busy, state.newPartName]);

  return <dialog ref={dialog} aria-label="Add part to assembly" onCancel={event => { event.preventDefault(); close(); }}
    onKeyDown={event => { if (event.key === "Escape") event.stopPropagation(); }}
    className="m-auto max-h-[85dvh] w-[min(480px,calc(100%-2rem))] overflow-y-auto rounded border border-hairline bg-chrome p-4 text-ink backdrop:bg-black/50">
    <div className="mb-3 flex items-center justify-between gap-3"><h2 className="font-semibold">Add part</h2>
      <button className={buttonClass} onClick={close} aria-label="Close part picker">Close</button></div>
    <p className="mb-2 text-12">Destination: {owner?.metadata.name || "Project"}</p>
    {!!target?.ownerPath.length && <p className="mb-2 text-11 text-ink-3">This is a shared subassembly definition. Adding a part updates all its instances.</p>}
    <div className="mb-3 flex gap-4 text-12">
      <button className={buttonClass} aria-pressed={!create} onClick={() => setCreate(false)}>Existing part</button>
      <button className={buttonClass} aria-pressed={create} disabled={!state.partCapabilities} onClick={() => setCreate(true)}>Create new part</button>
    </div>
    <div hidden={!create} className="text-12"><p>Create and apply the source first, then use Add part to place it.</p><PartCreate /></div>
    <div hidden={create} className="space-y-3 text-12">
      {state.selectedSourceId && <p>Return to Project view before placing a part. <button className={buttonClass} disabled={blocked}
        onClick={() => state.selectSource(null)}>Return to project view</button></p>}
      {!validOwner && <p role="alert">The destination is no longer available. Close this picker and select an assembly.</p>}
      {ready && cap?.reason && <p>{cap.reason}</p>}
      {!owner?.assembly && !!owner?.features.length && <p>A populated primary part cannot be converted here. Create separate sources in an empty project to start an assembly.</p>}
      <label className="block">Search parts<input type="search" aria-label="Search parts" className={inputClass}
        value={search} onChange={event => setSearch(event.target.value)} /></label>
      <label className="block">Source part<select aria-label="Instance source" className={inputClass} value={sourceId}
        onChange={event => setSourceId(event.target.value)}>
        <option value="">Choose a part</option>
        {sources.filter(source => source.project_id === sourceId || filtered.includes(source)).map(source =>
          <option key={source.project_id} value={source.project_id}>{source.metadata.name} · {source.assembly ? "Assembly" : `${uses.get(source.project_id) ?? 0} placed`}
            {sources.some(other => other.project_id !== source.project_id && other.metadata.name === source.metadata.name) ? ` (${source.project_id.slice(-6)})` : ""}</option>)}
      </select></label>
      {!filtered.length && <p>No matching parts. Try another search or create a new part.</p>}
      <form key={`${state.sessionId}:${state.sessionRevision}:${formTarget?.ownerId}:${formTarget?.ownerPath.join("/")}`} className="space-y-2"
        onSubmit={event => {
          event.preventDefault();
          try {
            setError(null);
            if (unavailable || !sources.some(source => source.project_id === sourceId)) return;
            const data = new FormData(event.currentTarget);
            state.previewAssembly([...(cap!.can_initialize ? [{ op: "initialize_assembly" as const, name: readName(data, "assembly_name") }] : []),
              { op: "add_instance", source_ledger_id: sourceId, name: readName(data), placement: readPlacement(data) }]);
          } catch (cause) { setError(cause instanceof Error ? cause.message : "Check the part placement."); }
        }}>
        <fieldset disabled={unavailable || !sourceId || (owner?.assembly?.components.length ?? 0) >= (cap?.max_components ?? 0)} className="space-y-2">
          {cap?.can_initialize && <label className="block">Assembly name<input aria-label="Assembly name" name="assembly_name" required maxLength={100} className={inputClass} /></label>}
          <label className="block">Instance name<input aria-label="New instance name" name="name" required maxLength={100} className={inputClass} /></label>
          <PlacementFields placement={zero} />
          <p>Coordinates are relative to the destination assembly. Choose a clear position to avoid overlapping parts.</p>
          <button className={buttonClass}>Preview instance</button>
        </fieldset>
      </form>
      {error && <p role="alert" className="text-red">{error}</p>}
    </div>
  </dialog>;
}
