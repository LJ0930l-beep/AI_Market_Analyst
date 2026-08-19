import { useCallback, useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";

import { apiClient as defaultApiClient, type ApplicationShellApiClient } from "./api/client";
import type { HealthResponse } from "./api/types";
import { HealthStatus, type BackendHealthState } from "./components/HealthStatus";
import { TimeProvenanceRail } from "./components/TimeProvenanceRail";
import { DashboardPage, EMPTY_DASHBOARD_PROVENANCE, type DashboardProvenance } from "./pages/DashboardPage";
import { AssetDetailPage } from "./pages/AssetDetailPage";
import { PaperTradesPage } from "./pages/PaperTradesPage";
import { PerformancePage } from "./pages/PerformancePage";
import { PredictionsPage } from "./pages/PredictionsPage";
import { SettingsHealthPage } from "./pages/SettingsHealthPage";

interface NavigationItem {
  label: string;
  to: string;
  end?: boolean;
}

const navigationItems: NavigationItem[] = [
  { label: "Dashboard", to: "/", end: true },
  { label: "Watchlist", to: "/watchlist" },
  { label: "Asset detail", to: "/assets/NVDA" },
  { label: "Predictions", to: "/predictions" },
  { label: "Paper trades", to: "/paper-trades" },
  { label: "Performance", to: "/performance" },
  { label: "Replay lab", to: "/replay" },
  { label: "Settings / health", to: "/settings" },
];

export interface ApplicationShellProps {
  apiClient?: ApplicationShellApiClient;
}

function NavigationLinks() {
  const location = useLocation();
  return (
    <ul className="nav-list">
      {navigationItems.map((item) => (
        <li key={item.to}>
          <NavLink
            aria-current={
              item.to === "/assets/NVDA" && location.pathname.startsWith("/assets/") ? "page" : undefined
            }
            className={({ isActive }) =>
              `nav-entry${isActive || (item.to === "/assets/NVDA" && location.pathname.startsWith("/assets/")) ? " nav-entry--active" : ""}`
            }
            end={item.end}
            to={item.to}
          >
            <span className="nav-entry__mark" aria-hidden="true" />
            <span>{item.label}</span>
          </NavLink>
        </li>
      ))}
    </ul>
  );
}

function DesktopNavigation({ healthState, health }: { healthState: BackendHealthState; health?: HealthResponse }) {
  return (
    <aside className="nav-ledger">
      <div className="brand-lockup">
        <p className="brand-lockup__eyebrow">AI Market Analyst</p>
        <p className="brand-lockup__title">Research desk</p>
        <p className="brand-lockup__meta">Local-first · paper-only</p>
      </div>
      <nav aria-label="Primary navigation">
        <p className="nav-heading">Workspace</p>
        <NavigationLinks />
      </nav>
      <HealthStatus state={healthState} health={health} />
    </aside>
  );
}

function MobileNavigation() {
  return (
    <div className="mobile-nav-shell">
      <details>
        <summary>Open workspace menu</summary>
        <nav aria-label="Mobile navigation">
          <NavigationLinks />
        </nav>
      </details>
    </div>
  );
}

function routeLabel(pathname: string): string {
  if (pathname === "/") {
    return "Dashboard";
  }
  if (pathname.startsWith("/assets/")) {
    const routeSymbol = pathname.slice("/assets/".length).split("/")[0];
    let symbol = routeSymbol;
    try {
      symbol = decodeURIComponent(routeSymbol);
    } catch {
      symbol = routeSymbol;
    }
    return symbol ? `Asset detail / ${symbol.toUpperCase()}` : "Asset detail";
  }
  const matchingItem = navigationItems.find((item) => item.to !== "/" && pathname.startsWith(item.to));
  return matchingItem?.label ?? "Workspace";
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

function WorkspaceRoutes({
  apiClient,
  onProvenanceChange,
}: {
  apiClient: ApplicationShellApiClient;
  onProvenanceChange: (provenance: DashboardProvenance) => void;
}) {
  return (
    <Routes>
      <Route index element={<DashboardPage apiClient={apiClient} onProvenanceChange={onProvenanceChange} />} />
      <Route
        path="watchlist"
        element={<PlaceholderPage description="A focused place for saved instruments will live here." title="Watchlist" />}
      />
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
        element={<PlaceholderPage description="Replay run provenance will be inspected here." title="Replay lab" />}
      />
      <Route path="settings" element={<SettingsHealthPage apiClient={apiClient} />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}

export function ApplicationShell({ apiClient = defaultApiClient }: ApplicationShellProps) {
  const location = useLocation();
  const [healthState, setHealthState] = useState<BackendHealthState>("loading");
  const [health, setHealth] = useState<HealthResponse>();
  const [provenance, setProvenance] = useState<DashboardProvenance>(EMPTY_DASHBOARD_PROVENANCE);
  const handleProvenanceChange = useCallback((next: DashboardProvenance) => setProvenance(next), []);

  useEffect(() => {
    if (location.pathname !== "/") {
      setProvenance(EMPTY_DASHBOARD_PROVENANCE);
    }
  }, [location.pathname]);

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
    <div className="app-frame">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <DesktopNavigation health={health} healthState={healthState} />
      <div className="app-column">
        <MobileNavigation />
        <header className="workspace-header">
          <div>
            <p className="eyebrow">Private local workstation</p>
            <p className="workspace-header__route">Research desk / {routeLabel(location.pathname)}</p>
          </div>
          <p className="workspace-header__note">Orientation first · evidence follows</p>
        </header>
        <main className="workspace-main" id="main-content" tabIndex={-1}>
          <div className="route-surface">
            <WorkspaceRoutes apiClient={apiClient} onProvenanceChange={handleProvenanceChange} />
          </div>
          <aside className="provenance-column" aria-label="Time and provenance">
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
