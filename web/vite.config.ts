import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],

  resolve: {
    alias: {
      // CONTRACTS.md §2.4: the binary frame codec is a SHARED, tested codec and
      // must not be hand-rolled per client. Point at the architect's TypeScript
      // mirror directly rather than keeping a second copy in sync by hand.
      "@cadenza/shared": fileURLToPath(new URL("../shared/ts", import.meta.url)),
    },
  },

  // occt-wasm ships a 22 MB .wasm next to its JS. Pre-bundling rewrites the
  // module's own URL resolution and the auto-locate breaks, so exclude it and
  // hand the worker an explicit `?url` import instead (see occt.worker.ts).
  optimizeDeps: {
    exclude: ["occt-wasm"],
  },

  build: {
    // WASM SIMD / tail calls / exceptions — occt-wasm requires a modern target.
    target: "esnext",
  },

  worker: {
    format: "es",
  },

  server: {
    proxy: {
      // Dev A's FastAPI app. `ws: true` so /ws upgrades are proxied too.
      "/ws": { target: "ws://localhost:8000", ws: true },
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
