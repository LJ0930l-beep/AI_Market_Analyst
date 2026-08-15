/// <reference types="vitest/config" />

import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

const apiPaths = [
  "/health",
  "/instruments",
  "/analysis",
  "/analyze",
  "/predictions",
  "/paper-trades",
  "/outcomes",
  "/stats",
  "/performance",
  "/calibration",
  "/replay",
];

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: Object.fromEntries(
        apiPaths.map((path) => [path, { target: apiTarget, changeOrigin: false }]),
      ),
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
      css: true,
    },
  };
});
