import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  I18nProvider,
  LANGUAGE_STORAGE_KEY,
  detectBrowserLanguage,
  resolveInitialLanguage,
  translateKey,
  translateText,
  translationCatalog,
  useI18n,
} from "./i18n";

function LanguageProbe() {
  const { language, setLanguage, t } = useI18n();
  return (
    <div>
      <label htmlFor="test-language">{t("language.label")}</label>
      <select id="test-language" value={language} onChange={(event) => setLanguage(event.target.value === "zh-CN" ? "zh-CN" : "en")}>
        <option value="en">{t("language.english")}</option>
        <option value="zh-CN">{t("language.chinese")}</option>
      </select>
      <h1>{t("nav.dashboard")}</h1>
      <p>{t("dashboard.description")}</p>
    </div>
  );
}

describe("i18n contract", () => {
  beforeEach(() => {
    window.localStorage.clear();
    Object.defineProperty(window.navigator, "language", { configurable: true, value: "en-US" });
    Object.defineProperty(window.navigator, "languages", { configurable: true, value: ["en-US"] });
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it("keeps English and Chinese catalogs key-complete", () => {
    expect(Object.keys(translationCatalog.en).sort()).toEqual(Object.keys(translationCatalog["zh-CN"]).sort());
    expect(translateKey("nav.dashboard", "zh-CN")).toBe("仪表盘");
    expect(translateKey("nav.consult", "zh-CN")).toBe("Qwen 咨询");
    expect(translateKey("consult.stop", "zh-CN")).toBe("停止生成");
    expect(translateText("PRELIMINARY", "zh-CN")).toBe("初步");
    expect(translateText('{"available":true}', "zh-CN")).toBe('{"available":true}');
    expect(translateText("unmapped backend message", "zh-CN")).toBe("unmapped backend message");
  });

  it("defaults a fresh installation to Chinese regardless of browser language", () => {
    expect(resolveInitialLanguage()).toBe("zh-CN");
    expect(translateText("AI_LED", "zh-CN")).toBe("AI 自主决策");
    expect(translateText("🤖 AI Autonomous Trader Console (N10)", "zh-CN")).toBe("🤖 AI 自主交易控制台");
    expect(translateText("Core CPI y/y", "zh-CN")).toBe("核心消费者价格指数 同比");
    expect(translateText("intent_AI_LED_123", "zh-CN")).toBe("intent_AI_LED_123");
  });

  it("chooses Chinese from the browser locale and persists a manual switch", () => {
    Object.defineProperty(window.navigator, "language", { configurable: true, value: "zh-CN" });
    Object.defineProperty(window.navigator, "languages", { configurable: true, value: ["zh-CN", "en-US"] });
    expect(detectBrowserLanguage()).toBe("zh-CN");
    expect(resolveInitialLanguage()).toBe("zh-CN");

    render(<I18nProvider><LanguageProbe /></I18nProvider>);
    expect(screen.getByRole("heading", { name: "仪表盘" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("语言"), { target: { value: "en" } });
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(window.localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe("en");
  });

  it("prefers a stored language over browser detection and falls back to English", () => {
    Object.defineProperty(window.navigator, "language", { configurable: true, value: "zh-CN" });
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    expect(resolveInitialLanguage()).toBe("en");
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "unsupported");
    expect(resolveInitialLanguage()).toBe("zh-CN");
  });
});
