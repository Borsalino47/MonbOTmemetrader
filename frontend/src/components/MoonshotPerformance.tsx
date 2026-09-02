import { useEffect, useState } from 'react';
import { api } from '../api';
import { DASH, num } from '../format';
import type { MoonshotPerformanceResponse } from '../types';

function pct(v: number | null | undefined): string {
  return v === null || v === undefined || !Number.isFinite(v) ? DASH : `${(v * 100).toFixed(1)}%`;
}

/**
 * Realised performance of the ×10 axis.
 *
 * Led by the distribution of multiples actually reached, not by a win rate. A
 * layer whose ×2 rate looks respectable while nothing ever passed ×3 has not
 * found what it claims to look for, and only the distribution shows that.
 */
export function MoonshotPerformance() {
  const [data, setData] = useState<MoonshotPerformanceResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);

  async function load() {
    try {
      setData(await api.moonshotPerformance());
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
      await api.resolveMoonshotOutcomes();
      await load();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setResolving(false);
    }
  }

  if (error) return <div className="error-box">Could not load ×10 performance: {error}</div>;
  if (!data) return <div className="loading">Loading…</div>;

  const c = data.counts;
  const p = data.performance;
  const noneSettled = c.settled === 0;

  return (
    <div>
      <div className="toolbar">
        <div className="filter">
          <label>Label</label>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>{data.label.config}</span>
        </div>
        <div className="filter">
          <label>Graded on</label>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>{data.label.timeframe} bars</span>
        </div>
        <button className="action" onClick={resolveNow} disabled={resolving} style={{ marginLeft: 'auto' }}>
          {resolving ? 'Grading…' : 'Grade now'}
        </button>
      </div>

      <div className="top-grid" style={{ marginBottom: 16 }}>
        <Stat label="Readings journalled" value={String(c.readings_journalled)} />
        <Stat label="Awaiting their horizon" value={String(c.pending_evaluation)} />
        <Stat label="Settled" value={String(c.settled)} />
        <Stat
          label="Ever reached ×10"
          value={String(c.reached_10x)}
          hint="The number this whole layer exists for. Reported even when it is zero."
        />
      </div>

      {noneSettled ? (
        <div className="table-wrap">
          <div className="empty">
            Nothing has settled yet — and on a {data.label.timeframe} horizon that is the expected
            state for a while. {c.pending_evaluation} reading(s) are waiting for their horizon to
            elapse. A win rate here would be invented, so none is shown.
          </div>
        </div>
      ) : (
        <>
          <div className="section-title">How far did they actually go?</div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="left">Reached at least</th>
                  <th>n</th>
                  <th>Share of settled readings</th>
                  <th className="left">&nbsp;</th>
                </tr>
              </thead>
              <tbody>
                {p.multiple_distribution.map((rung) => (
                  <tr key={rung.at_least}>
                    <td className="left sym">×{rung.at_least}</td>
                    <td>{rung.n}</td>
                    <td>{pct(rung.share)}</td>
                    <td className="left">
                      <span className="scorebar" style={{ minWidth: 160 }}>
                        <span
                          className="fill"
                          style={{ width: `${(rung.share ?? 0) * 100}%`, background: 'var(--up)' }}
                        />
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="top-grid" style={{ margin: '14px 0' }}>
            <Stat label="Best multiple reached" value={`×${num(p.best_multiple)}`} />
            <Stat label="Median multiple" value={`×${num(p.median_multiple)}`} />
            <Stat label="Win rate at the label target" value={pct(p.overall.win_rate)} />
            <Stat label="Expectancy" value={`${num(p.overall.expectancy_pct)}%`} />
          </div>

          <BucketTable title="By stage at signal time" rows={p.by_stage} minSample={p.min_sample} />
          <BucketTable title="By moonshot score band" rows={p.by_score_band} minSample={p.min_sample} />
          <BucketTable
            title="Did knowing the market cap help?"
            rows={p.by_capacity_known}
            minSample={p.min_sample}
          />
        </>
      )}

      {p.notes.map((note, i) => (
        <div key={i} className="disclaimer">{note}</div>
      ))}

      <div className="disclaimer">
        <strong>{data.label.definition}</strong>
      </div>
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="opp-card" style={{ cursor: 'default' }} title={hint}>
      <div className="opp-metrics" style={{ gridTemplateColumns: '1fr', margin: 0 }}>
        <div className="m">
          <span className="k">{label}</span>
          <span className="v" style={{ fontSize: 20 }}>{value}</span>
        </div>
      </div>
    </div>
  );
}

function BucketTable({
  title, rows, minSample,
}: { title: string; rows: MoonshotPerformanceResponse['performance']['by_stage']; minSample: number }) {
  if (!rows.length) return null;
  return (
    <>
      <div className="section-title">{title}</div>
      <div className="table-wrap" style={{ marginBottom: 14 }}>
        <table>
          <thead>
            <tr>
              <th className="left">Bucket</th>
              <th>n</th>
              <th>Win rate</th>
              <th>Expectancy</th>
              <th className="left">&nbsp;</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((b) => (
              <tr key={b.key}>
                <td className="left sym">{b.key}</td>
                <td>{b.n}</td>
                <td>{pct(b.win_rate)}</td>
                <td className={(b.expectancy_pct ?? 0) > 0 ? 'pos' : 'neg'}>{num(b.expectancy_pct)}%</td>
                <td className="left" style={{ fontSize: 11, color: 'var(--text-faint)' }}>
                  {b.insufficient_sample ? `fewer than ${minSample} — not a finding` : ''}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
