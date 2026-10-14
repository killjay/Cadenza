import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import EvaluationViewer from "./components/EvaluationViewer";
import { viewerLocation } from "./lib/evaluation-view";
import { useStore } from "./lib/store";
import "./index.css";

// Dev-only handle on the store, so the viewport can be poked from the console
// (and from headless-browser checks) without wiring a test harness into the UI.
//   __cadenza.getState().selection
if (import.meta.env.DEV) {
  (window as unknown as { __cadenza: typeof useStore }).__cadenza = useStore;
}

const location = viewerLocation(new URLSearchParams(window.location.search));

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {location.kind === "editor" ? <App /> : location.kind === "viewer" ? <EvaluationViewer runs={location.runs} />
      : <main className="evaluation-view"><h1>Invalid live-test URL</h1><p role="alert">{location.message}</p><a href="/">Open interactive editor</a></main>}
  </StrictMode>,
);
