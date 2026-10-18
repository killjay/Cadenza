import type { Ledger, PreviewChange } from "./types";

export function errorExplanation(code?: string): string {
  const explanations: Record<string, string> = {
    AGENT_UNAVAILABLE: "The AI service is unavailable. Check the server's AI configuration or try the request again later.",
    AGENT_TIMEOUT: "The AI request took too long. Try a smaller, more specific change.",
    AGENT_INVALID_OUTPUT: "The AI response could not be used. Try describing the change more specifically.",
    AGENT_REFUSED: "The AI could not fulfil this request. Review the details and try a different description.",
    AGENT_NEEDS_CLARIFICATION: "More information is needed. Review the clarification in the conversation or details below.",
    LEDGER_INVALID: "The proposed design did not pass validation and could not be applied. Check the requested dimensions and features.",
    PATCH_INVALID: "The proposed edit could not be applied. Try a smaller change.",
    PATCH_FORBIDDEN_PATH: "This edit attempts to change something that cannot be edited this way.",
    PATCH_UNKNOWN_TARGET: "The item to edit could not be found. Select it again before requesting the change.",
    PATCH_APPLY_FAILED: "The proposed edit could not be applied to the current design.",
    REVISION_CONFLICT: "The design changed while this request was being prepared. Review the refreshed model before trying again.",
    GEOMETRY_UNSUPPORTED_FEATURE: "This modelling operation is not supported yet. Review the details for the affected feature.",
    GEOMETRY_INVALID_PARAMETER: "The geometry could not be built with these dimensions. Check the feature values and placement.",
    GEOMETRY_KERNEL_FAILURE: "The geometry operation failed. Check the affected features or try a simpler change.",
    GEOMETRY_EMPTY_RESULT: "This operation leaves no solid geometry. Check the dimensions and cuts.",
    GEOMETRY_EXPORT_FAILURE: "The model could not be exported. Review the details before trying again.",
    GEOMETRY_TIMEOUT: "The geometry calculation took too long. Try a smaller change.",
    BUSY: "Another operation is still running. Wait for it to finish before trying again.",
    UNKNOWN_SESSION: "The server could not find this session. Reconnect, then open your saved project if needed.",
    PROTOCOL_MISMATCH: "The frontend and backend versions are incompatible. Run matching versions before continuing.",
    BAD_MESSAGE: "The server could not understand this request. Check that the frontend and backend versions match.",
    NO_HIT: "No selectable surface was found at that point. Try another surface or use the feature tree.",
    MODEL_DISPLAY: "The model could not be displayed. Review the technical details; this does not confirm a design change.",
    STEP_LOAD: "The detailed surface model could not be loaded. Displayed geometry may still be visible, but precise surface selection may be unavailable.",
  };
  return explanations[code ?? ""] ?? "The operation could not be completed. Review the technical details before trying again.";
}

export const fieldLabel = (field: string): string => {
  const labels: Record<string, string> = { origin: "position", rotation_deg: "rotation", target_feature: "pattern seed",
    sub_assembly_id: "source part", source_ledger_id: "source part", component_a: "first instance", component_b: "second instance",
    sweep_angle: "sweep angle", count: "total instances, including original", suppressed: "suppressed", ai_context: "design notes" };
  return field.split(/[/.]/).filter(part => part && part !== "parameters").map(part =>
    /^(feat|prj|comp|joint)_[a-z0-9_]+$/.test(part) ? "selected item" : labels[part] ?? part.replaceAll("_", " ")).join(" / ");
};
const lengthFields = new Set(["length", "width", "height", "diameter", "depth", "radius", "amount", "spacing", "origin", "x", "y", "z"]);
const angleFields = new Set(["rotation_deg", "sweep_angle", "pressure_angle_deg"]);
export function assumptionValue(field: string, value: unknown): string {
  const key = field.split(/[/.]/).at(-1) ?? "";
  const text = value !== null && typeof value === "object" ? JSON.stringify(value) : String(value);
  return text + (typeof value === "number" || Array.isArray(value) && value.every(item => typeof item === "number")
    ? lengthFields.has(key) ? " mm" : angleFields.has(key) ? " degrees" : "" : "");
}
const references: Record<string, string> = { target_feature: "feature", profile: "profile", profile_id: "profile",
  sub_assembly_id: "part", source_ledger_id: "part", ledger_id: "part", part_id: "part", source_part_id: "part", feature_id: "feature",
  reference_feature: "feature", component_id: "instance", component_a: "instance", component_b: "instance", joint_id: "attachment" };

