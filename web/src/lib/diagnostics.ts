/** Bounded browser evidence, separate from authoritative backend records. */
type Entry = { timestamp: string; event: string; data: Record<string, unknown> };
const trail: Entry[] = [];
let omitted = 0;
let trailBytes = 0;
const fields = ["type", "message_id", "session_id", "revision", "base_revision", "ledger_id", "feature_id",
  "preview_id", "action", "code", "phase", "owner_path", "component_path", "face_mode", "target", "level", "state", "error_type"];

export function recordDiagnostic(event: string, value: unknown = {}): void {
  try {
    const raw = value && typeof value === "object" ? value as Record<string, unknown> : {};
    const data: Record<string, unknown> = {};
    for (const key of fields) {
      if (!(key in raw)) continue;
      const text = JSON.stringify(raw[key]);
      if (text && text.length <= 500) data[key] = JSON.parse(text);
    }
    const face = raw.face as Record<string, unknown> | undefined;
    if (face && typeof face === "object") {
      if (face.mode === "edit_feature" || face.mode === "add_on_face") data.face_mode = face.mode;
      if (Array.isArray(face.component_path) && face.component_path.length <= 8
        && face.component_path.every(id => typeof id === "string" && /^comp_[a-z0-9_]{1,60}$/.test(id))) data.component_path = [...face.component_path];
    }
    const entry = { timestamp: new Date().toISOString(), event: event.slice(0, 80), data };
    trail.push(entry);
    trailBytes += new TextEncoder().encode(JSON.stringify(entry)).length;
    while (trail.length > 200 || trailBytes > 128 * 1024) {
      trailBytes -= new TextEncoder().encode(JSON.stringify(trail.shift())).length;
      omitted++;
    }
  } catch { omitted++; }
}

export function browserEvidence() {
  return { partial: true, omitted, limitation: "Browser metadata only; resets on page reload. No prompt/image or server snapshot payloads.",
    events: trail.map(e => ({ ...e, data: { ...e.data } })) };
}

export function downloadDiagnostic(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob), anchor = document.createElement("a");
  anchor.href = url; anchor.download = name; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export interface DiagnosticStatus {
  enabled: boolean; healthy: boolean; omitted_events: number; last_failure: string | null; capture_images: boolean;
  sessions: { session_id: string; last: string | null; events: number; missing: string[];
    requests: { trace_id: string; message_id: string | null; event: string; data: { outcome?: string } }[] }[];
}

export async function diagnosticStatus(): Promise<DiagnosticStatus> {
  const response = await fetch("/api/diagnostics/status", { cache: "no-store" });
  if (!response.ok) throw new Error("Backend diagnostics unavailable. Browser-only download is still available.");
  return response.json();
}

export async function diagnosticExport(sessionId: string, includeImages: boolean): Promise<Blob> {
  const evidence = browserEvidence();
  const response = await fetch(`/api/diagnostics/sessions/${encodeURIComponent(sessionId)}/export`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ include_images: includeImages, browser: evidence.events, browser_omitted: evidence.omitted }),
  });
  if (!response.ok) throw new Error("Diagnostic export failed. Try refresh or download browser-only evidence.");
  return response.blob();
}
