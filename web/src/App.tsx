import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";

import { apiClient as defaultApiClient, type ApplicationShellApiClient } from "./api/client";
import type { HealthResponse } from "./api/types";
import { HealthStatus, type BackendHealthState } from "./components/HealthStatus";
import { TimeProvenanceRail } from "./components/TimeProvenanceRail";
import { DashboardPage, emptyDashboardProvenance, type DashboardProvenance } from "./pages/DashboardPage";
import { I18nProvider, useI18n, type TranslationKey } from "./i18n";

const AssetDetailPage = lazy(() => import("./pages/AssetDetailPage").then((m) => ({ default: m.AssetDetailPage })));
const AlertsPage = lazy(() => import("./pages/AlertsPage").then((m) => ({ default: m.AlertsPage })));
const PaperTradesPage = lazy(() => import("./pages/PaperTradesPage").then((m) => ({ default: m.PaperTradesPage })));
const PerformancePage = lazy(() => import("./pages/PerformancePage").then((m) => ({ default: m.PerformancePage })));
const PredictionsPage = lazy(() => import("./pages/PredictionsPage").then((m) => ({ default: m.PredictionsPage })));
const QwenConsultPage = lazy(() => import("./pages/QwenConsultPage").then((m) => ({ default: m.QwenConsultPage })));
const ReplayLabPage = lazy(() => import("./pages/ReplayLabPage").then((m) => ({ default: m.ReplayLabPage })));
const SettingsHealthPage = lazy(() => import("./pages/SettingsHealthPage").then((m) => ({ default: m.SettingsHealthPage })));
const WatchlistPage = lazy(() => import("./pages/WatchlistPage").then((m) => ({ default: m.WatchlistPage })));
const MarketIntelligencePage = lazy(() => import("./pages/MarketIntelligencePage").then((m) => ({ default: m.MarketIntelligencePage })));

interface NavigationItem {
  label: TranslationKey;
  to: string;
  icon: string;
  end?: boolean;
}

const navigationItems: NavigationItem[] = [
  { label: "nav.dashboard", to: "/", icon: "⌂", end: true },
  { label: "nav.signals", to: "/predictions", icon: "⌁" },
  { label: "nav.watchlist", to: "/watchlist", icon: "◉" },
  { label: "nav.markets", to: "/markets", icon: "▥" },
  { label: "nav.calendar", to: "/calendar", icon: "□" },
  { label: "nav.news", to: "/news", icon: "≡" },
  { label: "nav.heatmap", to: "/heatmap", icon: "▦" },
  { label: "nav.consult", to: "/consult", icon: "✦" },
  { label: "nav.backtest", to: "/replay", icon: "↻" },
  { label: "nav.performance", to: "/performance", icon: "⌁" },
  { label: "nav.settings", to: "/settings", icon: "⚙" },
];

export interface ApplicationShellProps {
  apiClient?: ApplicationShellApiClient;
}

function NavigationLinks() {
  const { t } = useI18n();
  return (
    <ul className="nav-list">
      {navigationItems.map((item) => (
        <li key={item.to}>
          <NavLink
            className={({ isActive }) =>
              `nav-entry${isActive ? " nav-entry--active" : ""}`
            }
            end={item.end}
            to={item.to}
          >
            <span className="nav-entry__icon" aria-hidden="true">{item.icon}</span>
            <span className="nav-entry__label">{t(item.label)}<small aria-hidden="true">{item.to === "/" ? "Dashboard" : item.to.slice(1).replace("predictions", "Signals").replace("consult", "AI Assistant").replace("replay", "Replay / Backtest")}</small></span>
          </NavLink>
        </li>
      ))}
    </ul>
  );
}

function DesktopNavigation({ healthState, health }: { healthState: BackendHealthState; health?: HealthResponse }) {
  const { t } = useI18n();
  return (
    <aside className="nav-ledger">
      <div className="brand-lockup">
        <span className="brand-lockup__sigil" aria-hidden="true">L</span>
        <div><p className="brand-lockup__eyebrow">{t("shell.aiMarketAnalyst")}</p><p className="brand-lockup__title">LUNA TERMINAL</p><p className="brand-lockup__meta">{t("shell.localFirst")}</p></div>
      </div>
      <nav aria-label={t("shell.primaryNavigation")}>
        <p className="nav-heading">{t("shell.workspace")}</p>
        <NavigationLinks />
      </nav>
      <HealthStatus state={healthState} health={health} />
    </aside>
  );
}

function MobileNavigation() {
  const { t } = useI18n();
  return (
    <div className="mobile-nav-shell">
      <details>
        <summary>{t("shell.openWorkspaceMenu")}</summary>
        <nav aria-label={t("shell.mobileNavigation")}>
          <NavigationLinks />
        </nav>
      </details>
    </div>
  );
}

function LanguageSelector() {
  const { language, setLanguage, t } = useI18n();
  return (
    <div className="language-selector">
      <label htmlFor="language-select">{t("language.label")}</label>
      <select
        id="language-select"
        value={language}
        onChange={(event) => setLanguage(event.target.value === "zh-CN" ? "zh-CN" : "en")}
      >
        <option value="zh-CN">{t("language.chinese")}</option>
        <option value="en">{t("language.english")}</option>
      </select>
    </div>
  );
}

function routeKey(pathname: string): TranslationKey {
  if (pathname === "/") {
    return "nav.dashboard";
  }
  if (pathname.startsWith("/assets/")) {
    return "nav.assetDetail";
  }
  const matchingItem = navigationItems.find((item) => item.to !== "/" && pathname.startsWith(item.to));
  return matchingItem?.label ?? "shell.workspace";
}

