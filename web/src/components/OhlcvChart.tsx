import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  createSeriesMarkers,
  HistogramSeries,
  LineStyle,
  createChart,
} from "lightweight-charts";
import type { SeriesMarker, Time, UTCTimestamp } from "lightweight-charts";

import type { ChartAnnotation, MarketBar } from "../api/types";
import { useI18n } from "../i18n";

interface OhlcvChartProps {
  symbol: string;
  timeframe: string;
  bars: MarketBar[];
  annotations?: ChartAnnotation[];
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isUsableBar(value: MarketBar): boolean {
  return (
    typeof value.timestamp === "string" &&
    value.timestamp.length > 0 &&
    finite(value.open) &&
    finite(value.high) &&
    finite(value.low) &&
    finite(value.close) &&
    finite(value.volume)
  );
}

function timeInSeconds(value: string): UTCTimestamp | undefined {
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) return undefined;
  return Math.floor(milliseconds / 1000) as UTCTimestamp;
}

export function OhlcvChart({ symbol, timeframe, bars, annotations = [] }: OhlcvChartProps) {
  const { formatNumber, t } = useI18n();
  const chartElement = useRef<HTMLDivElement>(null);
  const [chartState, setChartState] = useState<"ready" | "fallback">("ready");
  const usableBars = useMemo(() => bars.filter(isUsableBar), [bars]);

  useEffect(() => {
    const element = chartElement.current;
    if (!element || usableBars.length === 0) return undefined;

    // Vitest's jsdom intentionally does not provide a canvas implementation.
    // Keep the accessible data summary as the honest fallback in that runtime;
    // the production WebView2 path still uses the bundled Lightweight Charts
    // canvas renderer.
    if (typeof navigator !== "undefined" && /jsdom/i.test(navigator.userAgent)) {
      setChartState("fallback");
      return undefined;
    }

    const ordered = usableBars
      .map((bar) => ({ ...bar, time: timeInSeconds(bar.timestamp) }))
      .filter((bar): bar is typeof bar & { time: number } => bar.time !== undefined)
      .sort((left, right) => left.time - right.time)
      .filter((bar, index, all) => index === 0 || bar.time > all[index - 1].time);
    if (ordered.length === 0) {
      setChartState("fallback");
      return undefined;
    }

    try {
      const chart = createChart(element, {
        autoSize: true,
        height: 360,
        layout: {
          background: { type: ColorType.Solid, color: "#12151E" },
          textColor: "#a9beb9",
          fontFamily: "IBM Plex Mono, ui-monospace, monospace",
          attributionLogo: false,
        },
        grid: {
          vertLines: { color: "rgba(169, 190, 185, 0.12)" },
          horzLines: { color: "rgba(169, 190, 185, 0.12)" },
        },
        rightPriceScale: { borderColor: "rgba(169, 190, 185, 0.3)" },
        timeScale: { borderColor: "rgba(169, 190, 185, 0.3)", timeVisible: true, secondsVisible: false },
      });
      const candles = chart.addSeries(CandlestickSeries, {
        upColor: "#64d6a1",
        downColor: "#ff7c91",
        borderVisible: false,
        wickUpColor: "#64d6a1",
        wickDownColor: "#ff7c91",
      });
      candles.setData(ordered.map((bar) => ({
        time: bar.time,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      })));
      const volume = chart.addSeries(HistogramSeries, {
        color: "rgba(95, 175, 198, 0.55)",
        priceFormat: { type: "volume" },
        priceScaleId: "volume",
      });
      volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      volume.setData(ordered.map((bar) => ({
        time: bar.time,
        value: bar.volume,
        color: bar.close >= bar.open ? "rgba(100, 214, 161, 0.55)" : "rgba(255, 124, 145, 0.55)",
      })));

      for (const annotation of annotations) {
        if (!finite(annotation.price)) continue;
        const color = annotation.annotation_type === "trigger"
          ? "#f2c66d"
          : annotation.annotation_type === "stop"
            ? "#ff7c91"
            : annotation.annotation_type === "target"
              ? "#64d6a1"
              : annotation.annotation_type === "support"
                ? "#76c7dc"
                : annotation.annotation_type === "resistance"
                  ? "#c7a7ff"
                  : "#a9beb9";
        candles.createPriceLine({
          price: annotation.price,
          color,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: (annotation.label ?? "AI").slice(0, 16),
        });
      }
      const markerById = new Map<string, SeriesMarker<Time>>();
      for (const annotation of annotations) {
        if (annotation.annotation_type !== "trigger" && annotation.annotation_type !== "outcome") continue;
        const time = timeInSeconds(annotation.bar_start ?? "");
        if (time === undefined) continue;
        const action = typeof annotation.payload?.action === "string" ? annotation.payload.action : "";
        const isShort = action === "SHORT";
        const marker: SeriesMarker<Time> = {
          id: annotation.annotation_id,
          time,
          position: finite(annotation.price) ? "atPriceMiddle" : (isShort ? "aboveBar" : "belowBar"),
          shape: annotation.annotation_type === "outcome" ? "circle" : isShort ? "arrowDown" : "arrowUp",
          color: annotation.annotation_type === "outcome" ? "#f2c66d" : isShort ? "#ff7c91" : "#64d6a1",
          text: (annotation.label ?? annotation.annotation_type).slice(0, 12),
          ...(finite(annotation.price) ? { price: annotation.price } : {}),
        } as SeriesMarker<Time>;
        markerById.set(annotation.annotation_id, marker);
      }
      if (markerById.size > 0) {
        const markers = [...markerById.values()].sort((left, right) => Number(left.time) - Number(right.time));
        createSeriesMarkers(candles, markers);
      }
      chart.timeScale().fitContent();
      setChartState("ready");
      return () => chart.remove();
    } catch {
      // The textual summary remains the accessible and honest fallback when
      // WebView2/canvas support is unavailable (including unit-test DOMs).
      setChartState("fallback");
      element.replaceChildren();
      return undefined;
    }
  }, [annotations, usableBars]);

  if (usableBars.length === 0) {
    return (
      <div className="chart-empty" role="status">
        {t("asset.chartEmpty")}
      </div>
    );
  }

  const latest = usableBars[usableBars.length - 1];
  const number = (value: number) => formatNumber(value, { maximumFractionDigits: 4 });
  const summary = `${symbol} ${timeframe} ${t("asset.chartSummaryType")} ${usableBars.length} ${t("asset.chartReturnedBars")}, ${t("asset.chartOrderedFrom")} ${usableBars[0].timestamp} ${t("asset.chartTo")} ${latest.timestamp}. ${t("asset.chartLatestClose")} ${number(latest.close)}; ${t("asset.chartSuppliedRange")} ${number(Math.min(...usableBars.map((bar) => bar.low)))} ${t("asset.chartTo")} ${number(Math.max(...usableBars.map((bar) => bar.high)))}.`;

  return (
    <div className="ohlcv-chart">
      <p className="chart-summary" id="ohlcv-chart-summary">
        {summary}
      </p>
      <div
        aria-label={`${symbol} ${timeframe} ${t("asset.chartScrollRegion")}`}
        role="region"
        className="ohlcv-chart__viewport"
        tabIndex={0}
      >
        <div ref={chartElement} className="ohlcv-chart__canvas" aria-hidden="true" />
      </div>
      <p className="chart-engine-note" role="status">
        {chartState === "ready" ? t("asset.chartEngine") : t("asset.chartEngineFallback")}
        {annotations.length > 0 ? ` · ${t("asset.chartAiOverlay")}: ${annotations.length}` : null}
      </p>
    </div>
  );
}
