import { useEffect, useState } from 'react';
import { api } from '../api';
import type { AssetDetail } from '../types';
import { DASH, age, compact, maturityColor, num, pct, price, scoreColor } from '../format';
import { VerdictPanel } from './VerdictBadge';

interface Props {
  symbol: string;
  onClose: () => void;
}

export function AssetDrawer({ symbol, onClose }: Props) {
  const [data, setData] = useState<AssetDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    api.asset(symbol)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(String(e.message ?? e)); });
    return () => { cancelled = true; };
  }, [symbol]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer">
        <div className="drawer-head">
          <div>
            <h2>{symbol}</h2>
            {data && <div className="price">{price(data.price)}</div>}
          </div>
          <button className="close-btn" onClick={onClose} aria-label="Close">×</button>
        </div>

        {error && <div className="error-box">Could not load {symbol}: {error}</div>}
        {!data && !error && <div className="loading">Loading…</div>}

        {data && (
          <>
            {data.verdict && <VerdictPanel verdict={data.verdict} />}

            <div className="score-grid">
              <Box k="Opportunity" v={num(data.final_score, 1)} color={scoreColor(data.final_score)} />
              <Box k="Acceleration" v={num(data.acceleration.momentum_acceleration, 0)} />
              <Box k="Maturity" v={num(data.pump_maturity.score, 0)} color={maturityColor(data.pump_maturity.score)} />
              <Box k="Safety" v={num(data.safety.score, 0)} />
              <Box k="Confidence" v={num(data.data_confidence.score, 0)}
                   color={data.data_confidence.score < 60 ? 'var(--down)' : undefined} />
            </div>

            <div className="panel">
              <h3>Status</h3>
              <div className="kv"><span className="k">Setup state</span>
                <span className="v"><span className={`pill ${data.setup.state}`}>{data.setup.state}</span></span></div>
              <div className="kv"><span className="k">Liquidity</span>
                <span className="v"><span className={`pill ${data.liquidity.status}`}>{data.liquidity.status}</span></span></div>
              <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 7 }}>{data.setup.rationale}</div>
              {data.setup.trigger && (
                <div className="kv" style={{ marginTop: 7 }}>
                  <span className="k">Trigger</span><span className="v" style={{ textAlign: 'right', maxWidth: '68%' }}>{data.setup.trigger}</span>
                </div>
              )}
            </div>

            <ScoreHistory points={data.score_history} />

            <div className="panel">
              <h3>Score explainability</h3>
              {data.explainability.breakdown.map((b) => (
                <div key={b.component} className={`breakdown-row ${b.points === 0 ? 'zero' : ''}`}>
                  <div className="name">{b.component}</div>
                  <div className="bar"><div className="fill" style={{ width: `${(b.points / b.max) * 100}%` }} /></div>
                  <div className="pts">+{b.points.toFixed(1)} / {b.max}</div>
                </div>
              ))}
              <div className="math">
                <div className="line"><span>RAW_SCORE</span><span>{data.explainability.raw_score.toFixed(2)}</span></div>
                <div className="line"><span>RISK_PENALTY</span><span>−{data.explainability.risk_penalty.toFixed(2)}</span></div>
                <div className="line total"><span>FINAL_SCORE</span><span>{data.explainability.final_score.toFixed(2)} / 100</span></div>
              </div>
            </div>

            {data.explainability.penalties.length > 0 && (
              <div className="panel">
                <h3>Risk penalties applied</h3>
                <ul>
                  {data.explainability.penalties.map((p, i) => (
                    <li key={i} className="risk">−{p.points.toFixed(1)} · {p.reason}</li>
                  ))}
                </ul>
              </div>
            )}

            <div className="panel">
              <h3>Why this asset?</h3>
              {data.why_this_asset.length > 0
                ? <ul>{data.why_this_asset.map((w, i) => <li key={i}>{w}</li>)}</ul>
                : <div className="empty" style={{ padding: 10 }}>Nothing notable.</div>}
            </div>

            <div className="panel">
              <h3>What can invalidate it?</h3>
              {data.what_can_invalidate_it.length > 0
                ? <ul>{data.what_can_invalidate_it.filter(Boolean).map((w, i) => <li key={i} className="risk">{w}</li>)}</ul>
                : <div className="empty" style={{ padding: 10 }}>No specific invalidation identified.</div>}
            </div>

            <TimeframePanel data={data} />
            <MarketPanel data={data} />

            <div className="panel">
              <h3>Data provenance</h3>
              <div className="kv"><span className="k">Engine</span><span className="v">{data.engine_version}</span></div>
              <div className="kv"><span className="k">Data age</span>
                <span className="v">{age(data.data_confidence.max_age_seconds)}</span></div>
              {data.data_confidence.issues.map((issue, i) => (
                <div key={i} style={{ fontSize: 11, color: 'var(--warn)', marginTop: 4 }}>! {issue}</div>
              ))}
            </div>
          </>
        )}
      </aside>
    </>
  );
}