interface PlaceholderPageProps {
  title: string;
  description: string;
  nextTask?: string;
}

function PlaceholderPage({ title, description, nextTask = "a later Phase 4 task" }: PlaceholderPageProps) {
  return (
    <section className="page-placeholder" aria-labelledby="page-title">
      <div className="page-placeholder__topline">
        <p className="eyebrow">Foundation surface</p>
        <span className="status-chip status-chip--neutral">Not implemented</span>
      </div>
      <h1 id="page-title">{title}</h1>
      <p className="page-placeholder__description">{description}</p>
      <div className="placeholder-note" role="note">
        <span className="placeholder-note__label">Scope note</span>
        <span>Page implementation follows in {nextTask}.</span>
      </div>
      <p className="page-placeholder__boundary">No market data, signal, performance metric or recommendation is shown on this foundation route.</p>
    </section>
  );
}

function NotFoundPage() {
  return (
    <PlaceholderPage
      description="This workspace route is not registered in the current frontend foundation."
      title="Route not found"
    />
  );
}

function RouteFallback() {
  const { t } = useI18n();
  return (
    <section className="terminal-loading" aria-live="polite">
      <span className="loading-orbit" />
      {t("common.loading")}
    </section>
  );
}

function WorkspaceRoutes({
  apiClient,
  onProvenanceChange,
}: {
  apiClient: ApplicationShellApiClient;
  onProvenanceChange: (provenance: DashboardProvenance) => void;
}) {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Routes>
      <Route index element={<DashboardPage apiClient={apiClient} onProvenanceChange={onProvenanceChange} />} />
      <Route
        path="watchlist"
        element={<WatchlistPage apiClient={apiClient} />}
      />
      <Route path="markets" element={<MarketIntelligencePage apiClient={apiClient} surface="markets" />} />
      <Route path="calendar" element={<MarketIntelligencePage apiClient={apiClient} surface="calendar" />} />
      <Route path="news" element={<MarketIntelligencePage apiClient={apiClient} surface="news" />} />
      <Route path="heatmap" element={<MarketIntelligencePage apiClient={apiClient} surface="heatmap" />} />
      <Route path="assets/:symbol" element={<AssetDetailPage apiClient={apiClient} onProvenanceChange={onProvenanceChange} />} />
      <Route
        path="predictions"
        element={<PredictionsPage apiClient={apiClient} onProvenanceChange={onProvenanceChange} />}
      />
      <Route
        path="paper-trades"
        element={<PaperTradesPage apiClient={apiClient} onProvenanceChange={onProvenanceChange} />}
      />
      <Route
        path="performance"
        element={<PerformancePage apiClient={apiClient} onProvenanceChange={onProvenanceChange} />}
      />
      <Route
        path="replay"
        element={<ReplayLabPage apiClient={apiClient} />}
      />
      <Route path="alerts" element={<AlertsPage apiClient={apiClient} />} />
      <Route path="consult" element={<QwenConsultPage apiClient={apiClient} />} />
      <Route path="settings" element={<SettingsHealthPage apiClient={apiClient} />} />
      <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </Suspense>
  );
}

function ApplicationShellContent({ apiClient = defaultApiClient }: ApplicationShellProps) {
  const { t } = useI18n();
  const location = useLocation();
  const [healthState, setHealthState] = useState<BackendHealthState>("loading");
  const [health, setHealth] = useState<HealthResponse>();
  const [provenance, setProvenance] = useState<DashboardProvenance>(() => emptyDashboardProvenance(t));
  const handleProvenanceChange = useCallback((next: DashboardProvenance) => setProvenance(next), []);

  useEffect(() => {
    if (location.pathname !== "/") {
      setProvenance(emptyDashboardProvenance(t));
    }
  }, [location.pathname, t]);

  useEffect(() => {
    const controller = new AbortController();
    setHealthState("loading");
    setHealth(undefined);

    void apiClient
      .health(controller.signal)
      .then((response) => {
        if (!controller.signal.aborted) {
          setHealth(response);
          setHealthState("connected");
        }
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && !(error instanceof Error && error.name === "AbortError")) {
          setHealthState("unavailable");
        }
      });

    return () => controller.abort();
  }, [apiClient]);

  return (
    <div className="app-frame" data-i18n-root="true">
      <a className="skip-link" href="#main-content">
        {t("shell.skipToContent")}
      </a>
      <DesktopNavigation health={health} healthState={healthState} />
      <div className="app-column">
        <MobileNavigation />
        <header className="workspace-header">
          <div>
            <p className="eyebrow">{t("shell.privateWorkstation")}</p>
            <p className="workspace-header__route">{t("shell.researchDesk")} / {t(routeKey(location.pathname))}</p>
          </div>
          <div className="workspace-header__controls">
            <p className="workspace-header__note">{t("shell.orientation")}</p>
            <LanguageSelector />
          </div>
        </header>
        <main className="workspace-main" id="main-content" tabIndex={-1}>
          <div className="route-surface">
            <WorkspaceRoutes apiClient={apiClient} onProvenanceChange={handleProvenanceChange} />
          </div>
          <aside className="provenance-column" aria-label={t("shell.timeProvenance")}>
            <TimeProvenanceRail
              generatedAt={provenance.generatedAt}
              reevaluateAt={provenance.reevaluateAt}
              expiresAt={provenance.expiresAt}
              dataSource={provenance.dataSource}
              model={provenance.model}
              state={provenance.state}
              footer={<p>{provenance.footer}</p>}
            />
          </aside>
        </main>
      </div>
    </div>
  );
}

export function ApplicationShell(props: ApplicationShellProps) {
  return (
    <I18nProvider>
      <ApplicationShellContent {...props} />
    </I18nProvider>
  );
}
