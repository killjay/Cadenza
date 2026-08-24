import { useEffect } from "react";
import { TitleBar } from "./components/TitleBar";
import { Viewport } from "./components/Viewport";

import { SidePane } from "./components/SidePane";
import { StatusBar } from "./components/StatusBar";
import { useStore } from "./lib/store";

export default function App() {
  const bootKernel = useStore((s) => s.bootKernel);
  const connect = useStore((s) => s.connect);

  useEffect(() => {
    // Boot the kernel immediately — 4.5 MB brotli has to be on the wire before
    // the user's first click, or the first pick stalls.
    void bootKernel();

    // The viewport starts EMPTY on purpose. It used to preload a fixture plate,
    // which was indistinguishable from a part the user had just built — so a
    // sketch that silently failed to build still looked like it had worked.
    connect();
  }, [bootKernel, connect]);

  return (
    <div className="grid h-full grid-rows-[32px_minmax(0,1fr)_20px] bg-canvas text-ink">
      <TitleBar />
      <main className="grid min-h-0 grid-cols-[minmax(0,1fr)_290px]">
        <div className="relative min-h-0">
          <Viewport />

        </div>
        <SidePane />
      </main>
      <StatusBar />
    </div>
  );
}
