/* global console, process */

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";

const playwrightVersion = execFileSync(process.platform === "win32" ? "npx.cmd" : "npx", ["--no-install", "playwright", "--version"], { encoding: "utf8", shell: process.platform === "win32" }).trim();
const browserCandidates = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
];
const installed = browserCandidates.filter((candidate) => existsSync(candidate));
console.log(`Playwright ${playwrightVersion}`);
if (process.env.P4_E2E_BROWSER_CHANNEL === "chromium") {
  console.log("Pinned Playwright Chromium selected; run npm run e2e:install before npm run e2e.");
  process.exit(0);
}
if (installed.length === 0) {
  console.error("No supported installed Chrome/Edge channel was found. Run npm run e2e:install for pinned Chromium.");
  process.exit(1);
}
console.log(`Browser channel candidates: ${installed.join(", ")}`);
