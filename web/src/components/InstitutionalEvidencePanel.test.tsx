import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "../api/client";
import { InstitutionalEvidencePanel } from "./InstitutionalEvidencePanel";

describe("institutional evidence panel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders v3 data, risk, evidence and model-digest states without inventing facts", async () => {
    const v3 = vi.spyOn(apiClient, "v3").mockImplementation(async (path: string) => {
      if (path === "/data/quality") {
        return {
          status: "OBSERVED",
          row_count: 24,
          instrument_count: 2,
          migrations: [{ status: "COMPLETED", source_rows: 24, target_rows: 24 }],
        } as never;
      }
      if (path.startsWith("/risk/summary")) {
        return {
          correlation_status: "UNKNOWN_CONSERVATIVE_FALLBACK",
          cluster_pressure: { status: "CONSERVATIVE_FALLBACK" },
          capacity: { status: "NOT_CONFIGURED", capacity_quantity: null },
          tca: { status: "PARTIAL_OR_COST_UNKNOWN" },
        } as never;
      }
      if (path.startsWith("/ai/evidence")) {
        return {
          bundles: [{ missing: ["model_weight_digest"] }],
          ai_cycles: [{ model_digest_status: "UNKNOWN", model_digest: null, evidence_status: "EVIDENCE_INSUFFICIENT" }],
        } as never;
      }
      return {} as never;
    });

    render(<InstitutionalEvidencePanel activeAccount="paper_test" />);

    const panel = await screen.findByTestId("institutional-evidence-panel");
    await waitFor(() => expect(panel).toHaveTextContent("OBSERVED"));
    expect(panel).toHaveTextContent("24 rows");
    expect(panel).toHaveTextContent("UNKNOWN_CONSERVATIVE_FALLBACK");
    expect(panel).toHaveTextContent("NOT_CONFIGURED");
    expect(panel).toHaveTextContent("EVIDENCE_INSUFFICIENT");
    expect(panel).toHaveTextContent("UNKNOWN");
    expect(v3).toHaveBeenCalledWith("/data/quality", "GET", undefined, expect.any(AbortSignal));
    expect(v3).toHaveBeenCalledWith("/risk/summary?account_id=paper_test", "GET", undefined, expect.any(AbortSignal));
  });

  it("does not query account projections until an account is selected", async () => {
    const v3 = vi.spyOn(apiClient, "v3").mockResolvedValue({ status: "NO_DATA", row_count: 0, instrument_count: 0 } as never);

    render(<InstitutionalEvidencePanel />);

    const panel = await screen.findByTestId("institutional-evidence-panel");
    await waitFor(() => expect(panel).toHaveTextContent("account required"));
    expect(v3).toHaveBeenCalledTimes(1);
    expect(v3).toHaveBeenCalledWith("/data/quality", "GET", undefined, expect.any(AbortSignal));
  });
});
