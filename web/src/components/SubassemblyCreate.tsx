import { useState } from "react";
import type { AssemblyOperation, Ledger } from "../lib/types";
import { PlacementFields, readPlacement, readName, inputClass, buttonClass, zero } from "./AssemblyFields";

export function SubassemblyCreate({ sources, initialize, disabled, run }: { sources: Ledger[]; initialize: boolean;
  disabled: boolean; run: (action: () => AssemblyOperation[]) => void }) {
  const [children, setChildren] = useState([0]);
  return <details className="mt-2"><summary>Create shared subassembly</summary>
    <p>Create a definition from existing sources and insert one instance atomically. Attach its children after applying.</p>
    <form onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget);
      run(() => [...(initialize ? [{ op: "initialize_assembly" as const, name: readName(data, "project_name") }] : []),
        { op: "create_subassembly", name: readName(data), instance_name: readName(data, "instance_name"), placement: readPlacement(data),
          children: children.map(id => ({ source_ledger_id: String(data.get(`source_${id}`)), name: readName(data, `name_${id}`), placement: readPlacement(data, `child_${id}_`) })) }]); }}>
      <fieldset disabled={disabled || !sources.length} className="space-y-2">
        {initialize && <label>Project assembly name<input name="project_name" aria-label="Project assembly name" required maxLength={100} className={inputClass} /></label>}
        <label>Definition name<input name="name" aria-label="Subassembly definition name" required maxLength={100} className={inputClass} /></label>
        <label>Outer instance name<input name="instance_name" aria-label="Subassembly instance name" required maxLength={100} className={inputClass} /></label>
        <p>Outer placement in the current owner frame</p><PlacementFields placement={zero} />
        {children.map((id, index) => <fieldset key={id} className="rounded border border-hairline p-1"><legend>Child {index + 1} in the new definition frame</legend>
          <label>Source<select name={`source_${id}`} aria-label={`Child ${index + 1} source`} className={inputClass}>
            {sources.map(l => <option key={l.project_id} value={l.project_id}>{l.metadata.name} · {l.assembly ? "Assembly" : "Part"} ({l.project_id.slice(-6)})</option>)}
          </select></label>
          <label>Child name<input name={`name_${id}`} aria-label={`Child ${index + 1} name`} required maxLength={100} className={inputClass} /></label>
          <PlacementFields placement={zero} prefix={`child_${id}_`} />
          <button type="button" disabled={children.length === 1} className={buttonClass} onClick={() => setChildren(children.filter(c => c !== id))}>Remove child</button>
        </fieldset>)}
        <button type="button" disabled={children.length >= 16} className={buttonClass} onClick={() => setChildren([...children, Math.max(...children) + 1])}>Add child</button>
        <button className={`ml-2 ${buttonClass}`}>Preview subassembly</button>
      </fieldset>
    </form>
  </details>;
}
