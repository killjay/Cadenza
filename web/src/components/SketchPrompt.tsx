import { useState } from "react";
import { useStore } from "../lib/store";

export function SketchPrompt() {
  const state = useStore(), draft = state.draft!, proposal = state.draftProposal;
  const [text, setText] = useState("");
  const disabled = state.busy || state.connection !== "open" || !!state.preview;
  const index = draft.definition.entities.findIndex(e => e.id === state.draftEntityId);
  return <section aria-label="Sketch prompt" className="flex max-h-[42%] shrink-0 flex-col border-t border-hairline bg-chrome p-3 text-12">
    <div className="min-h-0 overflow-auto">
      <p className="font-semibold">Sketch assistant · {index < 0 ? "No entity selected" : index === 0 ? "Outer selected" : `Opening ${index} selected`}</p>
      <p className="text-ink-3">Describe dimensions in mm. Review the proposal before updating the draft.</p>
      {!state.agentsAvailable && <p>No AI provider configured. Direct sketch controls remain available.</p>}
      {state.draftError && <p role="alert" className="text-red">{state.draftError}</p>}
      {proposal && <div className="my-2 space-y-2 rounded border border-accent p-2">
        <p className="font-semibold">{proposal.question ? "Clarification needed" : "Proposed sketch changes — not applied"}</p>
        <p>{proposal.summary}</p>
        {proposal.question ? <p>{proposal.question}</p> : <>
          <ul>{proposal.interpretation.map((item, i) => <li key={i}>{item}</li>)}</ul>
          <details><summary>Actual changes ({proposal.changes.length})</summary>
            <ul className="space-y-2">{proposal.changes.map((change, i) => <li key={i} className="break-words">
              <p>{change.label}</p><p>{change.before} → {change.after}</p>
            </li>)}</ul>
          </details>
          <ul className="text-ink-3">{proposal.checks.map((check, i) => <li key={i}>{check}</li>)}</ul>
        </>}
        {proposal.assumptions.map((assumption, i) => <p key={i} className="text-yellow">Assumption: {assumption}</p>)}
        <div className="flex gap-4">
          {!proposal.question && <button className="text-accent disabled:opacity-40" disabled={disabled} onClick={() => state.decideSketchProposal("update")}>Update sketch</button>}
          <button disabled={disabled} onClick={() => state.decideSketchProposal("cancel")}>Cancel proposal</button>
        </div>
        <p className="text-ink-3">Manual edits or selecting another entity invalidate this proposal. Finish and Apply are still required to accept the part.</p>
      </div>}
      <form className="mt-2 flex items-end gap-3" onSubmit={event => {
        event.preventDefault(); state.promptSketch(text, !!proposal?.question); setText("");
      }}>
        <label className="min-w-0 flex-1">{proposal?.question ? "Your answer" : "Sketch request"}
          <textarea aria-label="Sketch request" className="block w-full resize-none rounded border border-hairline bg-canvas p-2" rows={2}
            value={text} onChange={event => setText(event.target.value)} maxLength={state.limits.max_prompt_chars}
            disabled={disabled || !state.agentsAvailable} placeholder="Add two 6 mm openings with their centres 15 mm from the left and right edges." />
        </label>
        <button className="text-accent disabled:opacity-40" disabled={disabled || !state.agentsAvailable || !text.trim()}>
          {proposal?.question ? "Answer clarification" : "Propose changes"}
        </button>
      </form>
      {state.busy && <p role="status">{state.phase}</p>}
    </div>
  </section>;
}
