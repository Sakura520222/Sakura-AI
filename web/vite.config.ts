import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev proxy target follows the backend port. Override with APP_PORT when the
// backend listens on a non-default port (kept in sync with the database or env).
const backendOrigin = `http://127.0.0.1:${process.env.APP_PORT || "8000"}`;

export default defineConfig({
  base: "/app/",
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": backendOrigin,
      "/auth": backendOrigin,
      "/star-aid/auth": backendOrigin,
      "/sse": backendOrigin,
    },
  },
  build: { manifest: true, sourcemap: false, chunkSizeWarningLimit: 450 },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    exclude: ["e2e/**", "node_modules/**"],
  },
});
