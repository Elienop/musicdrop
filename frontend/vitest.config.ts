import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Separate from vite.config.ts so the dev proxy (which targets a real backend)
// never leaks into the hermetic, msw-mocked test environment.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    // Give jsdom a concrete origin so the client's same-origin relative URLs
    // (e.g. "/api/health") resolve to absolute URLs under Node's fetch/undici,
    // which — unlike the browser — refuses to parse relative URLs.
    environmentOptions: {
      jsdom: { url: "http://localhost" },
    },
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
