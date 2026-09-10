import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";

// Existing English UI contract tests select English explicitly. Production
// first-run defaults to Chinese; dedicated locale tests override this choice.
beforeEach(() => {
  window.localStorage.setItem("ai-market-analyst.language", "en");
  window.sessionStorage.clear();
});

afterEach(() => cleanup());
