import { useEffect, useState } from "react";
import { useStore } from "../lib/store";
import { browserEvidence, diagnosticExport, diagnosticStatus, downloadDiagnostic, recordDiagnostic } from "../lib/diagnostics";
import type { DiagnosticStatus } from "../lib/diagnostics";

export function DiagnosticsControls() {
  const sessionId = useStore(s => s.sessionId), revision = useStore(s => s.sessionRevision);
  const [open, setOpen] = useState(false), [pending, setPending] = useState(false);
  const [status, setStatus] = useState<DiagnosticStatus | null>(null);
  const [selected, setSelected] = useState("");
  const [images, setImages] = useState(false), [error, setError] = useState("");
  useEffect(() => {
    const failed = (event: ErrorEvent | PromiseRejectionEvent) => {
      const cause = event instanceof ErrorEvent ? event.error : event.reason;
      recordDiagnostic("browser.unhandled_error", { session_id: sessionId, error_type: cause instanceof Error ? cause.name : "UnknownError" });
    };
    window.addEventListener("error", failed); window.addEventListener("unhandledrejection", failed);
    return () => { window.removeEventListener("error", failed); window.removeEventListener("unhandledrejection", failed); };
  }, [sessionId]);
  async function refresh() {
    setPending(true); setError("");
    try {
      const next = await diagnosticStatus(); setStatus(next);
      setSelected(current => next.sessions.some(s => s.session_id === current) ? current
        : next.sessions.find(s => s.session_id === sessionId)?.session_id ?? next.sessions[0]?.session_id ?? "");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not read diagnostics"); }
    finally { setPending(false); }
  }
  async function download() {
    setPending(true); setError("");
    try { downloadDiagnostic(await diagnosticExport(selected, images), `cadenza-${selected}.zip`); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Could not export diagnostics"); }
    finally { setPending(false); }
  }
  const current = status?.sessions.find(s => s.session_id === selected);
  return <div className="relative">
    <button className="rounded-sm px-1.5 py-px hover:bg-wash" onClick={() => {
      setOpen(!open); if (!open) void refresh();
    }}>Diagnostics{status && (!status.healthy || !status.enabled) ? " ⚠" : ""}</button>
    {open && <section aria-label="Diagnostics" className="absolute bottom-7 right-0 z-50 max-h-[70vh] w-96 max-w-[90vw] overflow-auto rounded border border-hairline bg-chrome p-3 text-11 text-ink-2 shadow-lg">
      <p>Current session: {sessionId ?? "none"} · revision {revision}</p>
      {status && <p role="status">{!status.enabled ? "Recording disabled" : status.healthy ? "Recording locally" : "Recording degraded"}
        {` · ${status.omitted_events} omitted events`}</p>}
      <p className="my-2">Reports contain local prompts and design snapshots. Review before sharing. Old or incomplete records may be missing.</p>
      <label>Retained session<select aria-label="Diagnostic session" className="my-2 w-full bg-wash" value={selected} onChange={e => setSelected(e.target.value)}>
        <option value="">Select a session</option>
        {status?.sessions.map(s => <option key={s.session_id} value={s.session_id}>{s.session_id} ({s.events} events)</option>)}
      </select></label>
      <ul className="my-2 max-h-32 overflow-auto">{current?.requests.slice(-12).map((r, i) =>
        <li key={`${r.trace_id}-${i}`}>{r.message_id ?? r.trace_id.slice(0, 8)}: {r.event} {r.data.outcome ?? ""}</li>)}</ul>
      <label className="block"><input type="checkbox" checked={images} onChange={e => setImages(e.target.checked)} /> Include drawings</label>
      {status && !status.capture_images && <p>Drawing capture is disabled; uncaptured images cannot be recovered.</p>}
      <div className="my-2 flex flex-wrap gap-3">
        <button disabled={pending || !selected} onClick={() => void download()}>Download diagnostics</button>
        <button disabled={pending} onClick={() => void refresh()}>Refresh</button>
        <button onClick={() => downloadDiagnostic(new Blob([JSON.stringify(browserEvidence(), null, 2)], { type: "application/json" }), "cadenza-browser-only.json")}>Download browser-only</button>
      </div>
      <p>Browser-only evidence is partial and resets on page reload.</p>
      {pending && <p role="status">Working…</p>}
      {error && <p role="alert">{error}</p>}
      <button className="mt-2" onClick={() => setOpen(false)}>Close</button>
    </section>}
  </div>;
}
