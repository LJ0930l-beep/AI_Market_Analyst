import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ApplicationShell } from "../App";
import { ApiError } from "../api/client";
import type { CalibrationCurrent, PerformanceBuckets, PerformanceSummary } from "../api/types";
import { createFakeClient } from "../test/fakeClient";

function renderPerformance(client = createFakeClient()) {
  return render(
    <MemoryRouter initialEntries={["/performance"]}>
      <ApplicationShell apiClient={client} />
    </MemoryRouter>,
  );
}

function populatedSummary(): PerformanceSummary {
  return {
    scope: { source_type: "live", symbol: "NVDA", timeframe: "1h", model_id: "model-x", prompt_version: "phase2-json-v8" },
    status: "PRELIMINARY",
    source_type: "live",
    sample_count: 12,
    actionable_count: 8,
    metrics: {
      status: "PRELIMINARY",
      sample_count: 12,
      actionable_count: 8,
      resolved_actionable: 6,
      pending_actionable: 2,
      wait_count: 4,
      invalid_count: 0,
      wins: 4,
      losses: 2,
      win_rate: 0.6666666667,
      avg_r: 0.41,
      expectancy_r: 0.38,
      profit_factor: 1.9,
      max_drawdown_r: -0.7,
      mfe_r_avg: 0.8,
      mae_r_avg: -0.3,
      timeout_count: 1,
      timeout_rate: 0.1,
      coverage: 0.75,
      action_rate: 0.6666666667,
      wait_rate: 0.3333333333,
      brier_raw: 0.123456789,
      brier_calibrated: null,
      ece_raw: 0.04,
      ece_calibrated: null,
    },
  };
}

function populatedBuckets(): PerformanceBuckets {
  return {
    scope: { source_type: "live", model_id: "model-x" },
    confidence_buckets: [
      { bucket: "0.6-0.7", count: 3, raw_avg_confidence: 0.6123456789, empirical_win_rate: 0.6666666667, calibrated_confidence: 0.64 },
    ],
  };
}

function populatedCalibration(): CalibrationCurrent {
  return {
    status: "ACTIVE",
    calibration_id: "calibration-1",
    version: "v1",
    scope: { source_type: "live", model_id: "model-x", prompt_version: "phase2-json-v8" },
    method: "beta",
    sample_count: 6,
    trained_until: "2030-01-02T12:00:00Z",
    brier_raw: 0.123456789,
    brier_calibrated: 0.1,
    ece_raw: 0.04,
    ece_calibrated: 0.03,
    fallback: null,
    buckets: [{ lower: 0.6, upper: 0.7, n: 3, wins: 2, empirical_rate: 0.6666666667, shrunk_rate: 0.64 }],
  };
}

