import { useStore } from "../lib/store";

export function SavedSurfaces() {
  const state = useStore();
  const entries = Object.entries(state.ledgers).flatMap(([ledgerId, ledger]) =>
    Object.entries(ledger.surface_references ?? {}).map(([id, reference]) => ({ ledgerId, ledger, id, reference })));
  const disabled = state.busy || !!state.preview || state.surfaceChecking !== null || state.connection !== "open" || state.model?.source === "local";
  return <section aria-label="Saved surfaces" className="max-h-52 overflow-y-auto border-t border-hairline px-2.5 py-2">
    <h2 className="mb-2 text-10 uppercase tracking-wide text-ink-4">Saved surfaces</h2>
    {!entries.length && <p className="text-11 text-ink-4">Select a face and ask to remember it with a name.</p>}
    {entries.map(({ ledgerId, ledger, id, reference }) => {
      const key = `${ledgerId}:${id}`;
      const result = state.surfaceChecks[key];
      const checking = state.surfaceChecking === key;
      return <div key={key} className="mb-2 rounded border border-hairline bg-wash p-2 text-11">
        <div className="font-medium text-ink-2">{id.replace(/^ref_/, "").replaceAll("_", " ")}</div>
        <div className="text-10 text-ink-4">{ledger.metadata.name} · {reference.role.replaceAll("_", " ")}</div>
        <p role="status" className="my-1 text-ink-3">{checking ? "Checking…" : result?.status === "resolved" ? "Ready to use"
          : result?.message ?? "Not checked for this revision"}</p>
        <div className="flex gap-3">
          <button className="text-ink-2 hover:text-accent disabled:opacity-40" disabled={disabled}
            onClick={() => void state.checkSurface(ledgerId, id)}>Check</button>
          <button disabled={disabled || result?.status !== "resolved" || state.model?.revision !== state.sessionRevision}
            className="text-accent disabled:opacity-40"
            onClick={() => void state.checkSurface(ledgerId, id, true)}>Use for edit</button>
        </div>
      </div>;
    })}
  </section>;
}
