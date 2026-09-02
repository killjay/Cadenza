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

function describe(sel: NonNullable<ReturnType<typeof useStore.getState>["selection"]>): string {
  const info = sel.info;
  if (!info) return "resolving face…";
  if (info.surfaceType === "cylinder" && info.radius) {
    return `Ø${(info.radius * 2).toFixed(1)} cylindrical face · ${info.area.toFixed(0)} mm²`;
  }
  return `${info.surfaceType} face · ${info.area.toFixed(0)} mm²`;
}

export function Composer() {
  const attachments = useStore((s) => s.attachments);
  const attaching = useStore((s) => s.attaching);
  const busy = useStore((s) => s.busy);
  const limits = useStore((s) => s.limits);
  const addFiles = useStore((s) => s.addFiles);
  const removeAttachment = useStore((s) => s.removeAttachment);
  const submitComposer = useStore((s) => s.submitComposer);
  const selection = useStore((s) => s.selection);
  const select = useStore((s) => s.select);

  const [text, setText] = useState("");
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const areaRef = useRef<HTMLTextAreaElement>(null);

  const full = attachments.length >= limits.max_images;
  const canSend = !busy && (text.trim() !== "" || attachments.length > 0);

  // Paste is bound to the window, not the textarea: a user who has just dropped
  // a screenshot into the clipboard has not necessarily focused the input, and
  // making them click first is exactly the friction this is meant to remove.
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const files = imageFilesFrom(e.clipboardData);
      if (files.length === 0) return;
      e.preventDefault();
      void addFiles(files);
      areaRef.current?.focus();
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [addFiles]);

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
      const files = imageFilesFrom(e.dataTransfer);
      if (files.length) void addFiles(files);
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
    setText("");
  };

  return (
    <div
      className={`shrink-0 border-t px-2.5 py-2 transition-colors ${
        dragging ? "border-accent bg-accent/10" : "border-hairline"
      }`}
    >
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

      {selection && (
        <div className="mb-1.5 flex items-center justify-between rounded-base border border-accent bg-accent/5 px-2 py-1.5 text-11">
          <div className="flex flex-col gap-0.5 min-w-0">
            <span className="truncate font-medium text-accent">{describe(selection)}</span>
            <span className="font-mono text-10 text-accent/70">
              {selection.point[0].toFixed(1)}, {selection.point[1].toFixed(1)}, {selection.point[2].toFixed(1)}
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
          selection 
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
          {busy ? "working…" : "Build ⏎"}
        </button>
      </div>
    </div>
  );
}
