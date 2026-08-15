import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TimeProvenanceRail } from "./TimeProvenanceRail";

describe("TimeProvenanceRail", () => {
  it("labels the neutral timeline and provenance without relying on color", () => {
    render(<TimeProvenanceRail />);

    expect(screen.getByRole("list", { name: "Signal time provenance" })).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Generated: Not set" })).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Re-evaluate: Not set" })).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Expiry: Not set" })).toBeInTheDocument();
    expect(screen.getByText("Neutral placeholder")).toBeInTheDocument();
    expect(screen.getAllByText("Awaiting signal data")).toHaveLength(3);
    expect(screen.getByText("Data source")).toBeInTheDocument();
    expect(screen.getByText("Model")).toBeInTheDocument();
  });
});
