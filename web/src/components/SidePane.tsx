/**
 * Right column: the conversation, and what the kernel knows about the part.
 * Layout borrowed from draftsmith's ConversationPane — a scrolling transcript
 * pinned to the bottom, with the spike's instrumentation underneath it.
 */
import { useEffect, useRef } from "react";
import { useStore } from "../lib/store";
import { AssemblyEditor } from "./AssemblyEditor";
import { PartsWorkspace } from "./PartsWorkspace";
import { Composer } from "./Composer";
import { SavedSurfaces } from "./SavedSurfaces";
import { EditPreview } from "./EditPreview";
import { FeatureEditor } from "./FeatureEditor";
import { FeatureCreate } from "./FeatureCreate";
import { PatternControls } from "./PatternControls";
import { SketchEditor } from "./SketchEditor";
import { ParameterEditor } from "./ParameterEditor";
import { SketchDraftEditor } from "./SketchDraftEditor";

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-[3px]">
      <span className="text-10 text-ink-4">{label}</span>
      <span className="truncate font-mono text-10 text-ink-2">{value}</span>
    </div>
  );
}

export function SidePane() {
  const selectedSourceId = useStore(s => s.selectedSourceId);
  const sourceLoading = useStore(s => s.sourceLoading);
  const startFeaturePrompt = useStore(s => s.startFeaturePrompt);
  const promptDisabled = useStore(s => !s.featurePromptAvailable || !s.capabilities || !s.agentsAvailable
    || s.busy || !!s.preview || !!s.draft || s.draftStarting || s.connection !== "open" || s.model?.source === "local");
  const conversation = useStore((s) => s.conversation);
  const kernel = useStore((s) => s.kernel);
  const busy = useStore((s) => s.busy);
  const draft = useStore((s) => s.draft);
  const phase = useStore((s) => s.phase);
  const selections = useStore((s) => s.selections);
  const ledgers = useStore((s) => s.ledgers);
  const activeLedgerIds = useStore((s) => s.activeLedgerIds);
  const setActiveLedgerIds = useStore((s) => s.setActiveLedgerIds);
  const sessionId = useStore((s) => s.sessionId);
  const revision = useStore((s) => s.sessionRevision);

  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [conversation.length, busy]);

  return (
    <aside className="flex min-h-0 flex-col border-l border-hairline bg-chrome">
      <div className={draft ? "hidden" : "min-h-0 flex-1 overflow-y-auto px-2.5 py-2"}>
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
          <div key={item.id} className="mb-2 select-text">
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

      <EditPreview />
      <SketchDraftEditor />
      {!draft && <Composer />}
      {!draft && !selectedSourceId && <SavedSurfaces />}

      <div className={draft ? "hidden" : "max-h-80 shrink-0 overflow-auto border-t border-hairline px-2.5 py-2"}>
        {selections.length > 0 && (
          <div className="mb-4 rounded-base border border-accent/30 bg-accent/5 p-2">
            <div className="mb-2 text-11 font-semibold text-accent">Active Selection</div>
            {selections.map((sel, i) => (
              <div key={i} className="mb-2 last:mb-0">
                <Row label="Coordinates (X,Y,Z)" value={sel.point.map(n => n.toFixed(1)).join(", ")} />
                <Row label="Normal Vector" value={sel.normal.map(n => n.toFixed(1)).join(", ")} />
                {sel.label && <Row label="Face Label" value={sel.label} />}
                {sel.info && (
                   <>
                     <Row label="Surface Type" value={sel.info.surfaceType} />
                     {sel.info.radius !== null && <Row label="Radius" value={`${sel.info.radius.toFixed(2)} mm`} />}
                   </>
                )}
              </div>
            ))}
          </div>
        )}
        <PartsWorkspace />
        <AssemblyEditor />
        <div className="mb-1 text-10 uppercase tracking-wide text-ink-4">{selectedSourceId ? "Source features" : "Project sources"}</div>
        {kernel.state !== "ready" && (
          <div className="text-11 text-ink-4">
            {kernel.state === "booting" && "booting occt-wasm…"}
            {kernel.state === "cold" && "kernel not started"}
            {kernel.state === "failed" && <span className="text-red">{kernel.error}</span>}
          </div>
        )}
        {Object.values(ledgers).length > 0 ? (
          <div className="mt-2 space-y-2">
            {Object.values(ledgers).filter(l => !selectedSourceId || l.project_id === selectedSourceId).map((l) => (
              <div key={l.project_id} className="rounded border border-hairline bg-wash p-2">
                <div className="flex items-center gap-2 mb-2">
                  <input
                    type="checkbox"
                    checked={activeLedgerIds.includes(l.project_id)}
                    onChange={(e) => {
                      if (e.target.checked) {
                        setActiveLedgerIds([...activeLedgerIds, l.project_id]);
                      } else {
                        setActiveLedgerIds(activeLedgerIds.filter(id => id !== l.project_id));
                      }
                    }}
                    className="accent-accent"
                  />
                  <span className="text-12 font-semibold text-ink-2">
                    {l.metadata.name || "Untitled Part"}
                  </span>
                </div>
                <fieldset disabled={sourceLoading}>
                <ParameterEditor ledger={l} />
                {!l.assembly && <button disabled={promptDisabled} className="my-1 text-11 text-accent disabled:opacity-40"
                  onClick={() => startFeaturePrompt(l.project_id)}>Add with AI</button>}
                <FeatureCreate key={`${sessionId}:${revision}:${l.project_id}`} ledger={l} />
                <PatternControls key={`pattern:${sessionId}:${revision}:${l.project_id}`} ledger={l} />
                {l.features.length > 0 ? (
                  <div className="space-y-1.5 pl-4 border-l border-hairline ml-1.5">
                    {l.features.map(f => (
                      <div key={f.id} id={`feature-${l.project_id}-${f.id}`} className="rounded bg-chrome px-2 py-1.5 border border-hairline">
                        <div className="flex items-center justify-between">
                          <span className="text-11 font-medium text-ink-2">{f.name || f.kind}{f.suppressed ? " (suppressed)" : ""}</span>
                          <span className="text-9 uppercase tracking-wider text-ink-4">{f.operation}</span>
                        </div>
                        <FeatureEditor ledgerId={l.project_id} feature={f} />
                        {f.kind === "profile" && <SketchEditor ledgerId={l.project_id} feature={f} />}
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="pl-4 ml-1.5 text-10 text-ink-4">No features</div>
                )}
                </fieldset>
              </div>
            ))}
          </div>
        ) : (
          kernel.state === "ready" && <div className="text-11 text-ink-4">no components yet</div>
        )}
      </div>
    </aside>
  );
}
