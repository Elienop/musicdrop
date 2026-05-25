import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    proxy: {
      // Dev-only: forward API calls to the FastAPI backend on :3030 so the
      // frontend (vite on :5173) reaches it without CORS/baseUrl juggling.
      "/api": {
        target: "http://localhost:3030",
        changeOrigin: true,
      },
    },
  },
});
