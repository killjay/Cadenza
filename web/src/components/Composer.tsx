/**
 * The prompt box that is not anchored to a click: how a part gets CREATED,
 * and the only place a sketch can be attached.
 *
 * Point & Speak (`PointAndSpeak.tsx`) answers "change this thing I am pointing
 * at". It cannot answer "make me a thing", because there is nothing to point at
 * yet. This is that entry point, and since the Draftsman is also the agent that
 * reads drawings, attaching an image belongs here rather than on the floating
 * edit box.
 *
 * Three ways in, because people reach for different ones: the paperclip, a
 * drag-and-drop onto the panel, and ⌘V of a screenshot — the last being how
 * anyone with a drawing open on screen will actually try to do it.
 */
import { useEffect, useRef, useState } from "react";
import { imageFilesFrom } from "../lib/image";
import { useStore } from "../lib/store";
import { assemblyRows } from "../lib/assembly";

function describe(sel: NonNullable<ReturnType<typeof useStore.getState>["selections"][0]>): string {
  if (sel.saved_reference) return sel.label ?? "Saved surface";
  if (sel.faceHash === -1) return sel.label ?? "Previous selection";
  const info = sel.info;
  if (!info) return "resolving face…";
  if (info.surfaceType === "cylinder" && info.radius) {
    return `Ø${(info.radius * 2).toFixed(1)} cylindrical face · ${info.area.toFixed(0)} mm²`;
  }
  return `${info.surfaceType} face · ${info.area.toFixed(0)} mm²`;
}

