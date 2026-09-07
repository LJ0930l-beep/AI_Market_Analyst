import { useState } from "react";
import type { ApplicationShellApiClient } from "../api/client";
import { useV2Copy } from "../v2copy";
import { SettingsHealthPage } from "./SettingsHealthPage";
import { V2WorkspacePage } from "./V2WorkspacePage";

export function V2SettingsPage({
  apiClient,
}: {
  apiClient: ApplicationShellApiClient;
}) {
  const copy = useV2Copy();
  const [ledger, setLedger] = useState(false);
  return (
    <>
      <div className="v2-controls">
        <button aria-pressed={!ledger} onClick={() => setLedger(false)}>
          {copy.preferences}
        </button>
        <button aria-pressed={ledger} onClick={() => setLedger(true)}>
          {copy.ledger}
        </button>
      </div>
      {ledger ? (
        <V2WorkspacePage surface="ledger" />
      ) : (
        <SettingsHealthPage apiClient={apiClient} />
      )}
    </>
  );
}
