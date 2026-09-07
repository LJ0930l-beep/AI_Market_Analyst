import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "../api/client";
import { I18nProvider } from "../i18n";
import { V2WorkspacePage } from "./V2WorkspacePage";
import { createFakeClient } from "../test/fakeClient";

function show(surface:"dashboard"|"monitor"|"strategies"="dashboard") {
  render(<I18nProvider><MemoryRouter><V2WorkspacePage surface={surface}/></MemoryRouter></I18nProvider>);
}
beforeEach(()=>{
  const fake=createFakeClient();
  vi.spyOn(apiClient,"marketIntelligence").mockImplementation(fake.marketIntelligence);
  vi.spyOn(apiClient,"chartBars").mockImplementation(fake.chartBars);
  vi.spyOn(apiClient,"v2").mockResolvedValue({watchlist:[{symbol:"BTCUSDT"}],subscriptions:[],runtime:{state:"stopped"},decisions:[],positions:[],allow_unknown_macro:false});
});
afterEach(()=>vi.restoreAllMocks());
describe("V2 task workspace",()=>{
  it("shows useful deterministic data and explicit analysis without model POST",async()=>{
    show();
    expect(await screen.findByText(/No strategy proposal yet/)).toBeInTheDocument();
    expect(screen.getByRole("link",{name:"Ask AI explicitly"})).toHaveAttribute("href","/consult?symbol=BTCUSDT");
    expect(apiClient.v2).toHaveBeenCalledWith("/workspace","GET",undefined,expect.any(AbortSignal));
    expect(screen.getByText(/Structured macro provider not configured/)).toBeInTheDocument();
  });
  it("discloses backend failures without simulated values",async()=>{
    vi.mocked(apiClient.v2).mockRejectedValue(new Error("offline"));show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Backend unavailable");
    expect(screen.getByText(/No stored bars yet/)).toBeInTheDocument();
  });
  it("enables a strategy only through explicit user action",async()=>{
    show("monitor");
    const toggle=await screen.findByRole("checkbox");
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    await waitFor(()=>expect(apiClient.v2).toHaveBeenCalledWith("/subscriptions/BTCUSDT/ema_trend","PUT",{enabled:true,params:{}}));
  });
});
