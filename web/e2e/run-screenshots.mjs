/* global process */

import { spawnSync } from "node:child_process";

const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";
const build = spawnSync(npmCommand, ["run", "build"], {
  env: { ...process.env, VITE_API_BASE_URL: "/api" },
  stdio: "inherit",
  shell: process.platform === "win32",
});
if (build.status !== 0) process.exit(build.status ?? 1);

const playwrightCommand = process.platform === "win32" ? "npx.cmd" : "npx";
const capture = spawnSync(playwrightCommand, ["--no-install", "playwright", "test"], {
  env: { ...process.env, P4_E2E_CAPTURE: "1" },
  stdio: "inherit",
  shell: process.platform === "win32",
});
process.exit(capture.status ?? 1);
