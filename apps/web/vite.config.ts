import path from "node:path";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/auth": "http://localhost:8000",
      "/runs": "http://localhost:8000",
      "/lanes": "http://localhost:8000",
      "/sessions": "http://localhost:8000",
      "/repos": "http://localhost:8000",
      "/modes": "http://localhost:8000",
      "/hydration": "http://localhost:8000",
      "/approvals": "http://localhost:8000",
      "/team": "http://localhost:8000",
      "/knowledge": "http://localhost:8000",
      "/ideas": "http://localhost:8000",
      "/proposals": "http://localhost:8000",
      "/push": "http://localhost:8000",
      "/me": "http://localhost:8000",
      "/campaigns": "http://localhost:8000",
      "/deliveries": "http://localhost:8000",
      "/stats": "http://localhost:8000",
      "/bench": "http://localhost:8000",
      "/webhooks": "http://localhost:8000",
      "/health": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/__tests__/setup.ts"],
  },
});
