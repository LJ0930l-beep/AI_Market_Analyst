import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "../App";
import { ApiError } from "../api/client";
import type { ReplayRun } from "../api/types";
import { createFakeClient, fakeReplayRun } from "../test/fakeClient";

function renderReplay(overrides: Parameters<typeof createFakeClient>[0] = {}) {
  return render(
    <MemoryRouter initialEntries={["/replay"]}>
      <ApplicationShell apiClient={createFakeClient(overrides)} />
    </MemoryRouter>,
  );
}

describe("ReplayLabPage", () => {
  it("filters and paginates bounded run rows, then loads allowlisted detail", async () => {
    const page = Array.from({ length: 25 }, (_, index): ReplayRun => ({
      ...fakeReplayRun,
      run_id: `replay-${index}`,
      status: "RUNNING",
    }));
    const replayRuns = vi.fn().mockResolvedValue(page);
    let resolveDetail: (run: ReplayRun) => void = () => undefined;
    const replayRun = vi.fn().mockReturnValue(new Promise<ReplayRun>((resolve) => { resolveDetail = resolve; }));
    renderReplay({ replayRuns, replayRun });

    await screen.findByRole("button", { name: "replay-0" });
    fireEvent.change(screen.getByLabelText("Run status"), { target: { value: "RUNNING" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(replayRuns).toHaveBeenLastCalledWith(
      { status: "RUNNING", limit: 25, offset: 0 },
      expect.any(AbortSignal),
    ));

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(replayRuns).toHaveBeenLastCalledWith(
      { status: "RUNNING", limit: 25, offset: 25 },
      expect.any(AbortSignal),
    ));

    fireEvent.click(screen.getByRole("button", { name: "replay-0" }));
    const detail = screen.getByRole("region", { name: "Replay run detail" });
    expect(within(detail).getByText("Loading")).toBeInTheDocument();
    expect(within(detail).getByText(/Request: pending/)).toBeInTheDocument();
    await act(async () => resolveDetail({ ...fakeReplayRun, run_id: "replay-0" }));
    expect(within(detail).getByText("manifest-fixture")).toBeInTheDocument();
    expect(within(detail).getByText("phase2-json-v8")).toBeInTheDocument();
    expect(within(detail).getByText("Minimum history bars")).toBeInTheDocument();
    expect(within(detail).getByText("Sample coverage and status")).toBeInTheDocument();
    expect(within(detail).getByText("replay-prediction-fixture")).toBeInTheDocument();
    expect(replayRun).toHaveBeenCalledWith("replay-0", expect.any(AbortSignal));
    expect(screen.getByText("Neutral placeholder")).toBeInTheDocument();
  });

  it("shows completed-with-errors provenance and never renders unknown raw payloads", async () => {
    const sentinel = "REPLAY_RAW_CONTEXT_SENTINEL_72ac";
    const run: ReplayRun = {
      ...fakeReplayRun,
      run_id: "replay-errors",
      status: "COMPLETED_WITH_ERRORS",
      error_code: "sample_errors",
      raw_model_response: sentinel,
      context_json: sentinel,
      config: { samples: 2, context_json: sentinel },
      sampling_policy: { min_history_bars: 120, raw_model_response: sentinel },
      samples: [{
        run_id: "replay-errors",
        symbol: "NVDA",
        timeframe: "1h",
        as_of: "2030-01-01T20:00:00Z",
        status: "ERROR",
        error_code: "provider_timeout",
        context_json: sentinel,
      }],
    };
    renderReplay({ replayRuns: vi.fn().mockResolvedValue([run]), replayRun: vi.fn().mockResolvedValue(run) });

    fireEvent.click(await screen.findByRole("button", { name: run.run_id }));
    const detail = await screen.findByRole("region", { name: "Replay run detail" });
    expect(within(detail).getByText("Degraded")).toBeInTheDocument();
    expect(within(detail).getAllByText("sample_errors").length).toBeGreaterThan(0);
    expect(within(detail).getByText(/completed with one or more sample errors/i)).toBeInTheDocument();
    expect(within(detail).getByText(/provider_timeout/)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(sentinel);
  });

  it("states PENDING and RUNNING truth without exposing replay mutation controls", async () => {
    const pending: ReplayRun = { ...fakeReplayRun, run_id: "replay-pending", status: "PENDING", completed_at: null };
    const running: ReplayRun = { ...fakeReplayRun, run_id: "replay-running", status: "RUNNING", completed_at: null };
    const replayRun = vi.fn((runId: string) => Promise.resolve(runId === pending.run_id ? pending : running));
    renderReplay({ replayRuns: vi.fn().mockResolvedValue([pending, running]), replayRun });

    expect(await screen.findByText(/API-created requests do not self-execute/)).toBeInTheDocument();
    expect(screen.getByText(/actual replay execution remains an explicit CLI workflow/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: pending.run_id }));
    expect(await screen.findByText(/PENDING is stored metadata only/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: running.run_id }));
    expect(await screen.findByText(/RUNNING is the returned stored status/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /create replay|start replay|resume replay|retry run/i })).not.toBeInTheDocument();
  });

  it("distinguishes empty and unavailable run lists with scoped retry", async () => {
    const emptyThenLoaded = vi.fn().mockResolvedValueOnce([]).mockResolvedValueOnce([fakeReplayRun]);
    const emptyView = renderReplay({ replayRuns: emptyThenLoaded });

    expect(await screen.findByText("No replay runs matched the current status filter.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry Replay runs" }));
    expect(await screen.findByRole("button", { name: fakeReplayRun.run_id })).toBeInTheDocument();
    emptyView.unmount();

    const unavailableThenLoaded = vi.fn()
      .mockRejectedValueOnce(new ApiError(503, { code: "REPLAY_UNAVAILABLE", message: "replay store unavailable" }))
      .mockResolvedValueOnce([fakeReplayRun]);
    renderReplay({ replayRuns: unavailableThenLoaded });

    expect(await screen.findByRole("alert")).toHaveTextContent("REPLAY_UNAVAILABLE: replay store unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Retry Replay runs" }));
    expect(await screen.findByRole("button", { name: fakeReplayRun.run_id })).toBeInTheDocument();
  });
});
