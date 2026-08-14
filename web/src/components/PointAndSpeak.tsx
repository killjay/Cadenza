/**
 * The floating prompt (blueprint §5 step 3).
 *
 * It appears at the cursor, on the face the user just clicked, and it is the
 * only place in the product where a non-technical user has to type. So:
 *   * it is already focused when it appears — no second click to start typing
 *   * Escape closes it and clears the selection
 *   * Enter submits, and the payload carries the click coordinate silently
 *   * it never covers the face it is about (it flips when near an edge)
 *
 * The readout above the input is what makes the click feel understood: it
 * names the face using facts from the kernel ("Ø10.0 cylindrical face"), not
 * from the renderer.
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useStore } from "../lib/store";
import { occt } from "../workers/occtClient";

const SUGGESTIONS = [
  "Make this twice as thick",
  "Make this hole 12 mm",
  "Round these edges 3 mm",
];

function describe(sel: NonNullable<ReturnType<typeof useStore.getState>["selection"]>): string {
  const info = sel.info;
  if (!info) return "resolving face…";
  if (info.surfaceType === "cylinder" && info.radius) {
    return `Ø${(info.radius * 2).toFixed(1)} cylindrical face · ${info.area.toFixed(0)} mm²`;
  }
  return `${info.surfaceType} face · ${info.area.toFixed(0)} mm²`;
}

export function PointAndSpeak() {
  const selection = useStore((s) => s.selection);
  const open = useStore((s) => s.promptOpen);
  const busy = useStore((s) => s.busy);
  const verify = useStore((s) => s.verify);
  const submitPrompt = useStore((s) => s.submitPrompt);
  const select = useStore((s) => s.select);
  const setVerify = useStore((s) => s.setVerify);

  const [text, setText] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number }>({ left: 0, top: 0 });

  useEffect(() => {
    if (open) {
      setText("");
      // Focus after paint so the caret lands in the box that just appeared.
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open, selection?.faceHash]);

  // Place it at the cursor, flipped away from whichever edge it would overrun.
  useLayoutEffect(() => {
    if (!open || !selection) return;
    const el = boxRef.current;
    const w = el?.offsetWidth ?? 320;
    const h = el?.offsetHeight ?? 96;
    const gap = 14;
    const { x, y } = selection.screen;

    let left = x + gap;
    let top = y + gap;
    if (left + w > window.innerWidth - 12) left = x - w - gap;
    if (top + h > window.innerHeight - 12) top = y - h - gap;
    setPos({ left: Math.max(12, left), top: Math.max(12, top) });
  }, [open, selection]);

  if (!open || !selection) return null;

  const submit = (): void => {
    if (text.trim() === "") return;
    submitPrompt(text);
    setText("");
  };

  /** Re-runs the pick inside the kernel, against the exact B-rep. */
  const runVerify = async (): Promise<void> => {
    const facts = selection.info;
    if (!facts) return;
    // Fire the ray from just outside the surface, back along its own normal —
    // the kernel then has to find the same face the tessellation did.
    const n = facts.normal ?? selection.normal;
    const origin: [number, number, number] = [
      selection.point[0] + n[0] * 50,
      selection.point[1] + n[1] * 50,
      selection.point[2] + n[2] * 50,
    ];
    const dir: [number, number, number] = [-n[0], -n[1], -n[2]];
    const hit = await occt.raycast(origin, dir);
    setVerify(
      hit
        ? {
            faceHash: hit.faceHash,
            queryMs: Math.round(hit.queryMs * 10) / 10,
            agrees: hit.faceHash === selection.faceHash,
          }
        : { faceHash: -1, queryMs: 0, agrees: false },
    );
  };

  const [px, py, pz] = selection.point;

  return (
    <div
      ref={boxRef}
      className="pointer-events-auto fixed z-50 w-[340px] rounded-base border border-hairline-strong bg-elevated shadow-pop"
      style={{ left: pos.left, top: pos.top }}
      onPointerDown={(e) => e.stopPropagation()}
    >
      {/* what you pointed at */}
      <div className="flex items-center justify-between gap-2 border-b border-hairline px-2.5 py-1.5">
        <span className="truncate text-11 text-ink-2">{describe(selection)}</span>
        <span className="shrink-0 font-mono text-10 text-ink-4">
          {px.toFixed(1)}, {py.toFixed(1)}, {pz.toFixed(1)}
        </span>
      </div>

      {/* what you want */}
      <div className="flex items-center gap-2 px-2.5 py-2">
        <span className="select-none text-13 text-accent">›</span>
        <input
          ref={inputRef}
          value={text}
          disabled={busy}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submit();
            }
            if (e.key === "Escape") {
              e.preventDefault();
              select(null);
            }
          }}
          placeholder="Make this twice as thick"
          className="min-w-0 flex-1 bg-transparent text-13 text-ink outline-none placeholder:text-ink-4"
        />
        <button
          onClick={submit}
          disabled={text.trim() === "" || busy}
          className="shrink-0 rounded-sm bg-accent px-2 py-0.5 text-10 font-medium text-accent-ink disabled:opacity-30"
        >
          ⏎
        </button>
      </div>

      {/* one-tap phrasings, so the first thing a new user does succeeds */}
      <div className="flex flex-wrap gap-1 border-t border-hairline px-2.5 py-1.5">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            onClick={() => {
              setText(s);
              inputRef.current?.focus();
            }}
            className="rounded-sm bg-wash px-1.5 py-0.5 text-10 text-ink-3 hover:bg-wash-2 hover:text-ink-2"
          >
            {s}
          </button>
        ))}
      </div>

      {/* spike instrumentation — proves the fast pick and the kernel agree */}
      <div className="flex items-center justify-between gap-2 border-t border-hairline px-2.5 py-1 text-10 text-ink-4">
        <span className="font-mono">face #{selection.faceHash}</span>
        {verify ? (
          <span className={verify.agrees ? "text-green" : "text-red"}>
            {verify.agrees ? "kernel agrees" : `kernel says #${verify.faceHash}`} · {verify.queryMs}
            ms
          </span>
        ) : (
          <button onClick={() => void runVerify()} className="hover:text-ink-2">
            verify against B-rep
          </button>
        )}
      </div>
    </div>
  );
}
