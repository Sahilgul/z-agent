import path from "node:path";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vitest/config";

/** Backend router prefixes — mirrored in infra/vm/nginx.conf. */
const API_PREFIXES = [
  "/auth", "/runs", "/lanes", "/sessions", "/repos", "/modes", "/hydration",
  "/approvals", "/team", "/knowledge", "/ideas", "/proposals", "/push", "/me",
  "/campaigns", "/deliveries", "/stats", "/bench", "/webhooks", "/health",
];

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      ...Object.fromEntries(
        API_PREFIXES.map((prefix) => [
          prefix,
          {
            target: "http://localhost:8000",
            // The SPA and the API share these paths (/repos is both a screen and
            // a router prefix). Only a top-level browser navigation sets
            // Sec-Fetch-Mode: navigate — fetch/XHR never does — so hand those
            // back to the SPA instead of dumping raw JSON in the address bar.
            bypass: (req: { headers: Record<string, string | string[] | undefined> }) =>
              req.headers["sec-fetch-mode"] === "navigate" ? "/index.html" : undefined,
          },
        ]),
      ),
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/__tests__/setup.ts"],
  },
});
