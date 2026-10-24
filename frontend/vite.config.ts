import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The production build is served by the FastAPI backend from frontend/dist.
// During development, Vite proxies API and WebSocket traffic to the backend.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5190,
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: false, ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