/** Names are resolved only in structured fields, never by rewriting user or agent prose. */
export function previewPresentation(ledgers: Record<string, Ledger>, changes: PreviewChange[], ledgerId: string) {
  const names = new Map<string, string>();
  const ledger = ledgers[ledgerId];
  for (const part of Object.values(ledgers)) names.set(part.project_id, part.metadata.name || "Part");
  const remember = (value: unknown) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return;
    const item = value as { id?: string; name?: string; project_id?: string; metadata?: { name?: string } };
    if (item.id && item.name) names.set(item.id, item.name);
    if (item.project_id && item.metadata?.name) names.set(item.project_id, item.metadata.name);
  };
  ledger?.features.forEach(remember);
  ledger?.assembly?.components.forEach(remember);
  ledger?.assembly?.parts?.forEach(remember);
  for (const change of changes) {
    if (change.path === "") {
      remember(change.before); remember(change.after);
    }
    if (change.ledger_id !== ledgerId) continue;
    remember(change.before); remember(change.after);
    if (change.path === "" || change.path === "/assembly") {
      for (const payload of [change.before, change.after]) {
        const definition = payload as Ledger | null;
        definition?.features?.forEach(remember);
        definition?.assembly?.components.forEach(remember);
        (payload as Ledger["assembly"])?.components?.forEach(remember);
      }
    }
    const path = change.path.split("/").filter(Boolean);
    if (path.at(-1) === "name" && typeof change.after === "string") {
      if (path[0] === "assembly" && path[1] === "components" && path.length === 4) names.set(path[2], change.after);
      if (path[0] === "features" && path.length === 3) names.set(path[1], change.after);
    }
  }
  const value = (input: unknown, key = ""): string => {
    if (input === null || input === undefined) return "none";
    if ((key === "kind" || key === "operation") && typeof input === "string") return fieldLabel(input);
    if (key === "order" && typeof input === "string") return names.get(input) ?? "Unresolved item";
    if (references[key] && typeof input === "string") return names.get(input) ?? `Unresolved ${references[key]}`;
    if (Array.isArray(input)) return input.map(entry => value(entry, key)).join(", ");
    if (typeof input === "object") {
      const object = input as Record<string, unknown>;
      return Object.entries(object).filter(([name, entry]) => entry !== null && !["id", "project_id", "ai_context"].includes(name)
        && !(object.kind === "circular_pattern" && name === "parameters")).map(([name, entry]) => `${fieldLabel(name)}: ${value(entry, name)}`)
        .concat(object.kind === "circular_pattern" ? [value(Object.fromEntries(Object.entries(object.parameters as object).filter(([name]) => name !== "radius"))), "radius follows the seed position"] : []).join("; ");
    }
    return `${typeof input === "boolean" ? input ? "yes" : "no" : String(input)}${typeof input === "number" ? lengthFields.has(key) ? " mm" : angleFields.has(key) ? " degrees" : "" : ""}`;
  };
  const changeLabel = (change: PreviewChange): string => {
    const parts = change.path.split("/").filter(Boolean);
    const feature = parts[0] === "features";
    const assembly = parts[0] === "assembly" && ["components", "joints"].includes(parts[1]);
    const id = feature ? parts[1] : assembly ? parts[2] : undefined;
    const tail = parts.slice(feature ? 2 : assembly ? 3 : 0).filter(part => part !== "parameters");
    return [names.get(ledgerId) || "Part", id ? names.get(id) ?? (feature ? "Feature" : parts[1] === "joints" ? "Attachment" : "Instance") : "",
      ...tail.map(fieldLabel)].filter(Boolean).join(" / ");
  };
  return { value, changeLabel };
}
