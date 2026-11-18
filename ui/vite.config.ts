import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The browser only ever talks to /api on the BFF (server/bff.mjs), which holds
// the Cognito secret. Vite proxies /api to it during development.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": `http://127.0.0.1:${process.env.BFF_PORT ?? 8787}` },
  },
  test: { environment: "node" },
});
