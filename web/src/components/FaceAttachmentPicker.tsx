import { useEffect } from "react";
import { useStore } from "../lib/store";
import { assemblyRows } from "../lib/assembly";
import { buttonClass } from "./AssemblyFields";

/** Kept over the viewport so changing source view never hides the picking step. */
export function FaceAttachmentPicker() {
  const state = useStore(), draft = state.faceAttachment;
  useEffect(() => {
    if (draft && (draft.sessionId !== state.sessionId || draft.revision !== state.sessionRevision
      || state.connection !== "open" || state.draft || state.model?.source === "local")) {
      useStore.setState({ faceAttachment: null });
    }
  }, [draft, state.sessionId, state.sessionRevision, state.connection, state.draft, state.model?.source]);
  if (!draft || state.preview) return null;
  const owner = state.ledgers[draft.ownerId];
  const name = (id: string) => owner?.assembly?.components.find(c => c.id === id)?.name || "Part";
  const selection = state.selections[0], ref = selection?.surface_reference;
  const supported = state.selections.length === 1 && selection.mode === "face" && !selection.selection_error
    && !!selection.source && !!ref && ["box", "cylinder"].includes(ref.feature_kind) && ref.role !== "lateral_face";
  const busy = state.busy || state.sourceLoading || !!state.sourceRequest;
  const uses = owner?.project_id === state.primaryLedgerId ? 1 : assemblyRows(state.ledgers, state.primaryLedgerId ?? "")
    .filter(row => row.source?.project_id === owner?.project_id).length;
  return <section aria-label="Attach faces" className="absolute left-3 top-3 z-20 max-h-[55%] w-[340px] max-w-[calc(100%_-_24px)] overflow-auto rounded border border-accent bg-chrome p-3 text-11 shadow-xl">
    <h2 className="font-semibold">Attach faces · {draft.a ? "2 of 2" : "1 of 2"}</h2>
    <p>{name(draft.componentA)} anchors {name(draft.componentB)}. The following part and its attached descendants may move.</p>
    <p>Editing {owner?.metadata.name}. Affects {uses} assembly occurrence(s).</p>
    <div className="my-2 flex flex-wrap gap-2">
      <button className={buttonClass} disabled={busy} onClick={() => state.attachmentSide("a")}>Repick fixed face {draft.a ? "✓" : ""}</button>
      <button className={buttonClass} disabled={busy || !draft.a} onClick={() => state.attachmentSide("b")}>Repick following face {draft.b ? "✓" : ""}</button>
    </div>
    <p>Select a face on {name(draft.step === "a" ? draft.componentA : draft.componentB)}. Surrounding parts are context only.</p>
    <p>Box faces and cylinder end faces are supported. Pick the points that should meet; their in-plane positions are retained.</p>
    {selection && <p>{supported ? selection.label || ref?.role.replaceAll("_", " ") : "Choose a supported, unambiguous planar face."}</p>}
    {state.sourceError && <p role="alert">{state.sourceError}</p>}
    <div className="mt-2 flex flex-wrap gap-2">
      <button className={buttonClass} disabled={busy || !supported} onClick={state.useAttachmentFace}>Use selected face</button>
      <button className={buttonClass} disabled={busy || !draft.a || !draft.b} onClick={state.previewFaceAttachment}>Preview face attachment</button>
      <button className={buttonClass} disabled={state.busy} onClick={state.cancelFaceAttachment}>Cancel attachment</button>
    </div>
    {draft.a && draft.b && <p>Both faces selected. Preview verifies contact and interference before Apply.</p>}
  </section>;
}
