import { useLayoutEffect, useRef, useState } from "react";
import { useStore } from "../lib/store";
import { Composer } from "./Composer";
import { errorExplanation, fieldLabel, assumptionValue } from "../lib/presentation";

export function AssistantPanel({ active }: { active: boolean }) {
  const conversation = useStore(s => s.conversation);
  const sessionId = useStore(s => s.sessionId);
  const busy = useStore(s => s.busy), phase = useStore(s => s.phase);
  const scroll = useRef<HTMLDivElement>(null), content = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const anchor = useRef<{ id: string; offset: number } | null>(null);
  const previous = useRef({ sessionId, lastId: conversation.at(-1)?.id });
  const [unread, setUnread] = useState(false);
  const [query, setQuery] = useState("");
  const [match, setMatch] = useState<string | null>(null);
  const matches = query.trim() ? conversation.filter(item => item.text.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())).map(item => item.id) : [];
  const remember = () => {
    const panel = scroll.current;
    if (!panel?.clientHeight) return;
    following.current = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 32;
    const top = panel.getBoundingClientRect().top;
    const first = Array.from(content.current?.children ?? []).find(child => child.getBoundingClientRect().bottom > top) as HTMLElement | undefined;
    if (first?.dataset.messageId) anchor.current = { id: first.dataset.messageId, offset: first.getBoundingClientRect().top - top };
    if (following.current) setUnread(false);
  };
  const restore = () => {
    const panel = scroll.current;
    if (!panel?.clientHeight) return;
    if (following.current) panel.scrollTop = panel.scrollHeight;
    else if (anchor.current) {
      const row = Array.from(content.current?.children ?? []).find(child => (child as HTMLElement).dataset.messageId === anchor.current?.id);
      if (row) panel.scrollTop += row.getBoundingClientRect().top - panel.getBoundingClientRect().top - anchor.current.offset;
    }
  };
  useLayoutEffect(() => {
    const last = conversation.at(-1);
    const reset = previous.current.sessionId !== sessionId || !conversation.length;
    if (reset) { following.current = true; anchor.current = null; setQuery(""); setMatch(null); setUnread(false); }
    else if (last?.id !== previous.current.lastId) {
      const start = conversation.findIndex(item => item.id === previous.current.lastId) + 1;
      if (conversation.slice(start).some(item => item.kind === "user")) { following.current = true; setQuery(""); setMatch(null); }
      else if (!following.current) setUnread(true);
    }
    previous.current = { sessionId, lastId: last?.id };
    restore();
  }, [conversation, active, sessionId]);
  useLayoutEffect(() => {
    let frame = 0;
    const observer = new ResizeObserver(() => { cancelAnimationFrame(frame); frame = requestAnimationFrame(restore); });
    if (content.current) observer.observe(content.current);
    if (scroll.current) observer.observe(scroll.current);
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, []);
  const latest = () => { following.current = true; restore(); setUnread(false); remember(); };
  const find = (direction: number) => {
    if (!matches.length || !scroll.current) return;
    const index = matches.indexOf(match ?? "");
    const id = matches[index < 0 ? direction > 0 ? 0 : matches.length - 1 : (index + direction + matches.length) % matches.length];
    const row = Array.from(content.current?.children ?? []).find(child => (child as HTMLElement).dataset.messageId === id);
    if (row) { following.current = false; scroll.current.scrollTop += row.getBoundingClientRect().top - scroll.current.getBoundingClientRect().top; remember(); setMatch(id); }
  };
  return <div className="flex min-h-0 flex-1 flex-col" aria-label="Assistant conversation">
    <div className="flex flex-wrap items-center gap-2 border-b border-hairline p-2 text-11">
      <input aria-label="Search conversation" placeholder="Search this conversation" value={query}
        onChange={event => { setQuery(event.target.value); setMatch(null); }} onKeyDown={event => {
          if (event.key === "Enter") { event.preventDefault(); find(event.shiftKey ? -1 : 1); }
        }} className="min-w-0 flex-1 rounded border border-hairline bg-canvas p-2" />
      {query && <><span role="status">{matches.length} matches</span><button aria-label="Previous match" disabled={!matches.length} onClick={() => find(-1)}>Prev</button>
        <button aria-label="Next match" disabled={!matches.length} onClick={() => find(1)}>Next</button><button onClick={() => { setQuery(""); setMatch(null); }}>Clear search</button></>}
    </div>
    <div ref={scroll} onScroll={remember} tabIndex={0} aria-label="Conversation history" className="chat-history min-h-0 flex-1 overflow-y-auto p-3">
      {!conversation.length && <div className="my-4 space-y-2 text-12 text-ink-3"><p>Describe a part, or attach a dimensioned drawing.</p><p>Choose a part or feature to edit with AI. Review every proposal before applying it.</p></div>}
      <div ref={content}>
        {conversation.map((item) => (
          <div key={item.id} data-message-id={item.id} className="mb-3 select-text break-words" data-search-match={matches.includes(item.id) || undefined}>
            {item.kind === "user" && (
              <div className="rounded-base border border-hairline bg-elevated px-2 py-1.5">
                {item.images && item.images.length > 0 && (
                  <div className="mb-1.5 flex flex-wrap gap-1">
                    {item.images.map((src) => (
                      <img
                        key={src}
                        src={src}
                        alt="Prompt attachment"
                        className="h-12 w-12 rounded-sm border border-hairline object-cover"
                      />
                    ))}
                  </div>
                )}
                <div className="text-12 text-ink">{item.text}</div>
                {item.point && (
                  <div className="mt-1 font-mono text-9 text-ink-4">
                    at {item.point.map((n) => n.toFixed(1)).join(", ")}
                  </div>
                )}
              </div>
            )}
            {item.kind === "assistant" && (
              <div className="px-0.5 text-12 leading-relaxed text-ink-2">
                {item.text}
                {item.assumptions && item.assumptions.length > 0 && (
                  // Numbers the agent inferred rather than read. Surfaced because a
                  // guessed dimension and a measured one must never look alike.
                  <ul className="mt-1 space-y-0.5 border-l border-yellow/50 pl-1.5">
                    {item.assumptions.map((a, i) => (
                      <li key={i} className="text-10 text-ink-4">
                        <span className="text-ink-3">{fieldLabel(a.field)}</span> = {assumptionValue(a.field, a.value)} —{" "}
                        {a.basis}
                        {typeof a.confidence === "number" && (
                          <span className="text-ink-4"> ({Math.round(a.confidence * 100)}%)</span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                {!!item.assumptions?.length && <details className="mt-2 text-10"><summary>Technical details</summary>
                  <pre className="whitespace-pre-wrap break-words">{JSON.stringify(item.assumptions, null, 2)}</pre>
                </details>}
              </div>
            )}
            {item.kind === "status" && (
              <div className="px-0.5 text-11 text-ink-4">{item.text}</div>
            )}
            {item.kind === "error" && (
              <div className="rounded-base border border-red/40 bg-red/10 px-2 py-1.5 text-11 text-red">
                {item.error ? <>
                  <p>{errorExplanation(item.error.code)}</p>
                  <details className="mt-2"><summary>Technical details</summary>
                    <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-10">{item.text}{"\n"}{JSON.stringify(item.error, null, 2)}</pre>
                  </details>
                </> : item.text}
              </div>
            )}
          </div>
        ))}

      </div>
    </div>
    <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 px-3 py-1 text-11">
      <span role="status" aria-live="polite">{busy ? phase || "Working" : unread ? "New messages below" : ""}</span>
      <button className="text-accent" onClick={latest}>{unread ? "New messages - Jump to latest" : "Jump to latest"}</button>
    </div>
    <div className="max-h-[45%] shrink-0 overflow-auto"><Composer active={active} /></div>
  </div>;
}
