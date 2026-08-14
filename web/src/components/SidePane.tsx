/**
 * Right column: the conversation, and what the kernel knows about the part.
 * Layout borrowed from draftsmith's ConversationPane — a scrolling transcript
 * pinned to the bottom, with the spike's instrumentation underneath it.
 */
import { useEffect, useRef } from "react";
import { useStore } from "../lib/store";
import { Composer } from "./Composer";

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-[3px]">
      <span className="text-10 text-ink-4">{label}</span>
      <span className="truncate font-mono text-10 text-ink-2">{value}</span>
    </div>
  );
}

export function SidePane() {
  const conversation = useStore((s) => s.conversation);
  const model = useStore((s) => s.model);
  const kernel = useStore((s) => s.kernel);
  const busy = useStore((s) => s.busy);
  const phase = useStore((s) => s.phase);

  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [conversation.length, busy]);

  const s = model?.summary;

  return (
    <aside className="flex min-h-0 flex-col border-l border-hairline bg-chrome">
      <div className="min-h-0 flex-1 overflow-y-auto px-2.5 py-2">
        {conversation.length === 0 && (
          <div className="mt-6 space-y-2 text-11 leading-relaxed text-ink-4">
            <p className="text-ink-3">Describe a part, or drop in a sketch.</p>
            <p>
              A dimensioned drawing works best — the numbers on it are read directly. Once the part
              is on screen, click any face to change it.
            </p>
            <p className="pt-2 text-10">
              <span className="text-ink-3">F</span> fit · <span className="text-ink-3">G</span> grid
              · <span className="text-ink-3">E</span> edges · <span className="text-ink-3">Esc</span>{" "}
              deselect
            </p>
          </div>
        )}

        {conversation.map((item) => (
          <div key={item.id} className="mb-2">
            {item.kind === "user" && (
              <div className="rounded-base border border-hairline bg-elevated px-2 py-1.5">
                {item.images && item.images.length > 0 && (
                  <div className="mb-1.5 flex flex-wrap gap-1">
                    {item.images.map((src) => (
                      <img
                        key={src}
                        src={src}
                        alt=""
                        className="h-12 w-12 rounded-sm border border-hairline object-cover"
                      />
                    ))}
                  </div>
                )}
                <div className="text-12 text-ink">{item.text}</div>
                {item.point && (
                  <div className="mt-1 font-mono text-9 text-ink-4">
                    at {item.point.map((n) => n.toFixed(1)).join(", ")}
                  </div>
                )}
              </div>
            )}
            {item.kind === "assistant" && (
              <div className="px-0.5 text-12 leading-relaxed text-ink-2">
                {item.text}
                {item.assumptions && item.assumptions.length > 0 && (
                  // Numbers the agent inferred rather than read. Surfaced because a
                  // guessed dimension and a measured one must never look alike.
                  <ul className="mt-1 space-y-0.5 border-l border-yellow/50 pl-1.5">
                    {item.assumptions.map((a, i) => (
                      <li key={i} className="text-10 text-ink-4">
                        <span className="text-ink-3">{a.field}</span> = {String(a.value)} —{" "}
                        {a.basis}
                        {typeof a.confidence === "number" && (
                          <span className="text-ink-4"> ({Math.round(a.confidence * 100)}%)</span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            {item.kind === "status" && (
              <div className="px-0.5 text-11 text-ink-4">{item.text}</div>
            )}
            {item.kind === "error" && (
              <div className="rounded-base border border-red/40 bg-red/10 px-2 py-1.5 text-11 text-red">
                {item.text}
              </div>
            )}
          </div>
        ))}

        {busy && (
          <div className="flex items-center gap-1.5 px-0.5 py-1 text-11 text-ink-3">
            <span className="spin inline-block h-2.5 w-2.5 rounded-full border border-accent border-t-transparent" />
            {phase ?? "working"}
          </div>
        )}

        <div ref={endRef} />
      </div>

      <Composer />

      <div className="border-t border-hairline px-2.5 py-2">
        <div className="mb-1 text-10 uppercase tracking-wide text-ink-4">Exact geometry</div>
        {kernel.state !== "ready" && (
          <div className="text-11 text-ink-4">
            {kernel.state === "booting" && "booting occt-wasm…"}
            {kernel.state === "cold" && "kernel not started"}
            {kernel.state === "failed" && <span className="text-red">{kernel.error}</span>}
          </div>
        )}
        {s ? (
          <>
            <Row label="faces" value={String(s.faceCount)} />
            <Row label="edges" value={String(s.edgeCount)} />
            <Row label="volume" value={`${s.volume.toFixed(1)} mm³`} />
            <Row label="area" value={`${s.area.toFixed(1)} mm²`} />
            <Row
              label="bbox"
              value={`${(s.bbox.max[0] - s.bbox.min[0]).toFixed(0)} × ${(s.bbox.max[1] - s.bbox.min[1]).toFixed(0)} × ${(s.bbox.max[2] - s.bbox.min[2]).toFixed(0)}`}
            />
            <Row label="STEP parse" value={`${s.importMs.toFixed(0)} ms`} />
            {model?.pickMs !== undefined && (
              <Row label="pick mesh" value={`${model.pickMs.toFixed(0)} ms`} />
            )}
            {model?.parseMs !== undefined && (
              <Row label="GLB parse" value={`${model.parseMs} ms`} />
            )}
          </>
        ) : (
          kernel.state === "ready" && <div className="text-11 text-ink-4">no STEP loaded</div>
        )}
      </div>
    </aside>
  );
}
