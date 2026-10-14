import { useStore } from "../lib/store";
import type { Ledger, PreviewChange } from "../lib/types";

const label = (text: string) => text.replaceAll("_", " ");
const lengths = new Set(["length", "width", "height", "diameter", "depth", "radius", "amount", "spacing"]);

function valueLabel(value: unknown): string {
  if (value === null) return "none";
  if (Array.isArray(value)) return value.map(valueLabel).join(", ");
  if (typeof value === "object" && !Array.isArray(value)) {
    const definition = value as { project_id?: string; metadata?: { name: string }; assembly?: { components: unknown[] } };
    if (definition.project_id && definition.assembly) return `New shared definition ${definition.metadata?.name}: ${definition.assembly.components.map(valueLabel).join("; ")}`;
    const attachment = value as { id?: string; component_a?: string; component_b?: string; target_a?: unknown; target_b?: unknown };
    if (attachment.id?.startsWith("joint_")) return `${attachment.component_a} → ${attachment.component_b}; A ${valueLabel(attachment.target_a)}; B ${valueLabel(attachment.target_b)}`;
    const instance = value as { id?: string; name?: string; sub_assembly_id?: string; part_id?: string;
      components?: unknown[]; placement?: { origin: number[]; rotation_deg: number[] } };
    if (instance.components) return `Assembly: ${instance.components.map(valueLabel).join("; ")}`;
    if (instance.id?.startsWith("comp_") && instance.placement) return `${instance.name || instance.id}`
      + `; source ${instance.sub_assembly_id || instance.part_id || "catalog"}`
      + `; position (${instance.placement.origin.join(", ")}) mm; rotation (${instance.placement.rotation_deg.join(", ")}) degrees`;
    const feature = value as { name?: string; kind?: string; parameters?: unknown; placement?: { origin: number[]; rotation_deg: number[] } };
    if (feature.kind === "circular_pattern") {
      const { radius: _compatibilityRadius, ...settings } = feature.parameters as Record<string, unknown>;
      return `${feature.name || "Circular pattern"}: ${valueLabel(settings)}; center (${feature.placement?.origin.slice(0, 2).join(", ")}) mm; radius follows seed position`;
    }
    if (feature.kind) return `${feature.name || label(feature.kind)}: ${valueLabel(feature.parameters)}`
      + (feature.placement ? `; position (${feature.placement.origin.join(", ")}) mm; rotation (${feature.placement.rotation_deg.join(", ")})°` : "");
    return Object.entries(value).filter(([, entry]) => entry !== null).map(([key, entry]) =>
      `${key === "count" ? "total instances, including seed" : label(key)}: ${valueLabel(entry)}${lengths.has(key) ? " mm" : ""}`).join("; ");
  }
  return typeof value === "boolean" ? value ? "yes" : "no" : String(value);
}

function changeLabel(change: PreviewChange, ledger?: Ledger): string {
  const parts = change.path.split("/").filter(Boolean);
  const feature = parts[0] === "features" ? ledger?.features.find(feature => feature.id === parts[1]) : undefined;
  if (parts[0] === "assembly" && (parts[1] === "components" || parts[1] === "joints")) {
    const item = ledger?.assembly?.[parts[1]].find(item => item.id === parts[2]);
    const name = item && "name" in item ? item.name : parts[2];
    return [ledger?.metadata.name ?? "Assembly", parts[1] === "components" ? "Instance" : "Attachment",
      name || parts[2], ...parts.slice(3).map(label)].join(" / ")
      + (parts.includes("origin") ? " (mm)" : parts.includes("rotation_deg") ? " (degrees)" : "");
  }
  const location = feature ? [feature.name || label(feature.kind), ...parts.slice(2).filter(part => part !== "parameters")]
    : parts[0] === "features" && parts.length === 2 ? [change.before === null ? "Added feature" : "Removed feature"] : parts;
  return [ledger?.metadata.name ?? "Part", ...location.map(label)].join(" / ")
    + (lengths.has(parts.at(-1) ?? "") || parts.includes("origin") ? " (mm)" : "");
}

export function EditPreview() {
  const state = useStore();
  const preview = state.preview;
  if (!preview) return null;
  const disabled = state.busy || state.connection !== "open";
  return <section aria-label="Edit preview" className="flex max-h-80 shrink-0 flex-col border-t border-accent p-2.5 text-11">
    <h2 className="font-semibold text-accent">Preview — not applied</h2>
    <div className="min-h-0 overflow-auto">
    <p className="my-1">{preview.summary}</p>
    <p className="text-ink-4">Based on revision {preview.base_revision}. Apply to keep these changes.</p>
    <ul className="my-2 space-y-2">
      {preview.changes.map((change, i) => <li key={i} className="break-words rounded bg-wash p-1.5">
        <div className="text-ink-4">{changeLabel(change, state.ledgers[change.ledger_id])}</div>
        <div>{valueLabel(change.before)} → {valueLabel(change.after)}</div>
      </li>)}
    </ul>
    {preview.display_ledger_id && <p className="my-1 text-ink-3">Showing source part preview. Apply validates and updates the complete project.</p>}
    {preview.assumptions.map((assumption, i) => <p key={i} className="my-1 text-yellow">
      Assumption: {assumption.field} = {valueLabel(assumption.value)} ({assumption.basis})</p>)}
    <ul className="my-2 space-y-1 text-ink-3">{preview.checks.map((check, i) => <li key={i}>{check}</li>)}</ul>
    {state.previewLoading && <p role="status">Loading candidate geometry…</p>}
    {state.previewError && <p role="alert">{state.previewError}</p>}
    </div>
    <div className="mt-2 flex shrink-0 gap-3 border-t border-hairline pt-2">
      <button className="text-accent disabled:opacity-40" disabled={disabled || state.previewLoading || !!state.previewError}
        onClick={() => state.decidePreview("apply")}>Apply</button>
      <button disabled={disabled} onClick={() => state.decidePreview("cancel")}>{preview.draft_id ? "Back to sketch" : "Cancel"}</button>
      {preview.draft_id && <button disabled={disabled} onClick={() => state.actOnDraft("discard")}>Discard draft</button>}
      {preview.prompt && <button disabled={disabled} onClick={() => state.decidePreview("cancel", true)}>Revise request</button>}
    </div>
  </section>;
}
