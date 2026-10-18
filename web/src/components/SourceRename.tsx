import { useState } from "react";
import { useStore } from "../lib/store";
import type { Ledger } from "../lib/types";

export function SourceRename({ source, disabled }: { source: Ledger; disabled: boolean }) {
  const [name, setName] = useState(source.metadata.name);
  const state = useStore();
  const duplicate = Object.values(state.ledgers).some(other => other.project_id !== source.project_id && !other.assembly
    && (other.features.length || other.project_id !== state.primaryLedgerId)
    && other.metadata.name.trim().toLocaleLowerCase() === name.trim().toLocaleLowerCase());
  return <form className="my-2 space-y-1" aria-label={`Rename ${source.metadata.name}`} onSubmit={event => {
    event.preventDefault();
    if (!duplicate) state.renamePart(source.project_id, name);
  }}>
    <label className="block">Part name<input aria-label="Part name" required maxLength={100} value={name}
      disabled={disabled} onChange={event => setName(event.target.value)}
      className="w-full rounded border border-hairline bg-chrome p-1" /></label>
    {duplicate && <p role="alert">Another part has this name. Choose a different name.</p>}
    <button className="text-accent disabled:opacity-40" disabled={disabled || duplicate || !name.trim() || name.trim() === source.metadata.name}>
      Preview name change</button>
    <p className="text-ink-4">Updates the part name. Instance names and geometry stay unchanged.</p>
  </form>;
}
