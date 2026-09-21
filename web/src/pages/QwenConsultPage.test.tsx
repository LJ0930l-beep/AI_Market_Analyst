import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "../App";
import type { ApplicationShellApiClient } from "../api/client";
import type { ConsultRequest, ConsultStreamEvent } from "../api/types";
import { createFakeClient } from "../test/fakeClient";
import { CONSULT_SESSION_KEY } from "./QwenConsultPage";

function renderConsult(client: ApplicationShellApiClient = createFakeClient(), entry = "/consult") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <ApplicationShell apiClient={client} />
    </MemoryRouter>,
  );
}

describe("QwenConsultPage", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.localStorage.setItem("ai-market-analyst.language", "en");
  });

  it("renders incremental chunks, context evidence and persists the session", async () => {
    const consultStream = vi.fn(createFakeClient().consultStream);
    renderConsult(createFakeClient({ consultStream }), "/consult?symbol=NVDA");

    expect(await screen.findByText("Local model is available")).toBeInTheDocument();
    expect(screen.getByLabelText("Optional instrument context")).toHaveValue("NVDA");
    fireEvent.change(screen.getByLabelText("Message the local model"), { target: { value: "Explain the saved evidence" } });
    fireEvent.keyDown(screen.getByLabelText("Message the local model"), { key: "Enter", shiftKey: false });

    expect(await screen.findByText("Fixture local model answer.")).toBeInTheDocument();
    expect(screen.getByText("Response complete")).toBeInTheDocument();
    expect(screen.getByText("Verified model").parentElement).toHaveTextContent("Bonsai-2-27B-PTQ1_0");
    const context = screen.getByRole("region", { name: "Read-only context evidence" });
    expect(within(context).getByText("2030-01-02T12:00:00Z")).toBeInTheDocument();
    expect(within(context).getByText("durable_latest_live_prediction")).toBeInTheDocument();
    expect(consultStream).toHaveBeenCalledWith(
      expect.objectContaining({ language: "en", symbol: "NVDA", messages: [{ role: "user", content: "Explain the saved evidence" }] }),
      expect.any(Function),
      expect.any(AbortSignal),
    );
    expect(window.sessionStorage.getItem(CONSULT_SESSION_KEY)).toContain("Fixture local model answer.");
  });

  it("uses the selected Chinese language for UI and the server-owned prompt request", async () => {
    window.localStorage.setItem("ai-market-analyst.language", "zh-CN");
    const consultStream = vi.fn(createFakeClient().consultStream);
    renderConsult(createFakeClient({ consultStream }));

    expect(await screen.findByRole("heading", { name: "本地模型咨询" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("发送给本地模型"), { target: { value: "请解释当前证据" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await screen.findByText("Fixture local model answer.");
    expect(consultStream).toHaveBeenCalledWith(
      expect.objectContaining({ language: "zh-CN", messages: [{ role: "user", content: "请解释当前证据" }] }),
      expect.any(Function),
      expect.any(AbortSignal),
    );
  });

  it("aborts an in-flight stream and reports that generation stopped", async () => {
    const consultStream = vi.fn((_request: ConsultRequest, onEvent: (event: ConsultStreamEvent) => void, signal?: AbortSignal) => {
      onEvent({
        type: "meta",
        contract_version: "qwen_consult_v2",
        request_id: "blocking-request",
        provider: "fake_local_qwen",
        model_id: "Bonsai-2-27B-PTQ1_0",
        context: { status: "unavailable", sources: [], missing_reasons: ["symbol_not_selected"], read_only: true },
      });
      return new Promise<void>((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    });
    renderConsult(createFakeClient({ consultStream }));

    await screen.findByText("Local model is available");
    fireEvent.change(screen.getByLabelText("Message the local model"), { target: { value: "Keep generating" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    fireEvent.click(await screen.findByRole("button", { name: "Stop generating" }));

    expect(await screen.findByText("Generation stopped")).toBeInTheDocument();
    expect((consultStream.mock.calls[0]?.[2] as AbortSignal).aborted).toBe(true);
  });

  it("restores and explicitly clears browser-session messages", async () => {
    window.sessionStorage.setItem(CONSULT_SESSION_KEY, JSON.stringify({
      messages: [{ role: "user", content: "Restored question" }, { role: "assistant", content: "Restored answer" }],
      symbol: "NVDA",
    }));
    renderConsult();

    expect(await screen.findByText("Restored answer")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear conversation" }));
    const confirmation = screen.getByRole("group", { name: "Clear every message from this browser session?" });
    fireEvent.click(within(confirmation).getByRole("button", { name: "Clear now" }));

    expect(screen.queryByText("Restored answer")).not.toBeInTheDocument();
    expect(screen.getByText("No messages yet. Ask a research question to begin.")).toBeInTheDocument();
    const stored = JSON.parse(window.sessionStorage.getItem(CONSULT_SESSION_KEY) ?? "{}") as { messages?: unknown[] };
    expect(stored.messages ?? []).toEqual([]);
  });

  it("shows unavailable and structured stream errors without fabricated answers", async () => {
    const unavailable = createFakeClient({
      modelHealth: vi.fn().mockResolvedValue({
        provider: "ollama",
        available: false,
        consult: { contract_version: "qwen_consult_v2", configured: true, available: false, model_id: "Bonsai-2-27B-PTQ1_0" },
      }),
    });
    const first = renderConsult(unavailable);
    expect(await screen.findAllByText("Local model is unavailable")).not.toHaveLength(0);
    expect(screen.getByText("local_model_consult_v2")).toBeInTheDocument();
    expect(screen.queryByText("qwen_consult_v2")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Message the local model")).toBeDisabled();
    first.unmount();

    const failedStream = vi.fn(async (_request: ConsultRequest, onEvent: (event: ConsultStreamEvent) => void) => {
      onEvent({ type: "error", error: { code: "QWEN_STREAM_INTERRUPTED", message: "The local Qwen stream ended unexpectedly." } });
    });
    renderConsult(createFakeClient({ consultStream: failedStream }));
    await screen.findByText("Local model is available");
    fireEvent.change(screen.getByLabelText("Message the local model"), { target: { value: "Fail safely" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("QWEN_STREAM_INTERRUPTED");
    expect(screen.queryByText("Fixture local model answer.")).not.toBeInTheDocument();
  });
});
