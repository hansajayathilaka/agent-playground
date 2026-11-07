import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev, the UI runs on Vite and forwards API calls to Envoy, so the browser
// stays same-origin exactly like production (Envoy serves both there).
const envoy = process.env.ENVOY_URL ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/platform.v1.": { target: envoy, changeOrigin: true },
    },
  },
  build: { chunkSizeWarningLimit: 5000 },
});
