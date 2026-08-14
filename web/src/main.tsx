import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { useStore } from "./lib/store";
import "./index.css";

// Dev-only handle on the store, so the viewport can be poked from the console
// (and from headless-browser checks) without wiring a test harness into the UI.
//   __cadenza.getState().selection
if (import.meta.env.DEV) {
  (window as unknown as { __cadenza: typeof useStore }).__cadenza = useStore;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
