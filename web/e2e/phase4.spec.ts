import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import os from "node:os";
import path from "node:path";

import { validateRunDirectory } from "./global-teardown";
import { resolveStaticCandidate } from "./preview-server.mjs";

const phase4Routes = [
  "/",
  "/watchlist",
  "/assets/NVDA",
  "/predictions",
  "/paper-trades",
  "/performance",
  "/replay",
  "/settings",
];

test.describe.configure({ mode: "serial" });

test("test infrastructure rejects unsafe cleanup and static-root paths", () => {
  const temporaryDirectory = path.resolve(os.tmpdir());
  const safeRunDirectory = path.join(temporaryDirectory, "ai-market-analyst-p4-safety-check");
  expect(validateRunDirectory(safeRunDirectory, temporaryDirectory)).toBe(safeRunDirectory);
  expect(() => validateRunDirectory(path.join(safeRunDirectory, "nested"), temporaryDirectory)).toThrow();
  expect(() => validateRunDirectory(path.join(temporaryDirectory, "unrelated-run"), temporaryDirectory)).toThrow();

  const distDirectory = path.join(temporaryDirectory, "phase4-dist");
  expect(resolveStaticCandidate(distDirectory, "/assets/index.js")).toBe(path.resolve(distDirectory, "assets/index.js"));
  expect(resolveStaticCandidate(distDirectory, "/../phase4-dist-evil/secret.txt")).toBe(path.resolve(distDirectory, "index.html"));
});

test("health, Watchlist navigation, and read-only Asset Detail analysis", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("Backend connected")).toBeVisible();
  const before = await (await page.request.get("/api/stats")).json();

  await page.getByRole("link", { name: "Watchlist" }).press("Enter");
  await expect(page.getByRole("heading", { name: "Watchlist" })).toBeVisible();
  await page.getByRole("link", { name: "Open asset detail" }).first().press("Enter");
  await expect(page.getByRole("heading", { name: "Asset detail / AAPL" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run analysis" })).toBeVisible();
  await expect(page.getByText("OHLCV evidence")).toBeVisible();

  const loaded = await (await page.request.get("/api/stats")).json();
  expect(loaded.predictions).toBe(before.predictions);
  expect(loaded.paper_trades).toBe(before.paper_trades);

  const analysisResponse = page.waitForResponse((response) => response.url().endsWith("/api/analysis/AAPL") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Run analysis" }).press("Enter");
  const analysis = await (await analysisResponse).json();
  expect(analysis.signal.action).toBe("WAIT");
  await expect(page.getByRole("heading", { name: "Signal proposal" })).toBeVisible();
  await expect(page.getByText("WAIT · saved coverage result")).toBeVisible();
  await expect(page.getByText("Entry, stop and targets are not applicable to a WAIT result.")).toBeVisible();
  await expect(page.getByRole("button", { name: /Follow as PaperTrade/i })).toHaveCount(0);

  const after = await (await page.request.get("/api/stats")).json();
  expect(after.predictions).toBe(before.predictions + 1);
  expect(after.paper_trades).toBe(before.paper_trades);
});

test("fresh LONG double-click Follow creates exactly one PaperTrade and linked detail", async ({ page }) => {
  const before = await (await page.request.get("/api/stats")).json();
  let followPosts = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/api/predictions/p4-fresh-long/follow")) followPosts += 1;
  });

  await page.goto("/predictions");
  await page.getByLabel("Symbol").fill("TSLA");
  await page.getByRole("button", { name: "Apply filters" }).press("Enter");
  await expect(page.getByRole("button", { name: "p4-fresh-long" })).toBeVisible();
  await page.getByRole("button", { name: "p4-fresh-long" }).click();
  const follow = page.getByRole("button", { name: "Follow as PaperTrade" });
  await expect(follow).toBeEnabled();
  await follow.dblclick();
  await expect(page.getByText(/Already followed as a local PaperTrade/)).toBeVisible();
  expect(followPosts).toBe(1);

  const trade = await page.request.get("/api/paper-trades/p4-fresh-long");
  expect(trade.status()).toBe(200);
  expect((await trade.json()).prediction_id).toBe("p4-fresh-long");
  const after = await (await page.request.get("/api/stats")).json();
  expect(after.paper_trades).toBe(before.paper_trades + 1);

  await page.getByRole("link", { name: "Paper trades" }).click();
  await expect(page.getByRole("heading", { name: "Paper trades" })).toBeVisible();
  await page.getByRole("button", { name: "p4-fresh-long" }).click();
  await expect(page.getByText("Linked Prediction")).toBeVisible();
  await expect(page.getByText("No linked Outcome record was returned.")).toBeVisible();
  await page.getByRole("button", { name: "p4-paper-outcome" }).click();
  await expect(page.getByText("Linked Outcome")).toBeVisible();
  await expect(page.getByText("TP1").last()).toBeVisible();
  await expect(page.getByText(/No real order, broker action or execution/)).toBeVisible();
});

