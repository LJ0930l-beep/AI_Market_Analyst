import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

const configDirectory = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(configDirectory, "..");
const runDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "ai-market-analyst-p4-"));
const databasePath = path.join(runDirectory, "phase4-e2e.sqlite3");
const browserChannel = process.env.P4_E2E_BROWSER_CHANNEL ?? "chrome";
const pythonCommand = process.env.PYTHON ?? (process.platform === "win32" ? "python" : "python3");
const quote = (value: string) => `"${value.replaceAll('"', '\\"')}"`;
const harness = quote(path.join(repoRoot, "scripts", "phase4_e2e_harness.py"));
const preview = quote(path.join(configDirectory, "e2e", "preview-server.mjs"));

process.env.P4_E2E_RUN_DIR = runDirectory;

export default defineConfig({
  testDir: "./e2e",
  testMatch: process.env.P4_E2E_CAPTURE === "1" ? "**/capture-v11.spec.ts" : "**/phase4.spec.ts",
  globalTeardown: "./e2e/global-teardown.ts",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]],
  use: {
    baseURL: "http://127.0.0.1:4173",
    ...(browserChannel === "chromium" ? {} : { channel: browserChannel === "edge" ? "msedge" : "chrome" }),
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    ...devices["Desktop Chrome"],
  },
  webServer: [
    {
      command: `${pythonCommand} ${harness} --db ${quote(databasePath)} --host 127.0.0.1 --port 8000`,
      cwd: repoRoot,
      url: "http://127.0.0.1:8000/health",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `node ${preview} --dist ${quote(path.join(configDirectory, "dist"))} --api http://127.0.0.1:8000 --host 127.0.0.1 --port 4173`,
      cwd: repoRoot,
      url: "http://127.0.0.1:4173/",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
