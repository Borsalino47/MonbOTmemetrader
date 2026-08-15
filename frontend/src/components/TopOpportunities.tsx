import type { ScoreRow } from '../types';
import { DASH, maturityColor, num, scoreColor } from '../format';

interface Props {
  rows: ScoreRow[];
  onSelect: (symbol: string) => void;
}

/** Best *setups*, ranked by quality — not the biggest movers. */
export function TopOpportunities({ rows, onSelect }: Props) {
  if (rows.length === 0) {
    return (
      <>
        <div className="section-title">Top opportunities now</div>
        <div className="table-wrap">
          <div className="empty">
            No setup currently clears the gates. That is a valid answer — the scanner does not
            promote a best-of-a-bad-list.
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      <div className="section-title">Top opportunities now — ranked by setup quality, not by price change</div>
      <div className="top-grid">
        {rows.map((r) => {
          const rvol = r.metrics?.rvol ?? null;
          return (
            <div key={r.symbol} className={`opp-card ${r.setup.state}`} onClick={() => onSelect(r.symbol)}>
              <div className="opp-head">
                <span className="opp-sym">{r.symbol}</span>
                <span className="opp-score" style={{ color: scoreColor(r.final_score) }}>
                  {num(r.final_score, 0)}<span style={{ fontSize: 11, color: 'var(--text-faint)' }}>/100</span>
                </span>
              </div>

              <div>
                <span className={`pill ${r.setup.state}`}>{r.setup.state}</span>{' '}
                <span className={`pill ${r.liquidity.status}`}>{r.liquidity.status}</span>
                {r.score_acceleration !== null && r.score_acceleration > 0 && (
                  <span className="pill BREAKOUT" style={{ marginLeft: 4 }}>
                    +{r.score_acceleration.toFixed(0)} since last
                  </span>
                )}
              </div>

              <div className="opp-metrics">
                <div className="m">
                  <span className="k">Maturity</span>
                  <span className="v" style={{ color: maturityColor(r.pump_maturity.score) }}>
                    {num(r.pump_maturity.score, 0)}
                  </span>
                </div>
                <div className="m">
                  <span className="k">Accel</span>
                  <span className="v">{num(r.acceleration.momentum_acceleration, 0)}</span>
                </div>
                <div className="m">
                  <span className="k">RVOL</span>
                  <span className="v">{rvol === null ? DASH : `${num(rvol)}x`}</span>
                </div>
              </div>

              <ul className="opp-why">
                {r.why.slice(0, 3).map((w, i) => <li key={i}>{w}</li>)}
              </ul>
            </div>
          );
        })}
      </div>
    </>
  );
}
