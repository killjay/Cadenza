import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { useStore } from "../lib/store";
import { requestNumericDraftDiscard } from "../lib/featureCommands";
import { assemblyRows } from "../lib/assembly";
import { FaceAttachmentPicker } from "./FaceAttachmentPicker";
import { Viewport } from "./Viewport";
import { SketchWorkspace } from "./SketchWorkspace";
import { SketchDraftEditor } from "./SketchDraftEditor";
import { EditPreview } from "./EditPreview";
import { ModelTree } from "./ModelTree";
import { PropertiesPanel } from "./PropertiesPanel";
import type { Inspection } from "./PropertiesPanel";
import { AssistantPanel } from "./AssistantPanel";

export function WorkspacePanels() {
  const state = useStore();
  const [tab, setTab] = useState<"properties" | "assistant">("assistant");
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [assembly, setAssembly] = useState(false);
  const [pendingAssembly, setPendingAssembly] = useState<{ path?: string; session: string | null } | null>(null);
  const [width, setWidth] = useState(360), [expanded, setExpanded] = useState(false);
  const [compact, setCompact] = useState(() => matchMedia("(max-width: 1100px)").matches);
  const [treeOpen, setTreeOpen] = useState(false), [inspectorOpen, setInspectorOpen] = useState(false);
  const host = useRef<HTMLElement>(null);
  const drag = useRef<{ x: number; width: number } | null>(null);
  const show = (next: typeof tab) => { setTab(next); setInspectorOpen(true); setTreeOpen(false); };
  const inspect = (target: Inspection) => {
    if (state.draft || state.draftStarting) return;
    if ((inspection?.ledgerId !== target.ledgerId || inspection?.featureId !== target.featureId || assembly)
      && !requestNumericDraftDiscard(window, true)) return;
    setInspection(target); setAssembly(false); show("properties");
  };
  const inspectAssembly = (path?: string) => {
    if (state.draft || state.draftStarting) return;
    if (state.selectedSourceId) {
      state.selectSource(null);
      if (useStore.getState().selectedSourceId) return;
      setPendingAssembly({ path, session: state.sessionId });
      return;
    }
    if (path !== undefined && path !== state.assemblySelection) {
      state.selectAssembly(path);
      if (useStore.getState().assemblySelection !== path) return;
    } else if (!assembly && !requestNumericDraftDiscard(window, true)) return;
    setAssembly(true); show("properties");
  };
  useEffect(() => {
    if (!pendingAssembly) return;
    if (pendingAssembly.session !== state.sessionId || state.selectedSourceId || state.sourceError) {
      setPendingAssembly(null); return;
    }
    if (state.busy || state.sourceLoading || state.sourceRequest || state.model?.source !== "server") return;
    inspectAssembly(pendingAssembly.path);
    setPendingAssembly(null);
  }, [pendingAssembly, state.sessionId, state.selectedSourceId, state.sourceError, state.busy, state.sourceLoading, state.sourceRequest, state.model]);
  useEffect(() => {
    const media = matchMedia("(max-width: 1100px)");
    const resize = () => setCompact(media.matches);
    media.addEventListener("change", resize); return () => media.removeEventListener("change", resize);
  }, []);
  useEffect(() => {
    const assistant = () => { if (!useStore.getState().draft) show("assistant"); };
    const newPart = () => { setTreeOpen(true); setInspectorOpen(false); };
    window.addEventListener("cadenza:revise", assistant);
    window.addEventListener("cadenza:assistant", assistant);
    window.addEventListener("cadenza:new-part", newPart);
    return () => { window.removeEventListener("cadenza:revise", assistant); window.removeEventListener("cadenza:assistant", assistant); window.removeEventListener("cadenza:new-part", newPart); };
  }, []);
  useEffect(() => {
    const select = (event: Event) => inspect((event as CustomEvent<Inspection>).detail);
    window.addEventListener("cadenza:inspect-feature", select);
    return () => window.removeEventListener("cadenza:inspect-feature", select);
  });
  useEffect(() => {
    setInspection(state.selectedSourceId ? { ledgerId: state.selectedSourceId } : null);
    setAssembly(false);
    if (state.selectedSourceId) show("properties");
  }, [state.selectedSourceId, state.selectedSourcePath?.join("/"), state.sessionId]);
  const selection = state.selections.length === 1 ? state.selections[0] : undefined;
  useEffect(() => {
    if (selection?.source) show("properties");
  }, [selection?.faceHash, selection?.source?.generation]);
  const reference = !selection?.selection_error ? selection?.surface_reference ?? (selection?.saved_reference
    ? state.ledgers[selection.saved_reference.ledger_id]?.surface_references?.[selection.saved_reference.reference_id] : undefined) : undefined;
  const profileLedger = !reference?.source_part_id ? reference?.source_ledger_id : undefined;
  const profileId = reference?.profile_id;
  useEffect(() => {
    if (profileLedger && profileId) inspect({ ledgerId: profileLedger, featureId: profileId });
  }, [profileLedger, profileId]);
  useEffect(() => {
    if (state.draft || state.draftStarting) show("properties");
    if (state.preview) setInspectorOpen(true);
  }, [state.draft?.id, state.draftStarting, state.preview?.preview_id]);
  useEffect(() => {
    if (!inspection?.featureId || tab !== "properties" || (compact && !inspectorOpen)) return;
    const panel = document.getElementById(`feature-${inspection.ledgerId}-${inspection.featureId}`);
    panel?.querySelectorAll("details").forEach(details => { details.open = true; });
  }, [inspection, tab, compact, inspectorOpen]);
  const source = inspection && state.ledgers[inspection.ledgerId];
  const validInspection = source && (!inspection.featureId || source.features.some(feature => feature.id === inspection.featureId)) ? inspection : null;
  const displayId = state.preview?.display_ledger_id || state.selectedSourceId || state.primaryLedgerId;
  const occurrence = assemblyRows(state.ledgers, state.primaryLedgerId ?? "").find(row => row.path === state.selectedSourcePath?.join("/"));
  const display = !state.preview && occurrence ? occurrence.component.name : displayId ? state.ledgers[displayId]?.metadata.name : null;
  const maximumWidth = () => Math.min(720, Math.max(300, (host.current?.clientWidth ?? innerWidth) - 500));
  const resize = (value: number) => { setExpanded(false); setWidth(Math.min(maximumWidth(), Math.max(300, value))); };
  const close = (target: "tree" | "inspector") => {
    if (target === "tree") setTreeOpen(false); else setInspectorOpen(false);
    host.current?.querySelector<HTMLButtonElement>(`[data-toggle="${target}"]`)?.focus();
  };
  const visible = !compact || inspectorOpen;
  return <main ref={host} className="workspace" style={{ "--inspector-width": `${expanded ? Math.min(680, maximumWidth()) : width}px` } as CSSProperties}>
    <div className="workspace-toolbar">
      <button data-toggle="tree" aria-expanded={treeOpen} aria-controls="model-tree" onClick={() => { setTreeOpen(!treeOpen); setInspectorOpen(false); }}>Model tree</button>
      <span className="min-w-0 flex-1 truncate">{state.preview ? "Preview" : state.selectedSourceId ? "Part" : "Project"}: {display || "Untitled"}</span>
      <button data-toggle="inspector" aria-expanded={inspectorOpen} aria-controls="workspace-inspector" onClick={() => { setInspectorOpen(!inspectorOpen); setTreeOpen(false); }}>Inspector</button>
    </div>
    <aside id="model-tree" aria-label="Model tree" className="workspace-tree" hidden={compact && !treeOpen} onKeyDown={event => {
      if (compact && event.key === "Escape") { event.stopPropagation(); close("tree"); }
    }}>
      {compact && <button className="border-b border-hairline p-2 text-right" onClick={() => close("tree")}>Close model tree</button>}
      <ModelTree inspection={validInspection} inspect={inspect} inspectAssembly={inspectAssembly} />
    </aside>
    <div className="workspace-canvas">
      <Viewport />
      <FaceAttachmentPicker />
      {state.model?.colorMode && <div aria-label="Model colour legend" className="pointer-events-none absolute bottom-3 left-3 z-10 max-w-[90%] rounded bg-chrome/90 px-2 py-1 text-10 text-ink-3">
        {state.model.colorMode === "instances" ? "Colours distinguish instances; neutral faces have no instance attribution." : state.model.colorMode === "imported" ? "Imported model colours." : "Neutral shading: one part, including its features."}
        {!state.preview && !state.draft && <span> Blue: selected surface. White: hover.</span>}
        {state.selectedSourcePath && !state.preview && !state.sourceContextWarning && <span> Faded: assembly context, not selectable.</span>}
        <span> Colour does not indicate validity.</span>
      </div>}
      <div className="pointer-events-none absolute left-3 top-3 z-10 max-w-[80%] rounded bg-chrome/90 px-2 py-1 text-11">
        {state.sourceLoading ? "Loading part" : state.preview ? "Preview — not applied" : state.selectedSourceId ? "Part" : "Project view"}: {display || "Untitled"}
      </div>
      {state.draft && <SketchWorkspace key={state.draft.id} />}
    </div>
    <div className="workspace-resizer" hidden={compact} role="separator" aria-label="Inspector width" aria-orientation="vertical"
      aria-valuemin={300} aria-valuemax={maximumWidth()} aria-valuenow={expanded ? Math.min(680, maximumWidth()) : width} tabIndex={0}
      onPointerDown={event => { drag.current = { x: event.clientX, width: event.currentTarget.nextElementSibling!.getBoundingClientRect().width }; event.currentTarget.setPointerCapture(event.pointerId); }}
      onPointerMove={event => { if (drag.current) resize(drag.current.width + drag.current.x - event.clientX); }}
      onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}
      onKeyDown={event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault(); resize(event.key === "Home" ? 300 : event.key === "End" ? maximumWidth() : width + (event.key === "ArrowLeft" ? 20 : -20));
      }} />
    <aside id="workspace-inspector" aria-label="Inspector" className="workspace-inspector" hidden={!visible} onKeyDown={event => {
      if (compact && event.key === "Escape") { event.stopPropagation(); close("inspector"); }
    }}>
      <div className="flex shrink-0 items-center gap-1 border-b border-hairline p-2">
        <div role="tablist" aria-label="Inspector tabs" className="flex min-w-0 flex-1" onKeyDown={event => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault(); const next = event.key === "Home" ? "properties" : event.key === "End" ? "assistant" : tab === "properties" ? "assistant" : "properties";
          if (state.draft && next === "assistant") return;
          setTab(next); event.currentTarget.querySelector<HTMLButtonElement>(`#tab-${next}`)?.focus();
        }}>
          {(["properties", "assistant"] as const).map(value => <button id={`tab-${value}`} key={value} role="tab" aria-selected={tab === value}
            aria-controls={`panel-${value}`} tabIndex={tab === value ? 0 : -1} disabled={value === "assistant" && !!state.draft}
            className="flex-1 rounded px-2 py-2 aria-selected:bg-wash-2 aria-selected:text-accent disabled:opacity-40" onClick={() => setTab(value)}>
            {value === "properties" ? "Properties" : "Assistant"}</button>)}
        </div>
        {!compact && <button className="px-1 text-11" aria-label={expanded ? "Restore panel width" : "Expand panel"} onClick={() => setExpanded(!expanded)}>{expanded ? "↙" : "↗"}</button>}
        {compact && <button aria-label="Close inspector" className="px-2" onClick={() => close("inspector")}>×</button>}
      </div>
      <EditPreview />
      <div id="panel-properties" role="tabpanel" aria-labelledby="tab-properties" hidden={tab !== "properties"} className="flex min-h-0 flex-1 flex-col">
        <div hidden={!!state.draft} className="flex min-h-0 flex-1 flex-col"><PropertiesPanel inspection={validInspection} assembly={assembly} /></div>
        <SketchDraftEditor />
      </div>
      <div id="panel-assistant" role="tabpanel" aria-labelledby="tab-assistant" hidden={tab !== "assistant" || !!state.draft} className="flex min-h-0 flex-1 flex-col">
        <AssistantPanel active={visible && tab === "assistant" && !state.draft} />
      </div>
    </aside>
  </main>;
}
