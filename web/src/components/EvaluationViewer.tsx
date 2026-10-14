import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { createViewport, type Viewport } from "../viewport/scene";
import { disposeSubtree, parseGlb, swapModel } from "../viewport/model";
import { activeSelection, autoPhase, batchStatus, recordedDecision, recordedSteps, watchRuns,
  type ModelPhase, type RunStates, type Selection } from "../lib/evaluation-view";
import "./EvaluationViewer.css";

export default function EvaluationViewer({ runs }: { runs: string[] }) {
  const host = useRef<HTMLDivElement>(null);
  const viewport = useRef<Viewport | null>(null);
  const fittedTarget = useRef<string | null>(null);
  const [states, setStates] = useState<RunStates>({});
  const [manual, setManual] = useState<Selection | null>(null);
  const [view, setView] = useState<ModelPhase | "auto">("auto");
  const [modelStatus, setModelStatus] = useState("No geometry yet");
  const [now, setNow] = useState(Date.now());
  const selection = activeSelection(runs, states, manual);
  const run = selection.run;
  const manifest = states[run]?.manifest;
  const connection = states[run]?.error ?? (manifest ? "Connected" : "Waiting for run; check viewer configuration and run ID");
  const checkpoint = selection.sequence === undefined ? manifest?.checkpoints.at(-1)
    : manifest?.checkpoints.find(c => c.sequence === selection.sequence);
  const phase = view === "auto" ? autoPhase(checkpoint) : view;
  const file = checkpoint?.[phase];
  const target = checkpoint?.target;
  const targetKey = `${run}/${target ?? ""}`;
  const age = manifest ? Math.max(0, Math.floor((now - Date.parse(manifest.updated)) / 1000)) : 0;
  const decision = recordedDecision(manifest, checkpoint);
  const response = manifest?.checkpoints.filter(c => c.step === checkpoint?.step && c.sequence <= (checkpoint?.sequence ?? 0)
    && ["clarification", "rejection", "unsupported"].includes(c.phase)).at(-1);
  const steps = recordedSteps(runs, states);
  const stepIndex = steps.findIndex(s => s.run === run && s.step === checkpoint?.step);
  const pin = () => setManual({ run, sequence: checkpoint?.sequence });
  const chooseStep = (index: number) => { setManual(steps[index]); setView("auto"); };
  useEffect(() => watchRuns(runs, setStates), [runs]);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    if (!host.current) return;
    const vp = createViewport(host.current);
    viewport.current = vp;
    const resize = new ResizeObserver(() => vp.resize()); resize.observe(host.current);
    return () => {
      resize.disconnect();
      for (const child of vp.modelRoot.children.slice()) disposeSubtree(child);
      vp.dispose(); viewport.current = null; fittedTarget.current = null;
    };
  }, []);
  useEffect(() => {
    const vp = viewport.current;
    if (!vp) return;
    let obsolete = false;
    const controller = new AbortController();
    if (fittedTarget.current !== targetKey) {
      for (const child of vp.modelRoot.children.slice()) disposeSubtree(child);
    }
    if (!file) {
      for (const child of vp.modelRoot.children.slice()) disposeSubtree(child);
      setModelStatus("No geometry published for this phase");
    } else {
      setModelStatus("Loading selected geometry; previous model may still be visible...");
      const timeout = setTimeout(() => controller.abort(), 15000);
      void (async () => {
        try {
          const response = await fetch(`/api/evaluations/${run}/models/${file}`, { signal: controller.signal });
          if (!response.ok) throw new Error(`Model unavailable (${response.status})`);
          const bytes = await response.arrayBuffer();
          if (bytes.byteLength > 64 * 1024 * 1024) throw new Error("Model exceeds viewer limit");
          const model = await parseGlb(bytes);
          if (obsolete) { disposeSubtree(model.root); return; }
          swapModel(vp, model, fittedTarget.current !== targetKey);
          fittedTarget.current = targetKey;
          setModelStatus(`${phase}: ${model.triangleCount.toLocaleString()} triangles`);
        } catch (error) {
          if (!obsolete) setModelStatus(`${error instanceof Error ? error.message : "Model load failed"}. Previous model, if visible, is stale.`);
        } finally { clearTimeout(timeout); }
      })();
      return () => { obsolete = true; controller.abort(); clearTimeout(timeout); };
    }
    return () => { obsolete = true; controller.abort(); };
  }, [file, run, targetKey, phase]);
  return <main className="evaluation-view">
    <header><strong>CADenza | Live test</strong><a href="/" target="_blank" rel="noreferrer">Open interactive editor</a>
      <span>Read-only observation | {run}</span></header>
    <section className="evaluation-body">
      <aside>
        {runs.length > 1 && <p role="status">{batchStatus(runs, states)}</p>}
        <label>Run<select aria-label="Run" value={run} onChange={e => { setManual({ run: e.target.value }); setView("auto"); }}>
          {runs.map(id => <option key={id} value={id}>{id} - {states[id]?.error ? "observation error" : states[id]?.manifest?.status ?? "waiting"}</option>)}
        </select></label>
        <p role="status">{connection} | {manifest?.status ?? "waiting"}</p>
        {manifest && <p>Last update {age}s ago{manifest.status === "running" && age > 30 ? " - progress unknown; the runner may still be working" : ""}</p>}
        {manifest?.observer_error && <p role="alert">{manifest.observer_error}</p>}
        {manifest?.estimated_usd != null && <p>Estimated inference: ${manifest.estimated_usd.toFixed(6)} (not billed cost)</p>}
        <p>{manual ? "Reviewing recorded evidence; automatic navigation paused" : "Following latest started run"}</p>
        <button onClick={() => { setManual(null); setView("auto"); }} disabled={!manual && view === "auto"}>Resume live</button>
        <div className="evaluation-step-controls">
          <button disabled={stepIndex <= 0} onClick={() => chooseStep(stepIndex - 1)}>Previous step</button>
          <button disabled={stepIndex < 0 || stepIndex >= steps.length - 1} onClick={() => chooseStep(stepIndex + 1)}>Next step</button>
        </div>
        <label>Checkpoint<select aria-label="Checkpoint" value={checkpoint?.sequence ?? ""} onChange={e => { setManual({ run, sequence: Number(e.target.value) }); setView("auto"); }}>
          {!checkpoint && <option value="">Waiting for checkpoints</option>}
          {manifest?.checkpoints.map(c => <option key={c.sequence} value={c.sequence}>{c.sequence}. {c.step} | {c.phase}</option>)}
        </select></label>
        <h2>{checkpoint?.phase ?? "Waiting for test"}</h2>
        <p>Target: {target || "None"}</p>
        <h3>Prompt</h3><p className="evaluation-text">{checkpoint?.prompt || "No prompt yet"}</p>
        <h3>Recorded result / action</h3><p className="evaluation-text">{checkpoint?.detail || "No result yet"}</p>
        {response?.detail && response.detail !== checkpoint?.detail && <p className="evaluation-text">{response.phase}: {response.detail}</p>}
        {decision && <p className="evaluation-decision">{decision}</p>}
        <p>Decisions are performed by the test runner. This page cannot edit or apply changes.</p>
      </aside>
      <section className="evaluation-model">
        <nav>{(["auto", "before", "proposal", "accepted"] as const).map(name => <button key={name} aria-pressed={view === name} onClick={() => { pin(); setView(name); }}>{name[0].toUpperCase() + name.slice(1)}</button>)}
          <button onClick={() => { const vp = viewport.current; if (vp?.modelRoot.children.length) vp.frame(new THREE.Box3().setFromObject(vp.modelRoot), false); }}>Fit</button></nav>
        <p className="evaluation-displayed" data-testid="displayed-phase">{phase === "proposal" && checkpoint?.proposal
          ? checkpoint.proposal === checkpoint.accepted ? "Proposal - same as accepted" : "Proposal - not accepted"
          : phase === "accepted" ? "Accepted model" : phase === "before" ? "Before model" : "No proposal geometry"}</p>
        <p role="status" data-testid="model-status">{modelStatus}</p><div className="evaluation-canvas" ref={host} />
      </section>
    </section>
  </main>;
}
