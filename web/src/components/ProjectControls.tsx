import { useState } from "react";
import { useStore } from "../lib/store";

interface ProjectEntry { id: string; name: string; updated_at: string; }

async function request(path: string, body?: unknown) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail ?? "Project request failed.");
  return data;
}

export function ProjectControls() {
  const state = useStore();
  const [panel, setPanel] = useState<"save" | "open" | null>(null);
  const [name, setName] = useState("");
  const [projects, setProjects] = useState<ProjectEntry[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const disabled = pending || state.sourceLoading || state.busy || !!state.preview || state.connection !== "open";

  async function run(action: () => Promise<void>) {
    if (useStore.getState().sourceLoading || useStore.getState().busy || useStore.getState().preview || pending) return;
    setPending(true); setError("");
    useStore.setState({ busy: true });
    try { await action(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setPending(false); useStore.setState({ busy: false }); }
  }

  return <div className="relative flex items-center gap-2 text-11">
    <button disabled={disabled || state.model?.source === "local"} onClick={() => {
      setName(state.projectName); setError(""); setPanel("save");
    }}>Save project</button>
    {state.draft && <span className="text-yellow">Saves accepted design only; draft is temporary.</span>}
    <button disabled={disabled || !!state.draft} title={state.draft ? "Finish or discard the sketch before opening another project" : undefined} onClick={() => { setPanel("open"); void run(async () => {
      setProjects(await request("/api/projects"));
    }); }}>Open project</button>
    <span aria-live="polite" className="text-ink-4">{state.savedRevision === state.sessionRevision ? "Saved" : "Unsaved changes"}</span>
    {panel && <section aria-label={panel === "save" ? "Save project" : "Open project"}
      className="absolute left-0 top-8 z-50 w-80 rounded border border-hairline bg-chrome p-3 shadow-lg">
      {panel === "save" ? <form onSubmit={event => { event.preventDefault(); void run(async () => {
        const current = useStore.getState();
        const saved = await request(`/api/sessions/${current.sessionId}/save`, { name, base_revision: current.sessionRevision });
        if (useStore.getState().sessionId === current.sessionId)
          useStore.setState({ savedRevision: saved.revision, projectName: saved.name });
        setPanel(null);
      }); }}>
        <label>Project name<input className="my-2 w-full border border-hairline bg-wash p-2" value={name}
          maxLength={200} required onChange={event => setName(event.target.value)} /></label>
        <button disabled={disabled || !name.trim()}>Save</button>
      </form> : <div className="max-h-72 overflow-auto">
        <p className="mb-2">Open replaces the current view. Save your changes first.</p>
        {!pending && projects.length === 0 && <p>No saved projects yet.</p>}
        {projects.map(project => <button key={project.id} disabled={disabled || !!state.draft}
          className="block w-full p-2 text-left hover:bg-wash" onClick={() => void run(async () => {
            if (useStore.getState().draft) return;
            const opened = await request(`/api/projects/${project.id}/open`, {});
            useStore.setState({ sessionId: opened.session_id });
            useStore.getState().connect();
            setPanel(null);
          })}>{project.name}<small className="block text-ink-4">{project.updated_at}</small></button>)}
      </div>}
      {pending && <p role="status">Working…</p>}
      {error && <p role="alert" className="mt-2">{error}</p>}
      <button className="mt-2" disabled={pending} onClick={() => setPanel(null)}>Close</button>
    </section>}
  </div>;
}
