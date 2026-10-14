import { useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { useStore } from "../lib/store";
import { drawnEntity, fitSketch, gridSpacing, panSketch, screenToSketch, sketchToScreen, zoomSketch } from "../lib/sketchView";
import type { Point2, SketchTool, SketchView } from "../lib/sketchView";

export function SketchCanvas({ active, tool, setTool }: { active: boolean; tool: SketchTool; setTool: Dispatch<SetStateAction<SketchTool>> }) {
  const state = useStore(), draft = state.draft!;
  const host = useRef<HTMLDivElement>(null), svg = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState({ width: 800, height: 600 });
  const [view, setView] = useState<SketchView | null>(null);
  const [anchor, setAnchor] = useState<Point2 | null>(null), [cursor, setCursor] = useState<Point2 | null>(null);
  const [error, setError] = useState("");
  const pan = useRef<{ id: number; x: number; y: number } | null>(null);
  const entities = draft.solution?.entities ?? draft.definition.entities;
  const current = view ?? fitSketch(entities, size);
  const selected = entities.find(e => e.id === state.draftEntityId);
  const disabled = state.busy || state.connection !== "open" || !active;
  useEffect(() => {
    if (!host.current) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0 && entry.contentRect.height > 0)
        setSize({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(host.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => { setAnchor(null); setCursor(null); setError(""); }, [tool, active, draft.revision]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!active || event.ctrlKey || event.metaKey || event.altKey) return;
      const target = event.target;
      if (target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))) return;
      if (event.key.toLowerCase() === "f") { event.preventDefault(); setView(fitSketch(entities, size)); }
      if (event.key.toLowerCase() === "g") { event.preventDefault(); state.toggleGrid(); }
      if (event.key === "Escape") { event.preventDefault(); setAnchor(null); setCursor(null); setError(""); setTool("select"); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, entities, size, setTool, state.toggleGrid]);
  const local = (event: { clientX: number; clientY: number }) => {
    const rect = svg.current!.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };
  const origin = sketchToScreen({ x: 0, y: 0 }, current, size);
  const spacing = gridSpacing(current.scale), pixels = spacing * current.scale;
  const related = new Set(selected ? [selected.id] : []);
  for (let i = 0; i < entities.length; i++) for (const c of draft.definition.constraints)
    if (c.kind === "coincident" && (related.has(c.entity_a) || related.has(c.entity_b))) { related.add(c.entity_a); related.add(c.entity_b); }
  const conflicts = new Set(draft.solution?.conflicts ?? []);
  const conflictingEntities = new Set(draft.definition.constraints.filter(c => conflicts.has(c.id))
    .flatMap(c => c.kind === "dimension" ? [c.entity_id] : [c.entity_a, c.entity_b]));
  let ghost = null;
  if (anchor && cursor && tool !== "select") {
    try { ghost = drawnEntity(tool, anchor, cursor); } catch { /* A zero-sized initial gesture has no outline yet. */ }
  }
  const transform = `translate(${size.width / 2} ${size.height / 2}) scale(${current.scale} ${-current.scale}) translate(${-current.x} ${-current.y})`;
  return <div ref={host} className="relative min-h-0 flex-1 bg-canvas">
    <svg ref={svg} role="group" aria-label="Sketch canvas" tabIndex={0} className="absolute inset-0 h-full w-full touch-none"
      viewBox={`0 0 ${size.width} ${size.height}`} onContextMenu={e => e.preventDefault()}
      onWheel={event => { if (!active) return; setView(zoomSketch(current, local(event), Math.exp(-Math.max(-300, Math.min(300, event.deltaY)) * 0.002), size)); }}
      onPointerDown={event => {
        if (event.button === 1 || event.button === 2) {
          event.preventDefault(); pan.current = { id: event.pointerId, x: event.clientX, y: event.clientY };
          event.currentTarget.setPointerCapture(event.pointerId); setView(current); return;
        }
        if (event.button !== 0 || disabled || tool === "select") return;
        event.currentTarget.focus();
        const point = screenToSketch(local(event), current, size);
        if (!anchor) { setView(current); setAnchor(point); setCursor(point); setError(""); return; }
        try {
          const entity = drawnEntity(tool, anchor, point);
          state.updateDraft([{ op: "add_entity", entity }]);
          setAnchor(null); setCursor(null); setTool("select"); setError("");
        } catch (cause) { setError((cause as Error).message); }
      }}
      onPointerMove={event => {
        if (pan.current?.id === event.pointerId) {
          const dx = event.clientX - pan.current.x, dy = event.clientY - pan.current.y;
          setView(v => panSketch(v ?? current, dx, dy));
          pan.current = { id: event.pointerId, x: event.clientX, y: event.clientY };
        } else if (anchor) setCursor(screenToSketch(local(event), current, size));
      }}
      onPointerUp={event => { if (pan.current?.id === event.pointerId) { pan.current = null; event.currentTarget.releasePointerCapture(event.pointerId); } }}
      onPointerCancel={() => { pan.current = null; setAnchor(null); setCursor(null); }}>
      <defs><pattern id="sketch-grid" x={origin.x % pixels} y={origin.y % pixels} width={pixels} height={pixels} patternUnits="userSpaceOnUse">
        <path d={`M ${pixels} 0 L 0 0 0 ${pixels}`} fill="none" stroke="#303944" strokeWidth="1" />
      </pattern></defs>
      {state.showGrid && <rect width="100%" height="100%" fill="url(#sketch-grid)" pointerEvents="none" />}
      <path d={`M 0 ${origin.y} H ${size.width} M ${origin.x} 0 V ${size.height}`} stroke="#617083" strokeDasharray="5 5" pointerEvents="none" />
      <text x={origin.x + 7} y={origin.y - 7} fill="#94a3b8" fontSize="12" pointerEvents="none">0,0</text>
      <g transform={transform}>{entities.map((entity, index) => {
        const label = index === 0 ? "Outer" : `Opening ${index}`;
        const color = conflictingEntities.has(entity.id) ? "#fb7185" : entity.id === selected?.id ? "#38bdf8" : related.has(entity.id) ? "#c4b5fd" : "#94a3b8";
        const select = () => { if (!disabled && tool === "select") state.selectDraftEntity(entity.id); };
        const props = { fill: entity.operation === "subtract" ? "#131820" : "#334155", stroke: color, strokeWidth: entity.id === selected?.id ? 3 : 2,
          vectorEffect: "non-scaling-stroke", role: "button", "aria-label": `Select ${label}`, "aria-pressed": entity.id === selected?.id,
          tabIndex: disabled || tool !== "select" ? -1 : 0, style: { cursor: tool === "select" ? "pointer" : "crosshair" },
          onClick: select, onKeyDown: (event: React.KeyboardEvent) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); select(); } } };
        return entity.kind === "rectangle" ? <rect key={entity.id} {...props} x={entity.x - entity.width! / 2} y={entity.y - entity.height! / 2} width={entity.width!} height={entity.height!} />
          : <circle key={entity.id} {...props} cx={entity.x} cy={entity.y} r={entity.diameter! / 2} />;
      })}
      {ghost && <g fill="none" stroke="#38bdf8" strokeDasharray={`${6 / current.scale} ${4 / current.scale}`} pointerEvents="none">
        {ghost.kind === "rectangle" ? <rect x={ghost.x! - ghost.width / 2} y={ghost.y! - ghost.height / 2} width={ghost.width} height={ghost.height} strokeWidth={2 / current.scale} />
          : <circle cx={ghost.x} cy={ghost.y} r={ghost.diameter / 2} strokeWidth={2 / current.scale} />}
      </g>}</g>
      {selected && tool === "select" && (() => {
        const center = sketchToScreen(selected, current, size);
        const w = (selected.width ?? selected.diameter ?? 0) * current.scale / 2;
        const h = (selected.height ?? selected.diameter ?? 0) * current.scale / 2;
        const variables = selected.kind === "rectangle" ? ["width", "height", "x", "y"] as const : ["diameter", "x", "y"] as const;
        return variables.map((parameter, i) => {
          const constraints = draft.definition.constraints.filter(c => c.kind === "dimension" && c.parameter === parameter
            && ((parameter === "x" || parameter === "y") ? related.has(c.entity_id) : c.entity_id === selected.id));
          const constraint = constraints[0];
          const binding = constraint && draft.relationships.bindings.find(b => b.feature_id === draft.profile_id && b.constraint_id === constraint.id);
          const x = Math.max(100, Math.min(size.width - 100, center.x));
          const y = Math.max(22, Math.min(size.height - 80, center.y + h + 30)) + i * 22;
          const label = `${parameter}: ${Number(Number(selected[parameter]).toPrecision(6))} mm${binding ? " (bound)" : constraint ? "" : " (free)"}`;
          const focus = () => {
            if (disabled) return;
            state.selectDraftEntity(constraint?.kind === "dimension" ? constraint.entity_id : selected.id,
              constraint ? `constraint-${constraint.id}` : `seed-${parameter}`);
          };
          return <g key={parameter} role="button" aria-label={`Edit ${parameter} dimension`} tabIndex={disabled ? -1 : 0} style={{ cursor: "pointer" }}
            onClick={focus} onKeyDown={e => { if (["Enter", " "].includes(e.key)) { e.preventDefault(); focus(); } }}>
            {i === 0 && <path d={`M ${center.x - w} ${center.y + h} v 10 H ${center.x + w} v -10`} stroke="#38bdf8" fill="none" />}
            <rect x={x - 98} y={y - 15} width={196} height={21} rx={3} fill="#17212e" />
            <text x={x} y={y} fill={binding ? "#c4b5fd" : constraint ? "#e2e8f0" : "#7dd3fc"} textAnchor="middle" fontSize="12">{label}</text>
          </g>;
        });
      })()}
    </svg>
    <div className="pointer-events-none absolute bottom-2 left-3 text-11 text-ink-3">Grid {spacing.toPrecision(3)} mm · wheel zoom · middle/right drag pan · F fit</div>
    {tool !== "select" && <p className="pointer-events-none absolute left-3 top-3 rounded bg-chrome p-2 text-12 text-accent">
      {anchor ? "Choose the second point" : tool === "rectangle" ? "Choose the first corner" : "Choose the centre"} · Esc cancels</p>}
    {error && <p role="alert" className="absolute bottom-10 left-3 right-3 rounded bg-chrome p-2 text-12 text-red">{error}</p>}
  </div>;
}
