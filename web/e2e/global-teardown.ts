import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

export function validateRunDirectory(runDirectory: string, temporaryDirectory = os.tmpdir()): string {
  const resolvedRunDirectory = path.resolve(runDirectory);
  const resolvedTemporaryDirectory = path.resolve(temporaryDirectory);
  const relativeRunDirectory = path.relative(resolvedTemporaryDirectory, resolvedRunDirectory);
  const runDirectoryName = path.basename(resolvedRunDirectory);
  const isDirectTemporaryChild = relativeRunDirectory.length > 0
    && relativeRunDirectory !== ".."
    && !relativeRunDirectory.startsWith(`..${path.sep}`)
    && !path.isAbsolute(relativeRunDirectory)
    && !relativeRunDirectory.includes(path.sep);

  if (!isDirectTemporaryChild || !runDirectoryName.startsWith("ai-market-analyst-p4-")) {
    throw new Error("Refusing to clean a non-direct Phase 4 E2E temporary directory.");
  }
  return resolvedRunDirectory;
}

export default async function globalTeardown() {
  const runDirectory = process.env.P4_E2E_RUN_DIR;
  if (!runDirectory) {
    throw new Error("Refusing to clean an unspecified Phase 4 E2E temporary directory.");
  }
  await fs.rm(validateRunDirectory(runDirectory), { recursive: true, force: true });
}
