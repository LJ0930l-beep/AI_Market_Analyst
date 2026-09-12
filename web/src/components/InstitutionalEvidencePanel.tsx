import { useEffect, useState } from "react";
import { apiClient } from "../api/client";
import { useI18n } from "../i18n";

interface DataQualityResponse {
  status?: string | null;
  row_count?: number | null;
  instrument_count?: number | null;
  migrations?: Array<{
    status?: string | null;
    source_rows?: number | null;
    target_rows?: number | null;
    duplicate_rows?: number | null;
  }>;
}

interface RiskSummaryResponse {
  correlation_status?: string | null;
  cluster_pressure?: { status?: string | null } | null;
  capacity?: { status?: string | null; capacity_quantity?: string | null } | null;
  tca?: { status?: string | null } | null;
}

interface EvidenceResponse {
  bundles?: Array<{ missing?: string[] }>;
  ai_cycles?: Array<{
    model_digest_status?: string | null;
    model_digest?: string | null;
    evidence_status?: string | null;
  }>;
}

function fact(value: unknown): string {
  return value === null || value === undefined || value === "" ? "UNKNOWN" : String(value);
}

function count(value: number | null | undefined): string {
  return value === null || value === undefined ? "UNKNOWN" : new Intl.NumberFormat().format(value);
}

function requestError(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}

export interface InstitutionalEvidencePanelProps {
  activeAccount?: string | null;
}

/** Read-only v3 evidence, risk and data-quality projection for the Chinese workstation. */
export function InstitutionalEvidencePanel({ activeAccount }: InstitutionalEvidencePanelProps) {
  const { language } = useI18n();
  const zh = language === "zh-CN";
  const [quality, setQuality] = useState<DataQualityResponse | null>(null);
  const [risk, setRisk] = useState<RiskSummaryResponse | null>(null);
  const [evidence, setEvidence] = useState<EvidenceResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const account = activeAccount?.trim();
    let cancelled = false;

    const execute = async () => {
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const [qualityRes, riskRes, evidenceRes] = await Promise.all([
            apiClient.v3<DataQualityResponse>("/data/quality", "GET", undefined, controller.signal),
            account
              ? apiClient.v3<RiskSummaryResponse>(`/risk/summary?account_id=${encodeURIComponent(account)}`, "GET", undefined, controller.signal)
              : Promise.resolve(null),
            account
              ? apiClient.v3<EvidenceResponse>(`/ai/evidence?account_id=${encodeURIComponent(account)}`, "GET", undefined, controller.signal)
              : Promise.resolve(null),
          ]);
          if (!cancelled && !controller.signal.aborted) {
            setQuality(qualityRes);
            if (riskRes) setRisk(riskRes);
            if (evidenceRes) setEvidence(evidenceRes);
            setError(null);
            return;
          }
        } catch (err) {
          if (cancelled || controller.signal.aborted) return;
          if (attempt < 2) {
            await new Promise((r) => setTimeout(r, 800));
          } else {
            setError(requestError(err));
          }
        }
      }
    };

    void execute();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [activeAccount]);

  const latestMigration = quality?.migrations?.[0];
  const latestCycle = evidence?.ai_cycles?.[0];
  const evidenceCount = evidence ? count(evidence.bundles?.length ?? 0) : "UNKNOWN";
  const qualityStatus = fact(quality?.status);
  const isObserved = quality?.status === "OBSERVED";

  return (
    <section className="terminal-panel v2-institutional-panel" aria-labelledby="institutional-evidence-title" data-testid="institutional-evidence-panel">
      <header className="v2-panel-header">
        <div>
          <p className="eyebrow">{zh ? "机构量化审计 · v3 证据投影" : "Institutional audit · v3 evidence projection"}</p>
          <h2 id="institutional-evidence-title">{zh ? "数据与证据中心" : "Data & evidence center"}</h2>
          <small className="v2-subtitle">
            {zh ? "只读事实 · 未配置项保持 UNKNOWN / NOT_CONFIGURED" : "Read-only facts · unavailable dependencies remain UNKNOWN / NOT_CONFIGURED"}
          </small>
        </div>
        <span className={`v2-badge ${isObserved ? "v2-badge--bull" : "v2-badge--warning"}`}>{qualityStatus}</span>
      </header>

      <div className="v2-institutional-grid">
        <div>
          <span>{zh ? "数据质量" : "Data quality"}</span>
          <strong>{qualityStatus}</strong>
          <small>{count(quality?.row_count)} {zh ? "行" : "rows"} · {count(quality?.instrument_count)} {zh ? "标的" : "instruments"}</small>
        </div>
        <div>
          <span>{zh ? "迁移" : "Migration"}</span>
          <strong>{fact(latestMigration?.status)}</strong>
          <small>{count(latestMigration?.source_rows)} → {count(latestMigration?.target_rows)} {zh ? "行" : "rows"}</small>
        </div>
        <div>
          <span>{zh ? "账户范围" : "Account scope"}</span>
          <strong>{fact(activeAccount)}</strong>
          <small>{activeAccount ? (zh ? "已登记范围" : "registered scope") : (zh ? "需要选择账户" : "account required")}</small>
        </div>
        <div>
          <span>{zh ? "相关性" : "Correlation"}</span>
          <strong>{fact(risk?.correlation_status)}</strong>
          <small>{fact(risk?.cluster_pressure?.status)}</small>
        </div>
        <div>
          <span>{zh ? "容量" : "Capacity"}</span>
          <strong>{fact(risk?.capacity?.status)}</strong>
          <small>{fact(risk?.capacity?.capacity_quantity)}</small>
        </div>
        <div>
          <span>{zh ? "执行 TCA" : "Execution TCA"}</span>
          <strong>{fact(risk?.tca?.status)}</strong>
          <small>{zh ? "只读投影" : "read-only projection"}</small>
        </div>
        <div>
          <span>{zh ? "冻结证据包" : "Frozen evidence"}</span>
          <strong>{evidenceCount}</strong>
          <small>{latestCycle?.evidence_status ? fact(latestCycle.evidence_status) : "UNKNOWN"}</small>
        </div>
        <div>
          <span>{zh ? "模型权重摘要" : "Model weight digest"}</span>
          <strong>{fact(latestCycle?.model_digest_status)}</strong>
          <small>{latestCycle?.model_digest ? `${latestCycle.model_digest.slice(0, 16)}…` : "UNKNOWN"}</small>
        </div>
      </div>

      {!activeAccount ? <p className="v2-note">{zh ? "选择已登记账户后，才会查询账户风险和 AI 证据；此处不会访问私有交易账户。" : "Select a registered account to query account risk and AI evidence; private trading accounts are never accessed here."}</p> : null}
      {error ? (
        <div className="v2-risk-warning" role="status">
          <strong>{zh ? "部分 v3 投影不可用" : "Some v3 projections are unavailable"}</strong>
          <span>{error}</span>
        </div>
      ) : null}
    </section>
  );
}
