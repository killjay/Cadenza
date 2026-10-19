import { useStore } from "../lib/store";
import { fieldLabel, previewPresentation, assumptionValue } from "../lib/presentation";

export function EditPreview() {
  const state = useStore();
  const preview = state.preview;
  if (!preview) return state.interference ? <section role="alert" className="shrink-0 border-t border-hairline p-3 text-11">
    <strong>Change could not be built</strong><p>{state.interference.text}</p>
    <p>Adjust your dimensions or request and preview again.</p>
  </section> : null;
  const presentation = (ledgerId: string) => previewPresentation(state.ledgers, preview.changes, ledgerId);
  const disabled = state.busy || state.connection !== "open";
  return <section aria-label="Edit preview" className="flex max-h-[45%] shrink-0 flex-col border-t border-accent p-2.5 text-11">
    <h2 className="font-semibold text-accent">Preview — not applied</h2>
    {preview.independence && <p className="my-2">Make this instance independent as <strong>{preview.independence.name}</strong>.
      Original source {state.ledgers[preview.independence.old_source_id]?.metadata.name || "part"} remains available.
      {" "}{preview.independence.copied_assemblies} parent definition(s) copied. Position and attachments retained.</p>}
    <div className="mt-2 flex shrink-0 flex-wrap gap-2 border-b border-hairline pb-2">
      <button className="rounded bg-accent px-3 py-2 text-canvas disabled:opacity-40" disabled={disabled || state.previewLoading || state.previewRendering || !!state.previewError}
        onClick={() => state.decidePreview("apply")}>Apply changes</button>
      <button className="rounded border border-hairline px-3 py-2 disabled:opacity-40" disabled={disabled}
        onClick={() => state.decidePreview("cancel")}>{preview.draft_id ? "Back to sketch" : "Cancel"}</button>
      {preview.draft_id && <button disabled={disabled} onClick={() => state.actOnDraft("discard")}>Discard draft</button>}
      {preview.prompt && <button disabled={disabled} onClick={() => state.decidePreview("cancel", true)}>Revise request</button>}
    </div>
    {state.previewLoading && <p role="status">Loading candidate geometry…</p>}
    {state.previewError && <p role="alert">{state.previewError}</p>}
    {preview.display_ledger_id && preview.glb_url && <div className="my-2 flex shrink-0 flex-wrap gap-2">
      <button className="rounded border border-hairline px-2 py-1 disabled:opacity-40" disabled={disabled || state.previewLoading || !!preview.context_warning}
        aria-pressed={!state.previewWhole} onClick={() => void state.loadPreview(preview, false)}>
        {preview.context_component_path ? "Part in assembly" : "Part"}</button>
      <button className="rounded border border-hairline px-2 py-1 disabled:opacity-40" disabled={disabled || state.previewLoading}
        aria-pressed={state.previewWhole} onClick={() => void state.loadPreview(preview, true)}>Whole assembly</button>
    </div>}
    {preview.context_warning && <p>{preview.context_warning}</p>}
    <p className="text-ink-3">{preview.attachment_contacts?.length ? `${preview.attachment_contacts.length} declared face contacts verified. Interference checked. Other contacts are not verified.` : "Complete design rebuilt. Contact between mating faces is not verified."}</p>
    <details className="min-h-0 overflow-auto" key={preview.preview_id}>
    <summary className="cursor-pointer py-1">Review details ({preview.changes.length} changes)</summary>
    <p className="my-1">{preview.summary}</p>
    <p className="text-ink-4">Based on revision {preview.base_revision}. Apply to keep these changes.</p>
    <ul className="my-2 space-y-2">
      {preview.changes.map((change, i) => <li key={i} className="break-words rounded bg-wash p-1.5">
        <div className="text-ink-4">{presentation(change.ledger_id).changeLabel(change)}</div>
        <div>{presentation(change.ledger_id).value(change.before, change.path.split("/").at(-1))} → {presentation(change.ledger_id).value(change.after, change.path.split("/").at(-1))}</div>
      </li>)}
    </ul>
    {preview.display_ledger_id && <p className="my-1 text-ink-3">Candidate geometry only. Apply updates the complete project.</p>}
    {preview.assumptions.map((assumption, i) => <p key={i} className="my-1 text-yellow">
      Assumption: {fieldLabel(assumption.field)} = {assumptionValue(assumption.field, assumption.value)} ({assumption.basis})</p>)}
    <ul className="my-2 space-y-1 text-ink-3">{preview.checks.map((check, i) => <li key={i}>{check}</li>)}</ul>
    <details className="mt-2"><summary>Technical details</summary><pre className="whitespace-pre-wrap break-words text-10">{JSON.stringify({ changes: preview.changes, assumptions: preview.assumptions }, null, 2)}</pre></details>
    </details>
  </section>;
}