function Box({ k, v, color }: { k: string; v: string; color?: string }) {
  return (
    <div className="score-box">
      <div className="k">{k}</div>
      <div className="v" style={color ? { color } : undefined}>{v}</div>
    </div>
  );
}

function ScoreHistory({ points }: { points: AssetDetail['score_history'] }) {
  if (points.length < 2) {
    return (
      <div className="panel">
        <h3>Score history</h3>
        <div className="empty" style={{ padding: 12 }}>
          Only {points.length} observation so far. Score acceleration needs at least two scans.
        </div>
      </div>
    );
  }

  const w = 600;
  const h = 54;
  const scores = points.map((p) => p.final_score);
  const min = Math.min(...scores, 0);
  const max = Math.max(...scores, 100);
  const path = points
    .map((p, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = h - ((p.final_score - min) / (max - min || 1)) * h;
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');

  const first = points[0].final_score;
  const last = points[points.length - 1].final_score;
  const delta = last - first;

  return (
    <div className="panel">
      <h3>Score history ({points.length} scans)</h3>
      <svg className="sparkline" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
        <path d={path} fill="none" stroke="var(--accent)" strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="kv" style={{ marginTop: 6 }}>
        <span className="k">Oldest → newest</span>
        <span className="v">
          {first.toFixed(0)} → {last.toFixed(0)}{' '}
          <span className={delta > 0 ? 'pos' : delta < 0 ? 'neg' : 'muted'}>
            ({delta >= 0 ? '+' : ''}{delta.toFixed(1)})
          </span>
        </span>
      </div>
    </div>
  );
}

function TimeframePanel({ data }: { data: AssetDetail }) {
  const tfs = data.features?.timeframes;
  if (!tfs) return null;
  const order = ['5m', '15m', '1h', '4h', '1d'];
  const present = order.filter((t) => tfs[t]);
  return (
    <div className="panel">
      <h3>Multi-timeframe</h3>
      <div className="tf-grid">
        {present.map((t) => {
          const f = tfs[t];
          return (
            <div className="tf-box" key={t}>
              <div className="tf">{t}</div>
              <div className={`bias ${f.bias}`}>{f.bias}</div>
              <div className="sub">RSI {num(f.rsi14, 0)}</div>
              <div className="sub">RVOL {f.rvol === null ? DASH : `${num(f.rvol)}x`}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function MarketPanel({ data }: { data: AssetDetail }) {
  const f = data.features;
  const p = f?.timeframes?.['5m'];
  const st = p?.structure;
  return (
    <div className="panel">
      <h3>Market data</h3>
      <div className="kv"><span className="k">24h quote volume</span><span className="v">{compact(f?.quote_volume_24h)}</span></div>
      <div className="kv"><span className="k">24h change</span><span className="v">{pct(f?.price_change_pct_24h ?? null)}</span></div>
      <div className="kv"><span className="k">Spread</span>
        <span className="v">{f?.spread_bps === null || f?.spread_bps === undefined ? DASH : `${num(f.spread_bps, 1)} bps`}</span></div>
      <div className="kv"><span className="k">Order book imbalance</span>
        <span className="v">{f?.order_book_imbalance === null || f?.order_book_imbalance === undefined
          ? DASH : num(f.order_book_imbalance, 3)}</span></div>
      <div className="kv"><span className="k">ATR (5m)</span>
        <span className="v">{p?.atr_pct === null || p?.atr_pct === undefined ? DASH : `${num(p.atr_pct)}%`}</span></div>
      <div className="kv"><span className="k">Resistance</span>
        <span className="v">{st?.nearest_resistance ? price(st.nearest_resistance.price) : DASH}</span></div>
      <div className="kv"><span className="k">Distance to breakout</span>
        <span className="v">{st?.distance_to_resistance_atr === null || st?.distance_to_resistance_atr === undefined
          ? DASH : `${num(st.distance_to_resistance_atr)} ATR`}</span></div>
      <div className="kv"><span className="k">Support</span>
        <span className="v">{st?.nearest_support ? price(st.nearest_support.price) : DASH}</span></div>
    </div>
  );
}
