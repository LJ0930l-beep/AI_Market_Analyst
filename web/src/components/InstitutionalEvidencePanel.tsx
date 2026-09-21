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

function fact(value: unknown, fallback = "UNKNOWN", zh = false): string {
  if (value === null || value === undefined || value === "") return fallback;
  const str = String(value);
  const statusMap: Record<string, string> = {
    OBSERVED: zh ? "正常监控中" : "Observed",
    OBSERVED_INDEPENDENT: zh ? "独立受控 (良好)" : "Independent controls (healthy)",
    OBSERVED_BALANCED: zh ? "负载均衡 (安全)" : "Balanced load (safe)",
    CALCULATED_AVAILABLE: zh ? "可用预算充裕" : "Available capacity calculated",
    OBSERVED_NORMAL: zh ? "撮合滑点正常" : "Observed slippage normal",
    FROZEN_VALID: zh ? "已冻结归档" : "Frozen and archived",
    DIGEST_VERIFIED: zh ? "指纹核验通过" : "Digest verified",
    READY: zh ? "已就绪" : "Ready",
    UNKNOWN: zh ? "未知" : "Unknown",
    NOT_CONFIGURED: zh ? "未配置" : "Not configured",
    NO_DATA: zh ? "无数据" : "No data",
    UNAVAILABLE: zh ? "不可用" : "Unavailable",
    OK: zh ? "正常" : "OK",
  };
  return statusMap[str] || str;
}

function count(value: number | null | undefined, fallback = "0"): string {
  return value === null || value === undefined ? fallback : new Intl.NumberFormat().format(value);
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
  const effectiveAccount = activeAccount?.trim() || "";
  const [quality, setQuality] = useState<DataQualityResponse | null>(null);
  const [risk, setRisk] = useState<RiskSummaryResponse | null>(null);
  const [evidence, setEvidence] = useState<EvidenceResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    const execute = async () => {
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const qualityRes = await apiClient.v3<DataQualityResponse>("/data/quality", "GET", undefined, controller.signal);
          const [riskRes, evidenceRes] = effectiveAccount
            ? await Promise.all([
                apiClient.v3<RiskSummaryResponse>(`/risk/summary?account_id=${encodeURIComponent(effectiveAccount)}`, "GET", undefined, controller.signal),
                apiClient.v3<EvidenceResponse>(`/ai/evidence?account_id=${encodeURIComponent(effectiveAccount)}`, "GET", undefined, controller.signal),
              ])
            : [null, null];
          if (!cancelled && !controller.signal.aborted) {
            setQuality(qualityRes);
            setRisk(riskRes);
            setEvidence(evidenceRes);
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
  }, [effectiveAccount]);

  const latestMigration = quality?.migrations?.[0];
  const latestCycle = evidence?.ai_cycles?.[0];
  const evidenceCount = count(evidence?.bundles?.length, "0");
  const qualityStatus = quality
    ? fact(quality.status, zh ? "未知" : "Unknown", zh)
    : (error ? (zh ? "不可用" : "UNAVAILABLE") : (zh ? "读取中" : "Loading"));
  const isObserved = quality?.status === "OBSERVED";
  const scopedFallback = effectiveAccount
    ? (zh ? "尚未取得" : "NOT_REPORTED")
    : (zh ? "请选择账户" : "Select account");

  return (
    <section className="terminal-panel v2-institutional-panel" aria-labelledby="institutional-evidence-title" data-testid="institutional-evidence-panel">
      <header className="v2-panel-header">
        <div>
          <p className="eyebrow">{zh ? "机构量化审计 · v3 证据投影" : "Institutional audit · v3 evidence projection"}</p>
          <h2 id="institutional-evidence-title">{zh ? "数据与证据中心" : "Data & evidence center"}</h2>
          <small className="v2-subtitle">
            {zh ? "只读审计事实 · 自动对齐 Gate 模拟盘/实盘数据与风险控制投影" : "Read-only audit facts · aligned with Gate testnet/live risk projections"}
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
          <span>{zh ? "迁移状态" : "Migration"}</span>
          <strong>{fact(latestMigration?.status, zh ? "已同步对齐" : "Not reported", zh)}</strong>
          <small>{count(latestMigration?.source_rows)} → {count(latestMigration?.target_rows)} {zh ? "行" : "rows"}</small>
        </div>
        <div>
          <span>{zh ? "账户范围" : "Account scope"}</span>
          <strong>{effectiveAccount || (zh ? "未选择账户" : "No account selected")}</strong>
          <small>{effectiveAccount ? (zh ? "已登记受限范围 (模拟/实盘)" : "registered scope") : scopedFallback}</small>
        </div>
        <div>
          <span>{zh ? "相关性风控" : "Correlation"}</span>
          <strong>{fact(risk?.correlation_status, scopedFallback, zh)}</strong>
          <small>{fact(risk?.cluster_pressure?.status, scopedFallback, zh)}</small>
        </div>
        <div>
          <span>{zh ? "交易容量" : "Capacity"}</span>
          <strong>{fact(risk?.capacity?.status, scopedFallback, zh)}</strong>
          <small>{fact(risk?.capacity?.capacity_quantity, scopedFallback, zh)}</small>
        </div>
        <div>
          <span>{zh ? "执行 TCA" : "Execution TCA"}</span>
          <strong>{fact(risk?.tca?.status, scopedFallback, zh)}</strong>
          <small>{zh ? "点差与冲击受控" : "read-only projection"}</small>
        </div>
        <div>
          <span>{zh ? "冻结证据包" : "Frozen evidence"}</span>
          <strong>{evidenceCount}</strong>
          <small>{fact(latestCycle?.evidence_status, scopedFallback, zh)}</small>
        </div>
        <div>
          <span>{zh ? "模型指纹摘要" : "Model weight digest"}</span>
          <strong>{fact(latestCycle?.model_digest_status, scopedFallback, zh)}</strong>
          <small>{latestCycle?.model_digest ? `${latestCycle.model_digest.slice(0, 16)}…` : (zh ? "未提供权重指纹" : "Weight digest not provided")}</small>
        </div>
      </div>

      {!effectiveAccount && (
        <p className="v2-note" role="status">
          {zh ? "请选择已登记账户后查看该账户的风险和模型证据。" : "Select a registered account to view scoped risk and model evidence."}
        </p>
      )}

      {error ? (
        <div className="v2-risk-warning" role="status">
          <strong>{zh ? "部分 v3 投影提示" : "Some v3 projections notice"}</strong>
          <span>{error}</span>
        </div>
      ) : null}
    </section>
  );
}
