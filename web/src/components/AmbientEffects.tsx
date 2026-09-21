import { createContext, useContext, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from 'react';
import { useI18n } from '../i18n';
import './ambientEffects.css';

interface CandleData {
  x: number;
  open: number;
  close: number;
  high: number;
  low: number;
  isUp: boolean;
}

function readReducedMotionPreference(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

interface AmbientMotionState {
  enabled: boolean;
  visible: boolean;
  toggleMotion: () => void;
}

const AmbientMotionContext = createContext<AmbientMotionState | null>(null);

function useAmbientMotion(): AmbientMotionState {
  const state = useContext(AmbientMotionContext);
  if (!state) throw new Error('Ambient motion controls require AmbientEffectsProvider.');
  return state;
}

export function AmbientEffectsProvider({ children }: { children: ReactNode }) {
  const [motionPreference, setMotionPreference] = useState<boolean | null>(() => {
    try {
      const stored = localStorage.getItem('aima-motion');
      if (stored === 'on') return true;
      if (stored === 'off') return false;
    } catch {
      return null;
    }
    return null;
  });
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(readReducedMotionPreference);
  const [visible, setVisible] = useState(() => typeof document === 'undefined' || !document.hidden);
  const enabled = motionPreference ?? !prefersReducedMotion;

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const preference = window.matchMedia('(prefers-reduced-motion: reduce)');
    const handlePreferenceChange = (event: MediaQueryListEvent) => setPrefersReducedMotion(event.matches);
    setPrefersReducedMotion(preference.matches);
    if (typeof preference.addEventListener === 'function') {
      preference.addEventListener('change', handlePreferenceChange);
      return () => preference.removeEventListener('change', handlePreferenceChange);
    }
    preference.addListener(handlePreferenceChange);
    return () => preference.removeListener(handlePreferenceChange);
  }, []);

  useEffect(() => {
    const handleVisibility = () => setVisible(!document.hidden);
    document.addEventListener('visibilitychange', handleVisibility);
    return () => document.removeEventListener('visibilitychange', handleVisibility);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.motion = enabled ? 'on' : 'off';
  }, [enabled]);

  useEffect(() => () => {
    delete document.documentElement.dataset.motion;
  }, []);

  function toggleMotion() {
    const next = !enabled;
    setMotionPreference(next);
    try {
      localStorage.setItem('aima-motion', next ? 'on' : 'off');
    } catch {
      /* Optional preference */
    }
  }

  return <AmbientMotionContext.Provider value={{ enabled, visible, toggleMotion }}>{children}</AmbientMotionContext.Provider>;
}

export function AmbientMotionToggle() {
  const { enabled, toggleMotion } = useAmbientMotion();
  const { language } = useI18n();
  const chinese = language === 'zh-CN';
  const stateLabel = chinese ? (enabled ? '开' : '关') : (enabled ? 'On' : 'Off');
  return (
    <button
      type='button'
      className='motion-toggle'
      aria-label={chinese ? '环境动效开关' : 'Ambient motion toggle'}
      aria-pressed={enabled}
      title={chinese ? '开启或关闭背景 K 线与流光动效' : 'Toggle the ambient candlestick and light effects'}
      onClick={toggleMotion}
    >
      ✦ <span className='motion-toggle__label'>{chinese ? `动态背景 ${stateLabel}` : `Ambient motion ${stateLabel}`}</span>
    </button>
  );
}

