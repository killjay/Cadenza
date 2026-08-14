import { useStore } from "../lib/store";

const DOT: Record<string, string> = {
  open: "bg-green",
  connecting: "bg-yellow",
  idle: "bg-ink-4",
  closed: "bg-ink-4",
  error: "bg-red",
};

export function StatusBar() {
  const connection = useStore((s) => s.connection);
  const detail = useStore((s) => s.connectionDetail);
  const kernel = useStore((s) => s.kernel);
  const selection = useStore((s) => s.selection);
  const connect = useStore((s) => s.connect);

  return (
    <footer className="flex items-center gap-3 border-t border-hairline bg-chrome px-2.5 text-10 text-ink-4">
      <span className="flex items-center gap-1.5">
        <span className={`h-1.5 w-1.5 rounded-full ${DOT[connection] ?? "bg-ink-4"}`} />
        socket {connection}
        {detail && <span className="text-ink-4/70">({detail})</span>}
      </span>

      {/* The stub server is gone — there is a real backend now, and a fake one
          that answers plausibly is worse than a visible disconnection. */}
      <button
        className="rounded-sm px-1.5 py-px hover:bg-wash hover:text-ink-2"
        onClick={() => connect()}
      >
        reconnect
      </button>

      <span className="h-3 w-px bg-hairline-strong" />

      <span>
        occt-wasm{" "}
        <span
          className={
            kernel.state === "ready"
              ? "text-green"
              : kernel.state === "failed"
                ? "text-red"
                : "text-ink-3"
          }
        >
          {kernel.state}
        </span>
        {kernel.bootMs !== undefined && ` · ${kernel.bootMs} ms`}
      </span>

      <div className="flex-1" />

      {selection && (
        <span className="font-mono">
          {selection.point.map((n) => n.toFixed(2)).join("  ")} mm
        </span>
      )}
      <span>mm · Z-up</span>
    </footer>
  );
}
