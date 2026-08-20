/* global process */

import { spawnSync } from "node:child_process";

const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";
const buildOnly = process.argv.includes("--build-only");
const testArgs = process.argv.slice(2).filter((value) => value !== "--build-only");
const build = spawnSync(npmCommand, ["run", "build"], {
  env: { ...process.env, VITE_API_BASE_URL: "/api" },
  stdio: "inherit",
  shell: process.platform === "win32",
});
if (build.status !== 0) {
  process.exit(build.status ?? 1);
}
if (buildOnly) {
  process.exit(0);
}
const playwrightCommand = process.platform === "win32" ? "npx.cmd" : "npx";
const test = spawnSync(playwrightCommand, ["--no-install", "playwright", "test", ...testArgs], { stdio: "inherit", shell: process.platform === "win32" });
process.exit(test.status ?? 1);
