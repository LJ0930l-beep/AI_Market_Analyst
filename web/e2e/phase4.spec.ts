import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import os from "node:os";
import path from "node:path";

import { validateRunDirectory } from "./global-teardown";
import { resolveStaticCandidate } from "./preview-server.mjs";

const productRoutes = [
  "/",
  "/watchlist",
  "/monitoring",
  "/assets/NVDA",
  "/predictions",
  "/markets",
  "/calendar",
  "/news",
  "/heatmap",
  "/paper-trades",
  "/performance",
  "/replay",
  "/alerts",
  "/consult",
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

test("health, durable Watchlist, and read-only Asset Detail analysis", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Market intelligence, without the noise" })).toBeVisible();
  await expect(page.getByText("Backend connected")).toBeVisible();
  const before = await (await page.request.get("/api/stats")).json();
  const contextHealth = await page.request.get("/api/health/context");
  expect(contextHealth.status()).toBe(200);
  expect(await contextHealth.json()).toMatchObject({ phase: 7, api_version: "1.2.0", read_only_get: true, cloud_required: false });
  const releaseHealth = await page.request.get("/api/health/release");
  expect(releaseHealth.status()).toBe(200);
  expect(await releaseHealth.json()).toMatchObject({ phase: 7, api_version: "1.2.0", backup: { format_version: "phase7_backup_v1" }, capabilities: { local_only: true, scheduler_default_enabled: false } });

  await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", { name: "Watchlist" }).press("Enter");
  await expect(page.getByRole("heading", { name: "Watchlist", exact: true })).toBeVisible();
  const savedWatchlist = page.getByRole("region", { name: "Saved watchlist" });
  const availableInstruments = page.getByRole("region", { name: "Available instruments" });
  await expect(savedWatchlist.getByText("No instruments are saved yet. Add one from the available canonical universe.")).toBeVisible();
  await availableInstruments.getByRole("button", { name: "Add AAPL to saved watchlist" }).press("Enter");
  await expect(savedWatchlist.getByRole("link", { name: "Open asset detail" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("region", { name: "Saved watchlist" }).getByText("AAPL")).toBeVisible();
  await page.getByRole("region", { name: "Saved watchlist" }).getByRole("button", { name: "Remove AAPL from saved watchlist" }).press("Enter");
  await expect(page.getByRole("region", { name: "Saved watchlist" }).getByText("No instruments are saved yet. Add one from the available canonical universe.")).toBeVisible();

  const defaultSettings = await page.request.get("/api/settings");
  expect(defaultSettings.status()).toBe(200);
  expect((await defaultSettings.json()).find((item: { key: string }) => item.key === "scheduler.enabled").value).toBe(false);
  const changedSetting = await page.request.put("/api/settings/scheduler.interval_seconds", { data: { value: 600 } });
  expect(changedSetting.status()).toBe(200);
  expect((await changedSetting.json()).value).toBe(600);
  const resetSetting = await page.request.delete("/api/settings/scheduler.interval_seconds");
  expect(resetSetting.status()).toBe(200);
  expect((await resetSetting.json()).setting.value).toBe(900);

  await page.getByRole("link", { name: "Open asset detail" }).first().press("Enter");
  await expect(page.getByRole("heading", { name: "Asset detail / AAPL" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run analysis" })).toBeVisible();
  await expect(page.getByText("OHLCV evidence")).toBeVisible();
  const phase6Context = page.getByRole("region", { name: "Benchmark, events and Market Memory" });
  await expect(phase6Context).toBeVisible();
  await expect(phase6Context.getByText("benchmark_mapping_v1", { exact: true })).toBeVisible();
  await expect(phase6Context.getByText("event_schema_v1", { exact: true })).toBeVisible();
  await expect(phase6Context.getByText("market_memory_v1", { exact: true })).toBeVisible();

  const loaded = await (await page.request.get("/api/stats")).json();
  expect(loaded.predictions).toBe(before.predictions);
  expect(loaded.paper_trades).toBe(before.paper_trades);
  expect(loaded.phase6_events).toBe(before.phase6_events);
  expect(loaded.market_memory_features).toBe(before.market_memory_features);

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
  expect(after.phase6_events).toBeGreaterThan(loaded.phase6_events);
  expect(after.market_memory_features).toBe(loaded.market_memory_features);

  await page.goto("/watchlist");
  await expect(page.getByRole("heading", { name: "Watchlist", exact: true })).toBeVisible();
  const registration = page.getByRole("region", { name: "Register an instrument" });
  await registration.getByLabel("Instrument symbol").fill("MSFT");
  await registration.getByLabel("Instrument asset type").selectOption("equity");
  const registrationResponse = page.waitForResponse((response) => response.url().endsWith("/api/instruments/register") && response.request().method() === "POST");
  await registration.getByRole("button", { name: "Validate and add" }).click();
  const registered = await (await registrationResponse).json();
  expect(registered.instrument.symbol).toBe("MSFT");
  expect(registered.validation.mode).toBe("injected_test");
  await expect(registration.getByRole("status")).toContainText("MSFT passed provider validation and was saved");
  await expect(page.getByRole("region", { name: "Available instruments" }).getByText("MSFT")).toBeVisible();
  await expect(page.getByRole("region", { name: "Saved watchlist" }).getByText("MSFT")).toBeVisible();

  await page.locator('a[href="/assets/MSFT"]').first().click();
  await expect(page.getByRole("heading", { name: "Asset detail / MSFT" })).toBeVisible();
  await expect(page.getByText("OHLCV evidence")).toBeVisible();
  await page.getByRole("button", { name: "Run analysis" }).click();
  await expect(page.getByText("WAIT · saved coverage result")).toBeVisible();

  const radarWatchlist = await page.request.post("/api/watchlist", { data: { symbol: "TSLA" } });
  expect(radarWatchlist.status()).toBe(200);
});

test("language selector switches to Chinese and persists across route navigation and refresh", async ({ page }) => {
  await page.goto("/");
  const language = page.getByLabel("Language");
  await language.selectOption("zh-CN");
  await expect(page.getByRole("heading", { name: "看清市场，不被噪音淹没", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "关注列表", exact: true }).first()).toBeVisible();

  const chineseRouteTitles: Record<string, string> = {
    "/": "看清市场，不被噪音淹没",
    "/watchlist": "关注列表",
    "/monitoring": "智能盯盘",
    "/assets/NVDA": "资产详情 / NVDA",
    "/predictions": "预测",
    "/markets": "市场",
    "/calendar": "财经日历",
    "/news": "新闻情报",
    "/heatmap": "市场热力图",
    "/paper-trades": "纸面交易",
    "/performance": "表现",
    "/replay": "回放实验室",
    "/alerts": "提醒中心",
    "/consult": "Qwen 咨询",
    "/settings": "设置 / 健康",
  };
  const prohibitedChineseFixedCopy = [
    "Status",
    "API Version",
    "Phase",
    "Real Orders",
    "Private Keys",
    "Provider routing reports available.",
    "MARKET ROUTES",
    "NEWS ROUTE",
    "Mode",
    "Configured",
    "Probe",
    "Lifecycle",
    "Interval / session",
    "Concurrency",
    "Last Run",
    "Next Run",
    "Resource",
    "Backoff",
    "Cache",
    "Settlement",
    "Performance Refresh",
    "Not supplied",
    "Not parseable",
    "OHLCV evidence",
    "Benchmark Context",
    "Market Memory",
  ];
  for (const [route, title] of Object.entries(chineseRouteTitles)) {
    await page.goto(route);
    await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();
    for (const phrase of prohibitedChineseFixedCopy) {
      await expect(page.getByText(phrase, { exact: true }), `${route} leaked fixed English: ${phrase}`).toHaveCount(0);
    }
  }

  await page.getByRole("link", { name: "表现", exact: true }).first().click();
  await expect(page.getByRole("heading", { name: "表现", exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("语言")).toHaveValue("zh-CN");
  await expect(page.getByRole("heading", { name: "表现", exact: true })).toBeVisible();

  await page.getByLabel("语言").selectOption("en");
  await expect(page.getByRole("heading", { name: "Performance", exact: true })).toBeVisible();
  const englishRouteTitles: Record<string, string> = {
    "/": "Market intelligence, without the noise",
    "/watchlist": "Watchlist",
    "/monitoring": "Smart monitoring",
    "/assets/NVDA": "Asset detail / NVDA",
    "/predictions": "Predictions",
    "/markets": "Markets",
    "/calendar": "Economic Calendar",
    "/news": "News intelligence",
    "/heatmap": "Market Heatmap",
    "/paper-trades": "Paper trades",
    "/performance": "Performance",
    "/replay": "Replay lab",
    "/alerts": "Alert Center",
    "/consult": "Qwen Consult",
    "/settings": "Settings / health",
  };
  for (const [route, title] of Object.entries(englishRouteTitles)) {
    await page.goto(route);
    await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();
  }
});

test("Dashboard Market Intelligence reads saved evidence without provider, analysis, or trade side effects", async ({ page }) => {
  const before = await (await page.request.get("/api/stats")).json();
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Market Pulse" })).toBeVisible();
  await expect(page.getByText("NASDAQ").locator("..")).toContainText(/unavailable/i);
  await expect(page.getByRole("heading", { name: "Today's Events" })).toBeVisible();
  const intelligence = await page.request.get("/api/market-intelligence");
  expect(intelligence.status()).toBe(200);
  expect(await intelligence.json()).toMatchObject({ contract_version: "market_intelligence_v1", read_only: true, provider_calls: false, domain_writes: false });

  const after = await (await page.request.get("/api/stats")).json();
  expect(after.predictions).toBe(before.predictions);
  expect(after.paper_trades).toBe(before.paper_trades);
});

test("V1.1 market surfaces, durable preferences, and explicit Smart Daily Brief remain bounded", async ({ page }) => {
  const before = await (await page.request.get("/api/stats")).json();
  for (const [route, heading] of [["/markets", "Markets"], ["/calendar", "Economic Calendar"], ["/news", "News intelligence"], ["/heatmap", "Market Heatmap"]] as const) {
    await page.goto(route);
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
    await expect(page.getByText(/GET read-only/)).toBeVisible();
  }
  await page.goto("/settings");
  await page.getByLabel("Model preference").selectOption("smart");
  await page.getByLabel("AI response language").selectOption("zh-CN");
  await page.getByRole("button", { name: "Save preferences" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Preferences saved" })).toBeVisible();
  const settings = await (await page.request.get("/api/settings")).json();
  expect(settings.find((item: { key: string }) => item.key === "ai.model_preference").value).toBe("smart");

  await page.goto("/");
  const response = page.waitForResponse((item) => item.url().endsWith("/api/daily-brief/generate") && item.request().method() === "POST");
  await page.getByRole("button", { name: "Generate brief" }).click();
  const brief = await (await response).json();
  expect(brief.brief.model_tier).toBe("smart");
  expect(brief.brief.model_id).toBe("qwen-e2e-fixture");
  await expect(page.getByText("确定性 Qwen 测试回答。", { exact: true })).toBeVisible();
  const after = await (await page.request.get("/api/stats")).json();
  expect(after.daily_briefs).toBe(before.daily_briefs + 1);
  for (const key of ["predictions", "outcomes", "paper_trades", "calibration_results"]) expect(after[key]).toBe(before[key]);
});

test("Qwen Consult streams local fixture output, restores the browser session, and remains domain read-only", async ({ page }) => {
  const before = await (await page.request.get("/api/stats")).json();
  await page.goto("/assets/NVDA");
  await page.getByRole("link", { name: "Consult Qwen about this instrument" }).click();
  await expect(page).toHaveURL(/\/consult\?symbol=NVDA$/);
  await expect(page.getByRole("heading", { name: "Qwen Consult", exact: true })).toBeVisible();
  await expect(page.getByText("Qwen is available", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Optional instrument context")).toHaveValue("NVDA");

  const composer = page.getByLabel("Message Qwen");
  await composer.fill("Summarize only the saved NVDA evidence.");
  await composer.press("Shift+Enter");
  await expect(composer).toHaveValue(/\n$/);
  await composer.press("Enter");
  await expect(page.getByText("确定性 Qwen 测试回答。", { exact: true })).toBeVisible();
  await expect(page.getByText("Response complete", { exact: true })).toBeVisible();
  const context = page.getByRole("region", { name: "Read-only context evidence" });
  await expect(context.getByText("NVDA", { exact: true })).toBeVisible();
  await expect(context.getByText(/durable_latest_live_prediction/)).toBeVisible();

  await page.reload();
  await expect(page.getByText("确定性 Qwen 测试回答。", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Optional instrument context")).toHaveValue("NVDA");
  await page.getByRole("button", { name: "Clear conversation" }).click();
  await page.getByRole("button", { name: "Clear now" }).click();
  await expect(page.getByText("No messages yet. Ask a research question to begin.", { exact: true })).toBeVisible();

  const after = await (await page.request.get("/api/stats")).json();
  for (const key of ["predictions", "outcomes", "paper_trades", "calibration_results", "alerts", "market_memory_features"]) {
    expect(after[key], `${key} must remain unchanged by consultation`).toBe(before[key]);
  }
});

test("explicit local scheduler scans Watchlist once, updates Radar, and stops cleanly", async ({ page }) => {
  const before = await (await page.request.get("/api/stats")).json();
  const beforePerformance = await (await page.request.get("/api/performance/summary?source_type=live")).json();
  const beforeRadar = await (await page.request.get("/api/radar")).json();
  const beforeTsla = beforeRadar.entries.find((entry: { symbol: string }) => entry.symbol === "TSLA");
  const initial = await page.request.get("/api/scheduler/status");
  expect(initial.status()).toBe(200);
  expect(await initial.json()).toMatchObject({ enabled: false, running: false, thread_alive: false, effective_concurrency: 1 });

  const enabled = await page.request.put("/api/settings/scheduler.enabled", { data: { value: true } });
  expect(enabled.status()).toBe(200);
  const ready = await page.request.get("/api/scheduler/status");
  expect(await ready.json()).toMatchObject({ enabled: true, running: false, session_policy: "market_hours" });

  const run = await page.request.post("/api/scheduler/run-once");
  expect(run.status()).toBe(200);
  const runPayload = await run.json();
  expect(runPayload.run.status).toBe("COMPLETED");
  expect(runPayload.run.counts.planned).toBeGreaterThanOrEqual(1);
  expect(runPayload.run.counts.wait).toBeGreaterThanOrEqual(1);
  expect(runPayload.run.counts.settlement_planned).toBeGreaterThanOrEqual(1);
  expect(runPayload.run.counts.settlement_settled).toBeGreaterThanOrEqual(1);
  expect(runPayload.run.counts.settlement_wait).toBeGreaterThanOrEqual(1);
  expect(runPayload.items.some((item: { stage?: string; prediction_id?: string; outcome_status?: string }) => item.stage === "settlement" && item.prediction_id === "p4-fresh-long" && item.outcome_status === "TP1")).toBe(true);
  expect(runPayload.items.some((item: { symbol: string; status: string; prediction_id?: string | null }) => item.symbol === "TSLA" && item.status === "COMPLETED" && typeof item.prediction_id === "string")).toBe(true);

  const radar = await (await page.request.get("/api/radar")).json();
  const tsla = radar.entries.find((entry: { symbol: string }) => entry.symbol === "TSLA");
  expect(tsla.action).toBe("WAIT");
  expect(tsla.category).not.toBe(beforeTsla.category);
  const afterPerformance = await (await page.request.get("/api/performance/summary?source_type=live")).json();
  expect(afterPerformance.metrics.resolved_actionable).toBeGreaterThan(beforePerformance.metrics.resolved_actionable);
  const after = await (await page.request.get("/api/stats")).json();
  expect(after.predictions).toBeGreaterThan(before.predictions);
  expect(after.paper_trades).toBe(before.paper_trades);

  const disabled = await page.request.put("/api/settings/scheduler.enabled", { data: { value: false } });
  expect(disabled.status()).toBe(200);
  const stopped = await page.request.post("/api/scheduler/stop");
  expect(stopped.status()).toBe(200);
  expect(await stopped.json()).toMatchObject({ enabled: false, running: false, thread_alive: false });
  const rejected = await page.request.post("/api/scheduler/run-once");
  expect(rejected.status()).toBe(409);
  expect((await rejected.json()).error.code).toBe("SCHEDULER_DISABLED");

  await page.goto("/settings");
  const scheduler = page.getByRole("region", { name: "Local Watchlist scheduler" });
  await expect(scheduler.getByText("COMPLETED", { exact: true }).last()).toBeVisible();
  await expect(scheduler.getByText(/effective model limit 1/)).toBeVisible();
  await expect(scheduler.getByText(/settlement_v1/)).toBeVisible();
  await expect(scheduler.getByText(/live_performance_v1/)).toBeVisible();
  await expect(page.getByRole("region", { name: "Phase 6 context capabilities" })).toBeVisible();
  await expect(scheduler.getByRole("button", { name: "Enable scheduler" })).toBeVisible();
});

test("Alert Center reads deduped local evidence and acknowledges without financial mutation", async ({ page }) => {
  const before = await (await page.request.get("/api/stats")).json();
  const beforePrediction = await (await page.request.get("/api/predictions/p4-fresh-long")).json();
  const beforeCalibration = await (await page.request.get("/api/calibration/current")).json();
  const readMethods: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET") readMethods.push(request.method());
  });
  await page.goto("/alerts");
  await expect(page.getByRole("heading", { name: "Alert Center", exact: true })).toBeVisible();
  await expect(page.getByText("alert_policy_v1", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/No Telegram, Discord, email, cloud notifier/)).toBeVisible();
  const alertResponse = await page.request.get("/api/alerts?limit=100");
  expect(alertResponse.status()).toBe(200);
  const alertPayload = await alertResponse.json();
  const sourceRows = await Promise.all(["prediction", "outcome", "radar", "operational"].map(async (source) => {
    const response = await page.request.get(`/api/alerts?source=${source}&limit=100`);
    expect(response.status()).toBe(200);
    return [source, (await response.json()).alerts.length] as const;
  }));
  const sourceCounts = Object.fromEntries(sourceRows);
  expect(sourceCounts.prediction).toBeGreaterThan(0);
  expect(sourceCounts.outcome).toBeGreaterThan(0);
  expect(sourceCounts.radar).toBeGreaterThan(0);
  expect(sourceCounts.operational).toBeGreaterThan(0);
  expect(alertPayload.policy.version).toBe("alert_policy_v1");
  expect(alertPayload.capabilities.outbound_notifiers).toBe(false);
  expect(alertPayload.capabilities.broker_or_real_order).toBe(false);
  expect(alertPayload.counts.total).toBeGreaterThan(0);
  expect(readMethods).toEqual([]);

  const firstAck = page.getByRole("button", { name: "Acknowledge alert" }).first();
  await expect(firstAck).toBeVisible();
  const acknowledgeResponse = page.waitForResponse((response) => response.url().includes("/api/alerts/") && response.url().endsWith("/acknowledge") && response.request().method() === "POST");
  await firstAck.click();
  const acknowledged = await (await acknowledgeResponse).json();
  expect(acknowledged.status).toBe("ACKNOWLEDGED");
  const after = await (await page.request.get("/api/stats")).json();
  expect(after.predictions).toBe(before.predictions);
  expect(after.paper_trades).toBe(before.paper_trades);
  expect(after.outcomes).toBe(before.outcomes);
  expect(after.calibration_results).toBe(before.calibration_results);
  const afterPrediction = await (await page.request.get("/api/predictions/p4-fresh-long")).json();
  expect(afterPrediction.raw_confidence).toBe(beforePrediction.raw_confidence);
  expect(afterPrediction.calibrated_confidence).toBe(beforePrediction.calibrated_confidence);
  const afterCalibration = await (await page.request.get("/api/calibration/current")).json();
  expect(afterCalibration).toMatchObject({ status: beforeCalibration.status, scope: beforeCalibration.scope });
  const repeated = await page.request.get("/api/alerts?limit=100");
  expect((await repeated.json()).counts.total).toBe(alertPayload.counts.total);

  await page.goto("/settings");
  await expect(page.getByRole("region", { name: "Alert reconciliation" })).toBeVisible();
  await expect(page.getByText(/Alerts originate in local SQLite/i)).toBeVisible();
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

  await page.goto("/paper-trades");
  await expect(page.getByRole("heading", { name: "Paper trades" })).toBeVisible();
  await page.getByRole("button", { name: "p4-fresh-long" }).click();
  await expect(page.getByText("Linked Prediction", { exact: true })).toBeVisible();
  await expect(page.getByText("Linked Outcome", { exact: true })).toBeVisible();
  await expect(page.getByText("TP1").last()).toBeVisible();
  await page.getByRole("button", { name: "p4-paper-outcome" }).click();
  await expect(page.getByText("Linked Outcome", { exact: true })).toBeVisible();
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
  expect(await health.json()).toMatchObject({ status: "ok", phase: 7, api_version: "1.2.0", real_orders: false });
});

test("all V1.1 product routes pass axe and page-level overflow checks at desktop and 390x844", async ({ page }) => {
  test.setTimeout(120_000);
  for (const route of productRoutes) {
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