describe("PerformancePage", () => {
  it("shows PRELIMINARY returned metrics and confidence/calibration buckets", async () => {
    const summary = vi.fn().mockResolvedValue(populatedSummary());
    const buckets = vi.fn().mockResolvedValue(populatedBuckets());
    const calibration = vi.fn().mockResolvedValue(populatedCalibration());
    const client = createFakeClient({ performanceSummary: summary, performanceBuckets: buckets, calibrationCurrent: calibration });
    renderPerformance(client);

    const summaryPanel = await screen.findByRole("region", { name: "Performance summary" });
    expect(within(summaryPanel).getByText("PRELIMINARY")).toBeInTheDocument();
    expect(within(summaryPanel).getByText("0.123457")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Confidence buckets" })).getByText("0.6123456789")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Current calibration" })).getByText("calibration-1")).toBeInTheDocument();
    const calibrationPanel = screen.getByRole("region", { name: "Current calibration" });
    expect(within(calibrationPanel).getByText("Loaded")).toBeInTheDocument();
    expect(within(calibrationPanel).queryByText("Degraded")).not.toBeInTheDocument();
    expect(screen.getByText(/separate artifact/i)).toBeInTheDocument();
  });

  it("marks an insufficient current calibration as degraded with its returned status", async () => {
    const calibration = vi.fn().mockResolvedValue({ status: "INSUFFICIENT_SAMPLE", sample_count: 0, buckets: [] });
    const client = createFakeClient({ calibrationCurrent: calibration });
    renderPerformance(client);

    const calibrationPanel = await screen.findByRole("region", { name: "Current calibration" });
    expect(await within(calibrationPanel).findByText("Degraded")).toBeInTheDocument();
    expect(within(calibrationPanel).getByText(/status is INSUFFICIENT_SAMPLE; no fitted calibration is active/i)).toBeInTheDocument();
    expect(within(calibrationPanel).getByText("INSUFFICIENT_SAMPLE")).toBeInTheDocument();
  });

  it("treats a positive total sample with zero resolved actionable as degraded", async () => {
    const zeroResolved: PerformanceSummary = {
      ...populatedSummary(),
      sample_count: 7,
      actionable_count: 3,
      metrics: { ...populatedSummary().metrics, sample_count: 7, actionable_count: 3, resolved_actionable: 0, win_rate: null, expectancy_r: null },
    };
    const client = createFakeClient({ performanceSummary: vi.fn().mockResolvedValue(zeroResolved) });
    renderPerformance(client);

    const summaryPanel = await screen.findByRole("region", { name: "Performance summary" });
    expect(within(summaryPanel).getByText("Degraded")).toBeInTheDocument();
    expect(within(summaryPanel).getByText(/No resolved actionable sample is available/)).toBeInTheDocument();
    expect(summaryPanel.textContent).not.toMatch(/Resolved actionable\s*7/);
    expect(within(summaryPanel).getAllByText("Not supplied").length).toBeGreaterThan(0);
  });

  it("loads performance panels independently and sends the supported filter scopes", async () => {
    const summary = vi.fn().mockRejectedValue(new ApiError(400, { code: "INVALID_SYMBOL", message: "unsupported symbol" }));
    const buckets = vi.fn().mockResolvedValue(populatedBuckets());
    const calibration = vi.fn().mockRejectedValue(new Error("calibration unavailable"));
    const client = createFakeClient({ performanceSummary: summary, performanceBuckets: buckets, calibrationCurrent: calibration });
    const first = renderPerformance(client);

    expect(await within(screen.getByRole("region", { name: "Performance summary" })).findByRole("alert")).toHaveTextContent("INVALID_SYMBOL: unsupported symbol");
    expect(within(screen.getByRole("region", { name: "Confidence buckets" })).getByText("0.6123456789")).toBeInTheDocument();
    expect(await within(screen.getByRole("region", { name: "Current calibration" })).findByRole("alert")).toHaveTextContent("endpoint did not return");

    fireEvent.change(screen.getByLabelText("Source type"), { target: { value: "replay" } });
    fireEvent.change(screen.getByLabelText("Symbol"), { target: { value: "msft" } });
    fireEvent.change(screen.getByLabelText("Timeframe"), { target: { value: "15m" } });
    fireEvent.change(screen.getByLabelText("Model id"), { target: { value: "model-y" } });
    fireEvent.change(screen.getByLabelText("Prompt version"), { target: { value: "prompt-2" } });
    fireEvent.change(screen.getByLabelText("Replay run"), { target: { value: "run-7" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(summary).toHaveBeenLastCalledWith({ source_type: "replay", symbol: "MSFT", timeframe: "15m", model_id: "model-y", prompt_version: "prompt-2", replay_run_id: "run-7" }, expect.any(AbortSignal)));
    await waitFor(() => expect(buckets).toHaveBeenLastCalledWith({ source_type: "replay", model_id: "model-y", prompt_version: "prompt-2", replay_run_id: "run-7" }, expect.any(AbortSignal)));
    first.unmount();
  });
});
