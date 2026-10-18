import { useState } from "react";
import { NumericInput } from "./NumericInput";
import { useStore } from "../lib/store";

export function PartCreate() {
  const state = useStore();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"box" | "cylinder">("box");
  const blocked = !state.partCapabilities || state.busy || !!state.preview || !!state.draft || state.draftStarting
    || state.connection !== "open" || state.model?.source === "local";
  return <>
      <p className="mt-1">Give each reusable part its own name. Adding a feature to an existing part does not create a separate assembly source.</p>
      <form className="mt-2 space-y-2" onSubmit={event => {
        event.preventDefault();
        const data = new FormData(event.currentTarget);
        const placement = { origin: [0, 0, 0] as [number, number, number], rotation_deg: [0, 0, 0] as [number, number, number], relative_to: null };
        const height = Number(data.get("height"));
        state.createPart(name, [kind === "box"
          ? { op: "add_feature", kind, name: "Stock", operation: "add", placement,
            parameters: { length: Number(data.get("length")), width: Number(data.get("width")), height } }
          : { op: "add_feature", kind, name: "Stock", operation: "add", placement,
            parameters: { diameter: Number(data.get("diameter")), height } }]);
      }}>
        <fieldset disabled={blocked || state.sourceLoading} className="space-y-2">
          <label className="block">Part name <input aria-label="New part name" required maxLength={100} value={name}
            onChange={e => setName(e.target.value)} className="w-full rounded border border-hairline bg-chrome p-1" /></label>
          <label className="block">Starting shape <select aria-label="Starting shape" value={kind}
            onChange={e => setKind(e.target.value as "box" | "cylinder")} className="ml-2 bg-chrome">
            <option value="box">Box</option><option value="cylinder">Cylinder</option>
          </select></label>
          {(kind === "box" ? ["length", "width", "height"] : ["diameter", "height"]).map(field =>
            <label key={field} className="block">{field} (mm) <NumericInput aria-label={`New part ${field}`} name={field}
              min="0.000001" step="any" required defaultValue={field === "height" ? 5 : 30}
              className="w-full rounded border border-hairline bg-chrome p-1" /></label>)}
          <p className="text-ink-4">Centred in X/Y, base at Z=0. Position can be edited after Apply.</p>
          <button className="text-accent disabled:opacity-40" type="submit" disabled={!name.trim()}>Preview new part</button>
          <button className="ml-3 text-accent disabled:opacity-40" type="button" disabled={!name.trim() || !state.agentsAvailable}
            onClick={() => state.startPartPrompt(name)}>Create with AI</button>
        </fieldset>
      </form>
  </>;
}
