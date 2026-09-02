import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

const currentDirectory = path.dirname(fileURLToPath(import.meta.url));
const outputDirectory = path.resolve(currentDirectory, "..", "..", "docs", "screenshots", "v1.1");
const v12OutputDirectory = path.resolve(currentDirectory, "..", "..", "docs", "screenshots", "v1.2.1");

test("capture deterministic V1.1 desktop surfaces", async ({ page }) => {
  test.setTimeout(60_000);
  fs.mkdirSync(outputDirectory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1000 });

  const capture = async (route: string, language: "en" | "zh-CN", filename: string, heading: string) => {
    await page.goto(route);
    const selector = page.locator("#language-select");
    await selector.selectOption(language);
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
    await page.screenshot({ path: path.join(outputDirectory, filename), fullPage: true });
  };

  await capture("/", "zh-CN", "dashboard-zh-CN.png", "看清市场，不被噪音淹没");
  await capture("/", "en", "dashboard-en.png", "Market intelligence, without the noise");
  await capture("/calendar", "zh-CN", "calendar-zh-CN.png", "财经日历");
  await capture("/watchlist", "zh-CN", "watchlist-zh-CN.png", "关注列表");
  await capture("/predictions", "zh-CN", "signals-zh-CN.png", "预测");
  await capture("/heatmap", "zh-CN", "heatmap-zh-CN.png", "市场热力图");
  await capture("/consult", "zh-CN", "ai-assistant-zh-CN.png", "Qwen 咨询");
  await capture("/assets/NVDA", "zh-CN", "asset-detail-zh-CN.png", "资产详情 / NVDA");
  await capture("/settings", "zh-CN", "settings-zh-CN.png", "设置 / 健康");
});

test("capture deterministic V1.2 monitoring surfaces", async ({ page }) => {
  test.setTimeout(60_000);
  fs.mkdirSync(v12OutputDirectory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1000 });

  await page.goto("/monitoring");
  await page.locator("#language-select").selectOption("zh-CN");
  await expect(page.getByRole("heading", { name: "智能盯盘", exact: true })).toBeVisible();
  await page.screenshot({ path: path.join(v12OutputDirectory, "monitoring-zh-CN.png"), fullPage: true });

  await page.goto("/monitoring");
  await page.locator("#language-select").selectOption("en");
  await expect(page.getByRole("heading", { name: "Smart monitoring", exact: true })).toBeVisible();
  await page.screenshot({ path: path.join(v12OutputDirectory, "monitoring-en.png"), fullPage: true });
});
