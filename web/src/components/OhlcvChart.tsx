import type { MarketBar } from "../api/types";

interface OhlcvChartProps {
  symbol: string;
  timeframe: string;
  bars: MarketBar[];
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

function formatNumber(value: number): string {
  return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

export function OhlcvChart({ symbol, timeframe, bars }: OhlcvChartProps) {
  const usableBars = bars.filter(isUsableBar);
  if (usableBars.length === 0) {
    return (
      <div className="chart-empty" role="status">
        No OHLCV bars were supplied for this symbol and timeframe.
      </div>
    );
  }

  const width = 760;
  const height = 350;
  const left = 54;
  const right = 14;
  const top = 18;
  const priceBottom = 244;
  const volumeTop = 268;
  const volumeBottom = 330;
  const plotWidth = width - left - right;
  const highs = usableBars.map((bar) => bar.high);
  const lows = usableBars.map((bar) => bar.low);
  const high = Math.max(...highs);
  const low = Math.min(...lows);
  const priceRange = high - low || 1;
  const maxVolume = Math.max(...usableBars.map((bar) => bar.volume), 1);
  const slotWidth = plotWidth / usableBars.length;
  const bodyWidth = Math.max(2, Math.min(12, slotWidth * 0.58));
  const xFor = (index: number) => left + slotWidth * index + slotWidth / 2;
  const yForPrice = (value: number) => top + ((high - value) / priceRange) * (priceBottom - top);
  const yForVolume = (value: number) => volumeBottom - (value / maxVolume) * (volumeBottom - volumeTop);
  const latest = usableBars[usableBars.length - 1];
  const summary = `${symbol} ${timeframe} candlestick and volume chart with ${usableBars.length} returned bars, ordered from ${usableBars[0].timestamp} to ${latest.timestamp}. Latest close ${formatNumber(latest.close)}; supplied range ${formatNumber(low)} to ${formatNumber(high)}.`;

  return (
    <div className="ohlcv-chart">
      <p className="chart-summary" id="ohlcv-chart-summary">
        {summary}
      </p>
      <div
        aria-label={`${symbol} ${timeframe} OHLCV chart scroll region`}
        className="ohlcv-chart__viewport"
        tabIndex={0}
      >
        <svg
          className="ohlcv-chart__svg"
          role="img"
          aria-labelledby="ohlcv-chart-title ohlcv-chart-summary"
          viewBox={`0 0 ${width} ${height}`}
        >
          <title id="ohlcv-chart-title">{symbol} {timeframe} OHLCV chart</title>
          <line className="chart-grid-line" x1={left} x2={width - right} y1={priceBottom} y2={priceBottom} />
          <line className="chart-grid-line" x1={left} x2={width - right} y1={volumeTop} y2={volumeTop} />
          <text className="chart-axis-label" x={left - 8} y={top + 5} textAnchor="end">
            {formatNumber(high)}
          </text>
          <text className="chart-axis-label" x={left - 8} y={priceBottom} textAnchor="end">
            {formatNumber(low)}
          </text>
          <text className="chart-axis-label" x={left - 8} y={volumeTop + 5} textAnchor="end">
            Vol
          </text>
          {usableBars.map((bar, index) => {
            const x = xFor(index);
            const candleColor = bar.close >= bar.open ? "var(--long)" : "var(--short)";
            const bodyTop = yForPrice(Math.max(bar.open, bar.close));
            const bodyHeight = Math.max(1, Math.abs(yForPrice(bar.open) - yForPrice(bar.close)));
            return (
              <g key={`${bar.timestamp}-${index}`}>
                <line
                  className="chart-wick"
                  stroke={candleColor}
                  x1={x}
                  x2={x}
                  y1={yForPrice(bar.high)}
                  y2={yForPrice(bar.low)}
                />
                <rect
                  className="chart-candle"
                  fill={candleColor}
                  height={bodyHeight}
                  width={bodyWidth}
                  x={x - bodyWidth / 2}
                  y={bodyTop}
                />
                <rect
                  className="chart-volume"
                  fill={candleColor}
                  height={Math.max(1, volumeBottom - yForVolume(bar.volume))}
                  width={Math.max(2, bodyWidth)}
                  x={x - bodyWidth / 2}
                  y={yForVolume(bar.volume)}
                />
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}
