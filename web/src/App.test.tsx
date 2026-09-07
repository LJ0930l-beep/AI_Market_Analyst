import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "./App";
import type { ApplicationShellApiClient } from "./api/client";
import { createFakeClient, fakeHealth } from "./test/fakeClient";

function renderShell(initialEntry: string, client: ApplicationShellApiClient) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <ApplicationShell apiClient={client} />
    </MemoryRouter>,
  );
}

function backendStatusRegion(): HTMLElement {
  const heading = screen.getByRole("heading", { name: "Backend status" });
  const region = heading.closest("section");
  if (!region) {
    throw new Error("backend status region was not rendered");
  }
  return region;
}

describe("application shell", () => {
  it("renders semantic navigation, skip navigation and current route state", async () => {
    const client = createFakeClient();
    renderShell("/strategies", client);

    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    expect(within(navigation).getByRole("link", { name: "Strategies" })).toHaveAttribute("aria-current", "page");
    expect(within(navigation).getAllByRole("link")).toHaveLength(6);
    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main-content");
    expect(await screen.findByRole("heading", { name: "Strategy library" })).toBeInTheDocument();
  });

  it("shows a distinct loading health state", () => {
    const pending = new Promise<typeof fakeHealth>(() => undefined);
    const client = createFakeClient({ health: vi.fn().mockReturnValue(pending) });
    renderShell("/", client);

    expect(within(backendStatusRegion()).getByRole("status")).toHaveTextContent("Checking backend");
    expect(screen.getByText(/model and provider status is separate/i)).toBeInTheDocument();
  });

  it("shows a distinct connected health state without claiming model health", async () => {
    const client = createFakeClient();
    renderShell("/", client);

    expect(await within(backendStatusRegion()).findByRole("status")).toHaveTextContent("Backend connected");
    expect(screen.getByText(/API health is available; model and provider status is separate/i)).toBeInTheDocument();
  });

  it("shows a distinct unavailable health state", async () => {
    const client = createFakeClient({ health: vi.fn().mockRejectedValue(new Error("offline")) });
    renderShell("/", client);

    expect(await within(backendStatusRegion()).findByRole("status")).toHaveTextContent("Backend unavailable");
    expect(screen.getByText(/model and provider status is unknown/i)).toBeInTheDocument();
  });

  it("switches the shell and primary route copy to Chinese with local persistence", async () => {
    const client = createFakeClient();
    renderShell("/predictions", client);

    fireEvent.change(await screen.findByLabelText("Language"), { target: { value: "zh-CN" } });

    expect(await screen.findByRole("heading", { name: "预测" })).toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "主导航" })).getByRole("link", { name: "自选与盯盘" })).toBeInTheDocument();
    expect(screen.getByLabelText("语言")).toHaveValue("zh-CN");
    expect(window.localStorage.getItem("ai-market-analyst.language")).toBe("zh-CN");
  });
});
