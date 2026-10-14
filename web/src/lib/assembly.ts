import type { AssemblyPromptTarget, Component, Ledger } from "./types";

export interface AssemblyPromptContext { ledgerId: string; ownerPath: string[]; target: AssemblyPromptTarget; }

export function validAssemblyPrompt(ledgers: Record<string, Ledger>, root: string, context: AssemblyPromptContext): boolean {
  if (assemblyOwner(ledgers, root, context.ownerPath) !== context.ledgerId) return false;
  const assembly = ledgers[context.ledgerId]?.assembly, target = context.target;
  if (target.kind === "definition") return true;
  const components = assembly?.components ?? [], joints = assembly?.joints ?? [];
  if (target.kind === "instance") return components.some(c => c.id === target.component_id)
    && (target.detach_joint_ids ?? []).every(id => joints.some(j => j.id === id
      && (j.component_a === target.component_id || j.component_b === target.component_id)));
  return target.component_a !== target.component_b && [target.component_a, target.component_b].every(id => components.some(c => c.id === id))
    && (!target.joint_id || joints.some(j => j.id === target.joint_id && j.component_a === target.component_a && j.component_b === target.component_b));
}

export interface AssemblyRow { path: string; labels: string[]; component: Component; source?: Ledger; ownerId: string; ownerPath: string[]; }

export function assemblyOwner(ledgers: Record<string, Ledger>, root: string, path: string[]): string | null {
  if (path.length > 8) return null;
  let owner = root;
  for (const id of path) {
    const source = ledgers[owner]?.assembly?.components.find(c => c.id === id)?.sub_assembly_id;
    if (!source || !ledgers[source]?.assembly) return null;
    owner = source;
  }
  return ledgers[owner] ? owner : null;
}

/** Occurrence paths retain repeated definitions; malformed/oversized legacy trees are bounded. */
export function assemblyRows(ledgers: Record<string, Ledger>, root: string): AssemblyRow[] {
  const rows: AssemblyRow[] = [];
  function visit(id: string, path: string[], labels: string[], ancestors: string[]) {
    if (ancestors.includes(id) || path.length >= 8 || rows.length >= 1024) return;
    for (const component of (ledgers[id]?.assembly?.components ?? []).slice(0, 32)) {
      if (rows.length >= 1024) return;
      const parts = [...path, component.id], names = [...labels, component.name || component.id];
      const source = component.sub_assembly_id ? ledgers[component.sub_assembly_id] : undefined;
      rows.push({ path: parts.join("/"), labels: names, component, source, ownerId: id, ownerPath: path });
      if (source?.assembly) visit(source.project_id, parts, names, [...ancestors, id]);
    }
  }
  visit(root, [], [], []);
  return rows;
}
