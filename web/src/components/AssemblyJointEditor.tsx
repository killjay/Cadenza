import { useState } from "react";
import type { Assembly, AssemblyOperation, AssemblyPromptTarget, Joint } from "../lib/types";
import { VectorFields, readVector, inputClass, buttonClass } from "./AssemblyFields";

function JointForm({ assembly, joint, disabled, run, prompt }: { assembly: Assembly; joint?: Joint; disabled: boolean;
  prompt?: (target: AssemblyPromptTarget) => void;
  run: (action: () => AssemblyOperation[]) => void }) {
  const [normals, setNormals] = useState(!!joint?.target_a.normal);
  return <form onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget);
    run(() => {
      const target = (side: string) => {
        const normal = normals ? readVector(data, `normal_${side}`) : null;
        if (normal && !(Math.hypot(...normal) >= 1e-12 && Number.isFinite(Math.hypot(...normal)))) throw new Error("Use nonzero finite normals.");
        return { point: readVector(data, `point_${side}`), normal };
      };
      const a = String(data.get("component_a")), b = String(data.get("component_b"));
      if (a === b) throw new Error("Choose two different instances.");
      return [...(joint ? [{ op: "remove_joint" as const, joint_id: joint.id }] : []),
        { op: "add_rigid_joint", component_a: a, component_b: b, target_a: target("a"), target_b: target("b") }];
    }); }}>
    <fieldset disabled={disabled || assembly.components.length < 2} className="space-y-2">
      <label className="block"><input type="checkbox" checked={normals} onChange={e => setNormals(e.target.checked)} /> Oppose both outward normals</label>
      {(["a", "b"] as const).map((side, i) => <div key={side}>
        <label>{side === "a" ? "A anchors" : "B follows"}<select aria-label={`Attachment ${side.toUpperCase()}`} name={`component_${side}`}
          defaultValue={joint?.[`component_${side}`] ?? assembly.components[i]?.id} className={inputClass}>
          {assembly.components.map(c => <option key={c.id} value={c.id}>{c.name || c.id} ({c.id.slice(-6)})</option>)}
        </select></label>
        <VectorFields name={`point_${side}`} label={`${side.toUpperCase()} anchor (mm)`} value={joint?.[`target_${side}`].point ?? [0,0,0]} />
        {normals && <VectorFields name={`normal_${side}`} label={`${side.toUpperCase()} outward normal`} value={joint?.[`target_${side}`].normal ?? [0,0,i ? -1 : 1]} />}
      </div>)}
      <button className={buttonClass}>{joint ? "Preview attachment update" : "Preview attachment"}</button>
      {!joint && <button type="button" className={buttonClass} disabled={!prompt} onClick={event => {
        const data = new FormData(event.currentTarget.form!);
        prompt?.({ kind: "attachment", component_a: String(data.get("component_a")), component_b: String(data.get("component_b")) });
      }}>Create attachment with AI</button>}
    </fieldset>
  </form>;
}

export function AssemblyJointEditor({ assembly, disabled, run, prompt }: { assembly: Assembly; disabled: boolean;
  prompt?: (target: AssemblyPromptTarget) => void;
  run: (action: () => AssemblyOperation[]) => void }) {
  const name = (id: string) => assembly.components.find(c => c.id === id)?.name || id;
  return <details className="mt-2"><summary>Rigid attachments ({assembly.joints.length})</summary>
    <p>Coordinates use this assembly definition’s frame after stored placement, before attachments solve. Viewport picks are not converted.
      A anchors B. Stored rotation supplies twist. These are fixed coordinate attachments, not face-tracking mates.</p>
    {assembly.joints.map(j => <details key={j.id} className="my-2 border-t border-hairline pt-1">
      <summary>{name(j.component_a)} → {name(j.component_b)} · {j.target_a.normal ? "Opposed normals" : "Translation only"}</summary>
      <p className="break-all text-ink-4">{j.id}</p>
      <JointForm assembly={assembly} joint={j} disabled={disabled} run={run} />
      <button type="button" className={buttonClass} disabled={disabled || !prompt}
        onClick={() => prompt?.({ kind: "attachment", component_a: j.component_a, component_b: j.component_b, joint_id: j.id })}>Edit attachment with AI</button>
      <button className={buttonClass} disabled={disabled} onClick={() => run(() => [{ op: "remove_joint", joint_id: j.id }])}>Detach to stored placement</button>
      <p>Detaching can move this instance and its attached descendants. Review the preview before Apply.</p>
    </details>)}
    <details><summary>Add rigid attachment</summary><JointForm assembly={assembly} disabled={disabled} run={run} prompt={prompt} /></details>
  </details>;
}
