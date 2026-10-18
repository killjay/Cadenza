import { PartCreate } from "./PartCreate";
import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import type { Ledger } from "../lib/types";
import { useStore } from "../lib/store";
import { SourceRename } from "./SourceRename";

export function PartsWorkspace({ renderFeatures }: { renderFeatures?: (source: Ledger) => ReactNode }) {
  const state = useStore();
  const newPart = useRef<HTMLDetailsElement>(null);
  const openNewPart = () => { if (newPart.current) { newPart.current.open = true; requestAnimationFrame(() => newPart.current?.querySelector("input")?.focus()); } };
  useEffect(() => {
    window.addEventListener("cadenza:new-part", openNewPart);
    return () => window.removeEventListener("cadenza:new-part", openNewPart);
  }, []);
  if (!state.partCapabilities) return null;
  const blocked = state.busy || !!state.preview || !!state.draft || state.draftStarting
    || state.connection !== "open" || state.model?.source === "local";
  const sources = Object.values(state.ledgers).filter(l => !l.assembly && (l.features.length || l.project_id !== state.primaryLedgerId));
  const primary = state.primaryLedgerId ? state.ledgers[state.primaryLedgerId] : null;
  const canRename = state.partCapabilities.rename_source === true;
  return <section className="mb-3 rounded border border-hairline p-2 text-11" aria-label="Parts workspace">
    <div className="mb-2 flex flex-wrap justify-between gap-2"><h2 className="font-semibold">Parts workspace</h2>
      <button className="text-accent disabled:opacity-40" disabled={blocked} onClick={openNewPart}>New part</button></div>
    <button className="text-accent disabled:opacity-40" disabled={blocked} onClick={() => state.selectSource(null)}>
      Project view{state.primaryLedgerId && !state.ledgers[state.primaryLedgerId]?.features.length
        && !state.ledgers[state.primaryLedgerId]?.assembly ? " (empty)" : ""}
    </button>
    <p className="my-1 text-ink-3">{primary?.assembly ? "Project view shows placed assembly instances. Parts below are reusable sources."
      : primary?.features.length ? "Project view contains one part. Its features are shapes within that part, not separate assembly parts."
      : "Project view is empty until you place parts in an assembly. View each independent part below."}</p>
    <div className="my-2 flex flex-col gap-1">
      {sources.map(source => <div key={source.project_id}><button disabled={blocked}
        aria-pressed={state.selectedSourceId === source.project_id}
        className="rounded border border-hairline px-2 py-1 text-left disabled:opacity-40 aria-pressed:border-accent"
        onClick={() => state.selectSource(state.selectedSourceId === source.project_id ? null : source.project_id)}>
        View {source.metadata.name}
      </button>{canRename && source.features.length > 0 && <details className="ml-2">
        <summary className="cursor-pointer">Part actions for {source.metadata.name}</summary>
        <SourceRename key={`${state.sessionRevision}:${source.project_id}`} source={source} disabled={blocked || state.sourceLoading} />
      </details>}{renderFeatures?.(source)}</div>)}
    </div>
    {state.selectedSourceId && <p className="my-1 text-ink-3">Source-local view: {state.ledgers[state.selectedSourceId]?.metadata.name}.
      Select faces to inspect; edit features below. Source changes affect all instances. Face-directed AI edits and sketch workspace require Project view.</p>}
    {state.sourceLoading && <p role="status">Loading selected part…</p>}
    {state.sourceError && <p role="alert" className="text-red">{state.sourceError}</p>}
    <details ref={newPart} className="mt-2">
      <summary>Create an independent part</summary>
      <PartCreate />
    </details>
  </section>;
}