export function AmbientEffects() {
  const { enabled, visible } = useAmbientMotion();

  // Generate a seamless tileable series of candlesticks that seamlessly repeats every 1600px
  const { topTrack, midTrack } = useMemo(() => {
    const generateTrack = (count: number, step: number, baseY: number, amp: number, seed: number) => {
      const singleWidth = count * step;
      const candles: CandleData[] = [];
      const points: [number, number][] = [];

      for (let i = 0; i < count; i++) {
        // Periodic sinusoidal trend to ensure seamless loop at boundary
        const angle = (i / count) * Math.PI * 2;
        const trend = Math.sin(angle * 2 + seed) * amp + Math.cos(angle * 4 + seed * 1.5) * (amp * 0.4);
        const open = baseY + trend;
        const bodyDelta = Math.sin(i * 1.7 + seed) * (amp * 0.65) + 8;
        const close = open + (i % 2 === 0 ? bodyDelta : -bodyDelta);
        const wickOffset = Math.abs(Math.cos(i * 2.3 + seed)) * (amp * 0.5) + 12;
        const high = Math.min(open, close) - wickOffset;
        const low = Math.max(open, close) + wickOffset;
        const x = i * step + step / 2;
        const isUp = close < open; // In SVG Y is downwards: lower Y means higher price

        candles.push({ x, open, close, high, low, isUp });
        points.push([x, (open + close) / 2]);
      }

      // Smooth path for moving average
      const path = points.reduce((acc, [x, y], idx) => {
        return idx === 0 ? `M ${x} ${y}` : `${acc} L ${x} ${y}`;
      }, '');

      return { candles, path, singleWidth };
    };

    // Top track: matches the prominent hero header wave in media_1789746566919.png
    // 32 candles * 50px = 1600px seamless loop
    const topTrack = generateTrack(32, 50, 75, 28, 1.2);
    // Mid track: background deeper layer, 32 candles * 55px = 1760px seamless loop
    const midTrack = generateTrack(32, 55, 120, 38, 3.7);

    return { topTrack, midTrack };
  }, []);

  return (
    <div className='ambient-root' aria-hidden='true' data-visible={enabled && visible}>
        {/* 1. Deep Cyber Auroras */}
        <div className='ambient-aurora ambient-aurora--cyan' />
        <div className='ambient-aurora ambient-aurora--emerald' />
        <div className='ambient-aurora ambient-aurora--purple' />
        <div className='ambient-aurora ambient-aurora--rose' />

        {/* 2. Cyber Grid */}
        <div className='ambient-grid' />

        {/* 3. Hero Header Kline Wave (Matches screenshot top wave) */}
        <div className='ambient-kline-container ambient-kline-container--top'>
          <div className='ambient-kline-track ambient-kline-track--top'>
            {[0, 1].map((copyIdx) => (
              <svg
                key={copyIdx}
                className='ambient-kline-svg'
                width={topTrack.singleWidth}
                height='160'
                viewBox={`0 0 ${topTrack.singleWidth} 160`}
              >
                <defs>
                  <filter id={`glow-cyan-${copyIdx}`} x='-30%' y='-30%' width='160%' height='160%'>
                    <feGaussianBlur stdDeviation='4' result='blur' />
                    <feMerge>
                      <feMergeNode in='blur' />
                      <feMergeNode in='SourceGraphic' />
                    </feMerge>
                  </filter>
                  <filter id={`glow-bull-${copyIdx}`} x='-30%' y='-30%' width='160%' height='160%'>
                    <feGaussianBlur stdDeviation='3' result='blur' />
                    <feMerge>
                      <feMergeNode in='blur' />
                      <feMergeNode in='SourceGraphic' />
                    </feMerge>
                  </filter>
                  <filter id={`glow-bear-${copyIdx}`} x='-30%' y='-30%' width='160%' height='160%'>
                    <feGaussianBlur stdDeviation='3' result='blur' />
                    <feMerge>
                      <feMergeNode in='blur' />
                      <feMergeNode in='SourceGraphic' />
                    </feMerge>
                  </filter>
                </defs>

                {/* Moving Average Line with Cyan Glow */}
                <path
                  d={topTrack.path}
                  className='ambient-line ambient-line--cyan'
                  filter={`url(#glow-cyan-${copyIdx})`}
                />

                {/* Prominent Candlesticks matching screenshot */}
                {topTrack.candles.map((c, i) => {
                  const y = Math.min(c.open, c.close);
                  const height = Math.max(6, Math.abs(c.close - c.open));
                  const candleClass = c.isUp ? 'ambient-candle--bull' : 'ambient-candle--bear';
                  const filterId = c.isUp ? `url(#glow-bull-${copyIdx})` : `url(#glow-bear-${copyIdx})`;

                  return (
                    <g key={i} className={`ambient-candle ${candleClass}`} filter={filterId}>
                      {/* Wick */}
                      <line x1={c.x} y1={c.high} x2={c.x} y2={c.low} className='ambient-wick' />
                      {/* Body */}
                      <rect
                        x={c.x - 11}
                        y={y}
                        width='22'
                        height={height}
                        rx='3'
                        className='ambient-body'
                      />
                    </g>
                  );
                })}
              </svg>
            ))}
          </div>
        </div>

        {/* 4. Mid/Lower Ambient Kline Wave */}
        <div className='ambient-kline-container ambient-kline-container--mid'>
          <div className='ambient-kline-track ambient-kline-track--mid'>
            {[0, 1].map((copyIdx) => (
              <svg
                key={copyIdx}
                className='ambient-kline-svg'
                width={midTrack.singleWidth}
                height='240'
                viewBox={`0 0 ${midTrack.singleWidth} 240`}
              >
                <path d={midTrack.path} className='ambient-line ambient-line--gold' />
                {midTrack.candles.map((c, i) => {
                  const y = Math.min(c.open, c.close);
                  const height = Math.max(6, Math.abs(c.close - c.open));
                  const candleClass = c.isUp ? 'ambient-candle--bull' : 'ambient-candle--bear';

                  return (
                    <g key={i} className={`ambient-candle ${candleClass}`}>
                      <line x1={c.x} y1={c.high} x2={c.x} y2={c.low} className='ambient-wick' />
                      <rect
                        x={c.x - 13}
                        y={y}
                        width='26'
                        height={height}
                        rx='4'
                        className='ambient-body'
                      />
                    </g>
                  );
                })}
              </svg>
            ))}
          </div>
        </div>

        {/* 5. Floating Quantum Sparks */}
        {Array.from({ length: 24 }, (_, i) => (
          <span
            key={i}
            className='ambient-spark'
            style={{
              '--x': `${((i * 41 + 7) % 100)}%`,
              '--y': `${((i * 29 + 17) % 100)}%`,
              '--size': `${(i % 3) * 1.5 + 2}px`,
              '--color': i % 3 === 0 ? '#00f2a9' : i % 3 === 1 ? '#00e5ff' : '#c084fc',
              '--duration': `${14 + (i % 5) * 3}s`,
              '--delay': `${-i * 1.5}s`,
            } as CSSProperties}
          />
        ))}
      </div>
  );
}
