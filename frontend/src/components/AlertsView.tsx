import type { AlertItem } from '../types';
import { clock, num, price } from '../format';

export function AlertsView({ alerts, onSelect }: { alerts: AlertItem[]; onSelect: (s: string) => void }) {
  if (alerts.length === 0) {
    return (
      <div className="table-wrap">
        <div className="empty">
          No alerts yet. An alert requires the score threshold plus a clean liquidity gate,
          a clean safety gate, acceptable pump maturity and sufficient data confidence.
        </div>
      </div>
    );
  }

  return (
    <div>
      {alerts.map((a, i) => (
        <div key={`${a.symbol}-${a.timestamp_ms}-${i}`} className={`alert-card ${a.level}`}
             onClick={() => onSelect(a.symbol)} style={{ cursor: 'pointer' }}>
          <div className="alert-head">
            <span className="sym">{a.symbol}</span>
            <span className={`pill ${a.level}`}>{a.level.replace('_', ' ')}</span>
            <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>{a.headline}</span>
            <span className="time">{clock(a.timestamp_ms)}</span>
          </div>

          <div className="alert-metrics">
            <span>Opportunity {a.opportunity_score}</span>
            <span>Confidence {num(a.data_confidence, 0)}/100</span>
            <span>Maturity {num(a.pump_maturity, 0)}/100</span>
            <span>Safety {num(a.safety, 0)}/100</span>
            <span>Liquidity {a.liquidity}</span>
            <span>Price {price(a.price)}</span>
            {a.score_acceleration !== null && <span>Δscore {a.score_acceleration >= 0 ? '+' : ''}{num(a.score_acceleration, 1)}</span>}
          </div>

          {a.why.length > 0 && (
            <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 5 }}>
              <strong style={{ color: 'var(--text)' }}>Why:</strong> {a.why.slice(0, 4).join(' · ')}
            </div>
          )}
          {a.risks.length > 0 && (
            <div style={{ fontSize: 11, color: 'var(--warn)', marginTop: 3 }}>
              <strong>Risks:</strong> {a.risks.slice(0, 3).join(' · ')}
            </div>
          )}
          {a.trigger && (
            <div style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 3 }}>
              Trigger: {a.trigger}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