export function Composer({ active = true }: { active?: boolean }) {
  const attachments = useStore((s) => s.attachments);
  const attaching = useStore((s) => s.attaching);
  const busy = useStore((s) => s.busy || !!s.faceAttachment);
  const limits = useStore((s) => s.limits);
  const addFiles = useStore((s) => s.addFiles);
  const removeAttachment = useStore((s) => s.removeAttachment);
  const submitComposer = useStore((s) => s.submitComposer);
  const selections = useStore((s) => s.selections);
  const newPartName = useStore(s => s.newPartName);
  const sourceLoading = useStore(s => s.sourceLoading);
  const selectedSourceId = useStore(s => s.selectedSourceId);
  const partAvailable = useStore(s => !!s.partCapabilities);
  const featureTarget = useStore(s => s.featurePromptTarget);
  const assemblyTarget = useStore(s => s.assemblyPromptTarget);
  const assemblyBlocked = useStore(s => s.assemblyPromptBlocked);
  const assemblyCap = useStore(s => s.assemblyPromptCapabilities);
  const primaryId = useStore(s => s.primaryLedgerId);
  const featureAvailable = useStore(s => s.featurePromptAvailable);
  const ledgers = useStore(s => s.ledgers);
  const clearFeatureTarget = useStore(s => s.startFeaturePrompt);
  const select = useStore((s) => s.select);

  const [text, setText] = useState("");
  const [dragging, setDragging] = useState(false);
  const preview = useStore(s => s.preview);
  const draft = useStore(s => s.draft);
  const fileRef = useRef<HTMLInputElement>(null);
  const areaRef = useRef<HTMLTextAreaElement>(null);
  const previousTarget = useRef(featureTarget);
  const previousAssemblyTarget = useRef(assemblyTarget);
  useEffect(() => {
    if (!text.trim() && !attachments.length) return;
    const guard = (event: Event) => {
      const decision = (event as CustomEvent<{ discard?: boolean; preservePrompt?: boolean }>).detail;
      if (decision.preservePrompt) return;
      decision.discard ??= window.confirm("Discard unsent edits and change target? Choose Cancel to keep editing.");
      if (decision.discard) { setText(""); useStore.getState().clearAttachments(); }
      else event.preventDefault();
    };
    window.addEventListener("cadenza:discard-numeric-edits", guard);
    return () => window.removeEventListener("cadenza:discard-numeric-edits", guard);
  }, [text, attachments.length]);
  useEffect(() => {
    if (previousAssemblyTarget.current && !assemblyTarget) setText("");
    previousAssemblyTarget.current = assemblyTarget;
  }, [assemblyTarget]);

  useEffect(() => {
    if (previousTarget.current && !featureTarget) setText("");
    previousTarget.current = featureTarget;
  }, [featureTarget]);

  const full = attachments.length >= limits.max_images;
  const draftStarting = useStore(s => s.draftStarting);
  const connection = useStore(s => s.connection);
  const agentsAvailable = useStore(s => s.agentsAvailable);
  const canSend = !sourceLoading && (!selectedSourceId || !!featureTarget || !!newPartName) && !busy && !preview && !draft && !draftStarting
    && !assemblyBlocked
    && (assemblyTarget ? !!assemblyCap?.available && connection === "open" && text.trim() !== ""
        && (!attachments.length || assemblyCap.vision_available)
      : newPartName ? partAvailable && agentsAvailable && connection === "open" && text.trim() !== ""
      : featureTarget ? featureAvailable && agentsAvailable && connection === "open" && text.trim() !== ""
      : (text.trim() !== "" || attachments.length > 0));

  useEffect(() => {
    const revise = (event: Event) => { setText((event as CustomEvent<string>).detail); requestAnimationFrame(() => {
      if (areaRef.current?.getClientRects().length) areaRef.current.focus();
    }); };
    window.addEventListener("cadenza:revise", revise);
    return () => window.removeEventListener("cadenza:revise", revise);
  }, []);

  // Paste is bound to the window, not the textarea: a user who has just dropped
  // a screenshot into the clipboard has not necessarily focused the input, and
  // making them click first is exactly the friction this is meant to remove.
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const files = imageFilesFrom(e.clipboardData);
      if (files.length === 0) return;
      const target = e.target;
      if (target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))
        && target !== areaRef.current) return;
      const state = useStore.getState();
      if (state.draft || state.draftStarting || state.preview) return;
      e.preventDefault();
      if (!active) window.dispatchEvent(new Event("cadenza:assistant"));
      void addFiles(files);
      requestAnimationFrame(() => areaRef.current?.focus());
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [addFiles, active]);

  // Drop is bound to the window for the same reason, plus a worse one: the
  // obvious place to drop a drawing is the big 3D viewport, not this 290px
  // pane. Landing it only on the pane meant a drop onto the model did nothing
  // — and the browser default is to NAVIGATE AWAY to the image file, losing
  // the session. Accept it anywhere and cancel the default everywhere.
  useEffect(() => {
    const hasFiles = (e: DragEvent) =>
      Array.from(e.dataTransfer?.types ?? []).includes("Files");

    const onDragOver = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      setDragging(true);
    };
    const onDragLeave = (e: DragEvent) => {
      if (e.relatedTarget === null) setDragging(false); // left the window
    };
    const onDrop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      setDragging(false);
      const state = useStore.getState();
      if (state.draft || state.draftStarting || state.preview) return;
      const files = imageFilesFrom(e.dataTransfer);
      if (files.length) { window.dispatchEvent(new Event("cadenza:assistant")); void addFiles(files); }
    };

    window.addEventListener("dragover", onDragOver);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("drop", onDrop);
    };
  }, [addFiles]);

  const send = () => {
    if (!canSend) return;
    submitComposer(text);
    if (!featureTarget && !newPartName && !assemblyTarget) setText("");
  };

  const rows = assemblyRows(ledgers, primaryId ?? "");
  const primary = primaryId ? ledgers[primaryId] : null;
  const owner = assemblyTarget ? ledgers[assemblyTarget.ledgerId] : null;
  const ownerLabels = assemblyTarget?.ownerPath.length
    ? rows.find(row => row.path === assemblyTarget.ownerPath.join("/"))?.labels.join(" / ") : "Project root";
  const componentName = (id: string) => owner?.assembly?.components.find(component => component.id === id)?.name || "Unnamed instance";

  return (
    <div
      className={`shrink-0 border-t px-2.5 py-2 transition-colors ${
        dragging ? "border-accent bg-accent/10" : "border-hairline"
      }`}
    >
      {(assemblyTarget || assemblyBlocked) && <div className="mb-2 rounded border border-accent/40 p-2 text-11" role="status">
        {assemblyTarget ? <>
          <div>Assembly AI: {owner?.metadata.name} / {{ definition: "Add parts", instance: "Edit instance", attachment: "Edit attachment" }[assemblyTarget.target.kind]}</div>
          <div className="break-words">Location: {ownerLabels || "Assembly"}</div>
          {assemblyTarget.target.kind === "instance" && <div>Instance: {componentName(assemblyTarget.target.component_id)}<br />
            Allowed detachments: {assemblyTarget.target.detach_joint_ids?.length || "None"}</div>}
          {assemblyTarget.target.kind === "attachment" && <div>A: {componentName(assemblyTarget.target.component_a)}<br />
            B: {componentName(assemblyTarget.target.component_b)}<br />{assemblyTarget.target.joint_id ? "Edit attachment" : "New attachment"}</div>}
          <p>Shared definition: affects {assemblyTarget.ownerPath.length ? assemblyRows(ledgers, primaryId ?? "").filter(r => r.source?.project_id === assemblyTarget.ledgerId).length : 1} occurrence(s).
            Use millimetres and degrees relative to this assembly. Review before Apply.</p>
          {!!attachments.length && !assemblyCap?.vision_available && <p>Image interpretation is unavailable. Remove images to send text.</p>}
        </> : <p>The assembly target changed. Select an AI target again.</p>}
        <button type="button" className="text-accent" disabled={busy || !!preview || !!draft || draftStarting}
          onClick={() => useStore.getState().startAssemblyPrompt(null)}>Clear assembly mode</button>
      </div>}
      {newPartName && <div className="mb-2 rounded border border-accent/40 p-2 text-11" role="status">
        Create new part: {newPartName}. Existing parts stay unchanged.
        <button type="button" disabled={busy || !!preview} className="ml-2 text-accent"
          onClick={() => useStore.getState().startPartPrompt(null)}>Cancel creation mode</button>
      </div>}
      {featureTarget && <div className="mb-2 rounded border border-accent/40 p-2 text-11" role="status">
        <div>{featureTarget.face ? featureTarget.face.mode === "edit_feature" ? "Edit this feature" : "Add on this face" : featureTarget.featureId ? "Edit with AI" : "Add with AI"}: {ledgers[featureTarget.ledgerId]?.metadata.name ?? "Part"}
          {featureTarget.featureId && ` / ${ledgers[featureTarget.ledgerId]?.features.find(f => f.id === featureTarget.featureId)?.name || "Selected feature"}`}</div>
        <p className="text-10 text-ink-3">Changes update this part and all {rows.filter(row => row.source?.project_id === featureTarget.ledgerId).length} reused instances. Describe the intended change; review before Apply.</p>
        <button disabled={busy || !!preview || !!draft || draftStarting} className="text-accent disabled:opacity-40"
          onClick={() => clearFeatureTarget(null)}>Clear AI target</button>
      </div>}
      {!newPartName && !featureTarget && !assemblyTarget && !assemblyBlocked && <div className="mb-2 text-11" role="status">
        <p>{selectedSourceId ? `Viewing part: ${ledgers[selectedSourceId]?.metadata.name}. Choose Add with AI or Edit with AI in Properties.`
          : primary?.assembly ? `General modelling: ${primary.metadata.name}. Select the assembly name or an instance in the model tree for assembly actions.`
          : primary?.features.length ? `General modelling: ${primary.metadata.name}. Additions modify this part; they do not create separate assembly parts.`
          : "General modelling starts a primary part. For an assembly, create independent parts first."}</p>
        {partAvailable && <button type="button" className="text-accent disabled:opacity-40" disabled={busy || !!preview || !!draft || draftStarting}
          onClick={() => window.dispatchEvent(new Event("cadenza:new-part"))}>New independent part</button>}
      </div>}
      {dragging && (
        <div className="mb-1.5 rounded-base border border-dashed border-accent px-2 py-3 text-center text-11 text-accent">
          Drop the drawing anywhere
        </div>
      )}
      {attachments.length > 0 && (
        <div className="mb-1.5 flex flex-wrap gap-1.5">
          {attachments.map((img, i) => (
            <div
              key={img.previewUrl}
              className="group relative h-14 w-14 overflow-hidden rounded-base border border-hairline bg-elevated"
              title={`${img.name ?? "sketch"} — ${img.width}×${img.height}, ${(img.bytes / 1024).toFixed(0)} KB`}
            >
              <img src={img.previewUrl} alt="" className="h-full w-full object-cover" />
              <button
                onClick={() => removeAttachment(i)}
                className="absolute right-0 top-0 hidden h-4 w-4 items-center justify-center bg-canvas/85 text-10 text-ink group-hover:flex"
                aria-label="Remove image"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      {selections.length > 0 && (
        <div className="mb-1.5 flex items-center justify-between rounded-base border border-accent bg-accent/5 px-2 py-1.5 text-11">
          <div className="flex flex-col gap-0.5 min-w-0">
            <span className="truncate font-medium text-accent">{selectedSourceId ? "Selected part surface · choose an action in Properties" : describe(selections[0])}</span>
            <span className="font-mono text-10 text-accent/70">
              {selections[0].point[0].toFixed(1)}, {selections[0].point[1].toFixed(1)}, {selections[0].point[2].toFixed(1)}
            </span>
          </div>
          <button
            onClick={() => select(null)}
            className="shrink-0 p-1 text-accent hover:text-accent-hover hover:bg-accent/10 rounded-sm"
            aria-label="Clear selection"
            title="Clear selection"
          >
            ×
          </button>
        </div>
      )}

      <textarea
        ref={areaRef}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            send();
          }
        }}
        rows={2}
        maxLength={limits.max_prompt_chars}
        placeholder={
          assemblyTarget ? "Describe the change in millimetres and degrees relative to this assembly"
          : featureTarget ? "Describe the change, including dimensions and placement"
          : selectedSourceId ? "Choose Add with AI or Edit with AI in Properties"
          : selections.length > 0
            ? "What should happen to this face?"
            : attachments.length
            ? "Anything to add about the drawing? (optional)"
            : "Describe a part, or drop in a sketch…"
        }
        className="w-full resize-none rounded-base border border-hairline bg-elevated px-2 py-1.5 text-12 text-ink placeholder:text-ink-4 focus:border-accent focus:outline-none"
      />

      <div className="mt-1 flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            multiple
            className="hidden"
            onChange={(e) => {
              void addFiles(Array.from(e.target.files ?? []));
              e.target.value = "";
            }}
          />
          <button
            onClick={() => fileRef.current?.click()}
            disabled={full || attaching}
            className="rounded-base border border-hairline px-1.5 py-0.5 text-10 text-ink-3 hover:text-ink disabled:opacity-40"
            title={full ? `At most ${limits.max_images} images` : "Attach a sketch or drawing"}
          >
            {attaching ? "reading…" : "📎 sketch"}
          </button>
          <span className="text-9 text-ink-4">drop or ⌘V</span>
        </div>

        <button
          onClick={send}
          disabled={!canSend}
          className="rounded-base bg-accent px-2 py-0.5 text-10 font-medium text-accent-ink disabled:opacity-30"
        >
          {busy ? "working…" : (featureTarget || newPartName || assemblyTarget) ? "Preview with AI" : "Build ⏎"}
        </button>
      </div>
    </div>
  );
}
