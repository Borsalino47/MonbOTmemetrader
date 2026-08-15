import { useEffect, useState } from 'react';
import { api } from '../api';
import { DASH, num } from '../format';
import type { PerformanceResponse, Bucket } from '../types';

function pct(v: number | null | undefined): string {
  return v === null || v === undefined || !Number.isFinite(v) ? DASH : `${(v * 100).toFixed(1)}%`;
}

function expectancyClass(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return 'muted';
  return v > 0 ? 'pos' : v < 0 ? 'neg' : 'muted';
}

export function PerformanceView() {
  const [data, setData] = useState<PerformanceResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);

  async function load() {
    try {
      setData(await api.performance());
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, []);

  async function resolveNow() {
    setResolving(true);
    try {
      await api.resolveOutcomes();
      await load();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setResolving(false);
    }
  }

  if (error) return <div className="error-box">Could not load performance: {error}</div>;
  if (!data) return <div className="loading">Loading…</div>;

  const c = data.counts;
  const p = data.performance;
  const o = p.overall;
  const noneSettled = c.settled === 0;

  return (
    <div>
      <div className="toolbar">
        <div className="filter">
          <label>Label</label>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>{data.label.config}</span>
        </div>
        <div className="filter">
          <label>Round-trip cost</label>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>
            {num(data.costs.round_trip_cost_pct as number, 2)}%
          </span>
        </div>
        <button className="action" onClick={resolveNow} disabled={resolving} style={{ marginLeft: 'auto' }}>
          {resolving ? 'Resolving…' : 'Resolve now'}
        </button>
      </div>

      <div className="score-grid" style={{ marginBottom: 18 }}>
        <Stat k="Signals" v={String(c.total_signals)} />
        <Stat k="Settled" v={String(c.settled)} />
        <Stat k="Pending" v={String(c.pending_evaluation)} />
        <Stat k="Unresolvable" v={String(c.unresolvable)} />
        <Stat
          k="Win rate"
          v={noneSettled ? DASH : pct(o.win_rate)}
          color={noneSettled ? 'var(--text-faint)' : undefined}
        />
        <Stat
          k="Expectancy"
          v={noneSettled ? DASH : `${num(o.expectancy_pct)}%`}
          color={
            noneSettled ? 'var(--text-faint)' : (o.expectancy_pct ?? 0) > 0 ? 'var(--up)' : 'var(--down)'
          }
        />
      </div>

      {noneSettled && (
        <div className="table-wrap">
          <div className="empty">
            No signal has a verdict yet. The tracker grades a signal only once its full
            horizon ({data.label.config}) has elapsed, so this stays empty until the journal
            has had time to mature. Nothing here is estimated in the meantime.
          </div>
        </div>
      )}

      {!noneSettled && (
        <>
          {o.insufficient_sample && (
            <div className="banner stale" style={{ borderRadius: 6, marginBottom: 14 }}>
              <strong>SMALL SAMPLE</strong>
              <span>
                {o.n} settled signals, below the {p.min_sample} minimum. These figures describe
                what happened; they are not yet evidence about what will happen.
              </span>
            </div>
          )}

          <div className="panel">
            <h3>Definition</h3>
            <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>
              {data.label.definition}
            </div>
          </div>

          <div className="panel">
            <h3>Overall</h3>
            <div className="kv"><span className="k">Wins / losses / timeouts</span>
              <span className="v">{o.wins} / {o.losses} / {o.timeouts}</span></div>
            <div className="kv"><span className="k">Win rate</span><span className="v">{pct(o.win_rate)}</span></div>
            <div className="kv"><span className="k">Expectancy per signal</span>
              <span className={`v ${expectancyClass(o.expectancy_pct)}`}>{num(o.expectancy_pct)}%</span></div>
            <div className="kv"><span className="k">Average win / loss</span>
              <span className="v">{num(o.avg_win_pct)}% / {num(o.avg_loss_pct)}%</span></div>
            <div className="kv"><span className="k">Profit factor</span>
              <span className="v">{o.profit_factor === null ? DASH : num(o.profit_factor)}</span></div>
            <div className="kv"><span className="k">Average MFE / MAE</span>
              <span className="v">{num(o.avg_mfe_atr)} / {num(o.avg_mae_atr)} ATR</span></div>
          </div>

          <BucketTable title="By opportunity score" buckets={p.by_score_band} />
          <BucketTable title="By setup state" buckets={p.by_state} />
          <BucketTable title="By pump maturity" buckets={p.by_maturity_band} />
          <BucketTable title="By market regime" buckets={p.by_regime} />
          <BucketTable title="By liquidity" buckets={p.by_liquidity} />

          {p.component_edge.length > 0 && (
            <div className="panel">
              <h3>Component edge — average points, winners vs losers</h3>
              <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 10, lineHeight: 1.55 }}>
                A component that scores eventual winners and losers the same is contributing
                noise to the final score, however sensible it looked when it was written.
              </div>
              <table>
                <thead>
                  <tr>
                    <th className="left">Component</th>
                    <th>Winners</th>
                    <th>Losers</th>
                    <th>Edge</th>
                    <th>n</th>
                  </tr>
                </thead>
                <tbody>
                  {p.component_edge.map((c2) => (
                    <tr key={c2.component} style={c2.insufficient_sample ? { opacity: 0.5 } : undefined}>
                      <td className="left" style={{ textTransform: 'capitalize' }}>{c2.component}</td>
                      <td>{c2.avg_points_winners.toFixed(2)}</td>
                      <td>{c2.avg_points_losers.toFixed(2)}</td>
                      <td className={c2.edge > 0 ? 'pos' : c2.edge < 0 ? 'neg' : 'muted'}>
                        {c2.edge > 0 ? '+' : ''}{c2.edge.toFixed(2)}
                      </td>
                      <td className="muted">{c2.n_winners + c2.n_losers}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {p.notes.length > 0 && (
        <div className="disclaimer">
          {p.notes.map((n, i) => <div key={i} style={{ marginBottom: 5 }}>{n}</div>)}
        </div>
      )}
    </div>
  );
}

function Stat({ k, v, color }: { k: string; v: string; color?: string }) {
  return (
    <div className="score-box">
      <div className="k">{k}</div>
      <div className="v" style={color ? { color } : undefined}>{v}</div>
    </div>
  );
}

function BucketTable({ title, buckets }: { title: string; buckets: Bucket[] }) {
  if (!buckets || buckets.length === 0) return null;
  return (
    <div className="panel">
      <h3>{title}</h3>
      <table>
        <thead>
          <tr>
            <th className="left">Bucket</th>
            <th>n</th>
            <th>W / L / T</th>
            <th>Win rate</th>
            <th>Expectancy</th>
            <th>PF</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((b) => (
            <tr key={b.key} style={b.insufficient_sample ? { opacity: 0.5 } : undefined}>
              <td className="left">
                {b.key}
                {b.insufficient_sample && (
                  <span className="muted" style={{ fontSize: 10, marginLeft: 6 }}>small n</span>
                )}
              </td>
              <td>{b.n}</td>
              <td className="muted">{b.wins}/{b.losses}/{b.timeouts}</td>
              <td>{pct(b.win_rate)}</td>
              <td className={expectancyClass(b.expectancy_pct)}>{num(b.expectancy_pct)}%</td>
              <td>{b.profit_factor === null ? DASH : num(b.profit_factor)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