test("Performance exposes preliminary and resolved evidence plus current calibration scope", async ({ page }) => {
  await page.goto("/performance");
  await expect(page.getByRole("heading", { name: "Performance", exact: true })).toBeVisible();
  await expect(page.getByText("PRELIMINARY", { exact: true })).toBeVisible();
  await expect(page.getByText("Resolved actionable evidence is available for the returned scope.")).toBeVisible();
  await expect(page.getByText("ACTIVE", { exact: true })).toBeVisible();
  await expect(page.getByText("p4-e2e-model", { exact: true })).toBeVisible();

  const summary = await (await page.request.get("/api/performance/summary?source_type=live")).json();
  expect(summary.status).toBe("PRELIMINARY");
  expect(summary.metrics.resolved_actionable).toBeGreaterThanOrEqual(100);
  expect(summary.metrics.actionable_count).toBeGreaterThanOrEqual(summary.metrics.resolved_actionable);
  const calibration = await (await page.request.get("/api/calibration/current")).json();
  expect(calibration.status).toBe("ACTIVE");
  expect(calibration.scope).toMatchObject({ source_type: "live", model_id: "p4-e2e-model" });
});

test("Replay Lab exposes error/status/count/capability evidence and no mutation controls", async ({ page }) => {
  const nonGetMethods: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET") nonGetMethods.push(request.method());
  });
  await page.goto("/replay");
  await expect(page.getByRole("heading", { name: "Replay lab" })).toBeVisible();
  await expect(page.getByRole("button", { name: "p4-e2e-replay-with-errors" })).toBeVisible();
  await page.getByRole("button", { name: "p4-e2e-replay-with-errors" }).click();
  const detail = page.getByRole("region", { name: "Replay run detail" });
  await expect(detail.getByText("COMPLETED_WITH_ERRORS", { exact: true }).last()).toBeVisible();
  await expect(detail.getByText("SAMPLE_ERRORS", { exact: true })).toBeVisible();
  await expect(detail.getByText(/Error: HISTORICAL_NEWS_UNAVAILABLE/)).toBeVisible();
  await expect(detail.getByText(/Historical news: not available/).first()).toBeVisible();
  await expect(detail.getByText(/Technical-only: yes/).first()).toBeVisible();
  await expect(detail.getByText("Errors", { exact: true })).toBeVisible();
  await expect(detail.getByText("1", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/there is no create, start, resume or retry-run action/i)).toBeVisible();
  expect(nonGetMethods).toEqual([]);
});

test("SPA deep links return HTML while /api/health remains JSON", async ({ page }) => {
  const deepLink = await page.request.get("/assets/NVDA");
  expect(deepLink.status()).toBe(200);
  expect(deepLink.headers()["content-type"]).toContain("text/html");
  expect(await deepLink.text()).toContain('<div id="root"></div>');

  const health = await page.request.get("/api/health");
  expect(health.status()).toBe(200);
  expect(health.headers()["content-type"]).toContain("application/json");
  expect(await health.json()).toMatchObject({ status: "ok", phase: 4, real_orders: false });
});

test("all Phase 4 routes pass axe and page-level overflow checks at desktop and 390x844", async ({ page }) => {
  for (const route of phase4Routes) {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(route);
    await expect(page.locator("main h1")).toBeVisible();
    await page.waitForTimeout(300);
    const desktopAxe = await new AxeBuilder({ page }).analyze();
    expect(desktopAxe.violations, `${route} desktop axe violations`).toEqual([]);
    const desktopOverflow = await page.evaluate(() => ({
      body: document.body.scrollWidth,
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }));
    expect(desktopOverflow.body, `${route} desktop body overflow`).toBeLessThanOrEqual(desktopOverflow.viewport);
    expect(desktopOverflow.document, `${route} desktop document overflow`).toBeLessThanOrEqual(desktopOverflow.viewport);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    await expect(page.locator("main h1")).toBeVisible();
    await page.waitForTimeout(300);
    const mobileAxe = await new AxeBuilder({ page }).analyze();
    expect(mobileAxe.violations, `${route} mobile axe violations`).toEqual([]);
    const mobileOverflow = await page.evaluate(() => ({
      body: document.body.scrollWidth,
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }));
    expect(mobileOverflow.body, `${route} mobile body overflow`).toBeLessThanOrEqual(mobileOverflow.viewport);
    expect(mobileOverflow.document, `${route} mobile document overflow`).toBeLessThanOrEqual(mobileOverflow.viewport);
  }
});
