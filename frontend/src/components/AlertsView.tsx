import type { AlertItem } from '../types';
import { clock, num, price } from '../format';
import { ALERT_LEVEL_FR, LIQUIDITY_FR, labelFor } from '../i18n/fr';

export function AlertsView({ alerts, onSelect }: { alerts: AlertItem[]; onSelect: (s: string) => void }) {
  if (alerts.length === 0) {
    return (
      <div className="table-wrap">
        <div className="empty">
          Aucune alerte pour l’instant. Une alerte exige le seuil de score, une liquidité
          saine, une sécurité saine, une maturité du mouvement acceptable et une confiance
          dans les données suffisante.
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
            <span className={`pill ${a.level}`}>{labelFor(ALERT_LEVEL_FR, a.level)}</span>
            <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>{a.headline}</span>
            <span className="time">{clock(a.timestamp_ms)}</span>
          </div>

          <div className="alert-metrics">
            <span>Opportunité {a.opportunity_score}</span>
            <span>Confiance {num(a.data_confidence, 0)}/100</span>
            <span>Maturité {num(a.pump_maturity, 0)}/100</span>
            <span>Sécurité {num(a.safety, 0)}/100</span>
            <span>Liquidité {labelFor(LIQUIDITY_FR, a.liquidity)}</span>
            <span>Prix {price(a.price)}</span>
            {a.score_acceleration !== null && <span>Δscore {a.score_acceleration >= 0 ? '+' : ''}{num(a.score_acceleration, 1)}</span>}
          </div>

          {/* Reasons are listed, never run together with separators: on a phone a
              middot-joined run reads as one sentence and nothing stands out
              (spec §22). */}
          {a.why.length > 0 && (
            <div className="alert-reasons">
              <strong>POURQUOI</strong>
              <ul className="why-list">
                {a.why.slice(0, 4).map((r, j) => <li key={j}>{r}</li>)}
              </ul>
            </div>
          )}
          {a.risks.length > 0 && (
            <div className="alert-reasons risk">
              <strong>RISQUES</strong>
              <ul className="risk-list">
                {a.risks.slice(0, 3).map((r, j) => <li key={j}>{r}</li>)}
              </ul>
            </div>
          )}
          {a.trigger && (
            <div style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 3 }}>
              Déclencheur : {a.trigger}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
