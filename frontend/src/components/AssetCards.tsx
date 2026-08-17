import { DASH, maturityColor, mult, num, pct, price, scoreColor, signClass } from '../format';
import { LIQUIDITY_FR, SETUP_STATE_FR, labelFor } from '../i18n/fr';
import type { ScoreRow } from '../types';
import { ExplosionBadge } from './ExplosionBadge';
import { VerdictBadge } from './VerdictBadge';

interface Props {
  rows: ScoreRow[];
  onSelect: (symbol: string) => void;
  expert: boolean;
}

/** The scanner as a list of cards rather than a thirteen-column table.
 *
 * A table is the right shape on a desktop and the wrong one on a phone, where
 * it becomes horizontal scrolling — the reader has to remember column headers
 * that are off-screen. A card puts the four things that decide whether to look
 * closer (symbol, score, verdict, why) on one screen with no scrolling at all.
 *
 * Expert mode adds the measured columns underneath. It does not add a different
 * set of facts: the same row, more of it. */
export function AssetCards({ rows, onSelect, expert }: Props) {
  if (rows.length === 0) {
    return <div className="empty">Aucun actif ne correspond aux filtres actuels.</div>;
  }

  return (
    <div className="cards">
      {rows.map((r, i) => {
        const vetoed = r.safety.hard_veto || r.liquidity.veto;
        return (
          <button
            key={r.symbol}
            className={`card ${vetoed ? 'vetoed' : ''}`}
            onClick={() => onSelect(r.symbol)}
          >
            <div className="card-head">
              <span className="card-rank">{i + 1}</span>
              <span className="card-sym">{r.symbol}</span>
              {vetoed && <span className="veto-flag">VETO</span>}
              <span className="card-price">{price(r.price)}</span>
            </div>

            <div className="card-verdict">
              {r.verdict && <VerdictBadge verdict={r.verdict} size="sm" />}
              <span className="card-score" style={{ color: scoreColor(r.final_score) }}>
                {num(r.final_score, 0)}<small>/100</small>
                {r.score_acceleration !== null && Math.abs(r.score_acceleration) >= 1 && (
                  <span className={signClass(r.score_acceleration)} style={{ fontSize: 10, marginLeft: 5 }}>
                    {r.score_acceleration > 0 ? '▲' : '▼'}{num(Math.abs(r.score_acceleration), 0)}
                  </span>
                )}
              </span>
            </div>

            {/* Shown only when it says something. A CALME badge on every row
                would train the eye to skip the badge entirely, which is exactly
                when the loud one needs to be noticed. */}
            {r.explosion && (r.explosion.explosion_score >= 45 || r.explosion.vetoed) && (
              <div className="card-exp"><ExplosionBadge explosion={r.explosion} size="sm" /></div>
            )}

            <div className="card-why">{r.why[0] ?? r.setup.rationale ?? DASH}</div>

            <div className="card-strip">
              <Cell k="5m" v={pct(r.metrics?.change_5m_pct)} cls={signClass(r.metrics?.change_5m_pct)} />
              <Cell k="1h" v={pct(r.metrics?.change_1h_pct)} cls={signClass(r.metrics?.change_1h_pct)} />
              <Cell k="RVOL" v={r.metrics?.rvol ? mult(r.metrics.rvol, 1) : DASH} />
              <Cell
                k="Maturité"
                v={num(r.pump_maturity.score, 0)}
                style={{ color: maturityColor(r.pump_maturity.score) }}
              />
            </div>

            {expert && (
              <div className="card-strip expert">
                <Cell k="Statut" v={labelFor(SETUP_STATE_FR, r.setup.state)} />
                <Cell k="Liquidité" v={labelFor(LIQUIDITY_FR, r.liquidity.status)} />
                <Cell k="Sécurité" v={num(r.safety.score, 0)} />
                <Cell
                  k="Confiance"
                  v={num(r.data_confidence.score, 0)}
                  cls={r.data_confidence.score < 60 ? 'neg' : ''}
                />
                <Cell k="ATR" v={r.metrics?.atr_pct != null ? `${num(r.metrics.atr_pct, 2)}%` : DASH} />
                <Cell k="RSI" v={num(r.metrics?.rsi14, 0)} />
                <Cell
                  k="Dist. rés."
                  v={r.metrics?.distance_to_breakout_atr != null
                    ? `${num(r.metrics.distance_to_breakout_atr, 2)} ATR`
                    : DASH}
                />
                <Cell k="Pénalité" v={r.risk_penalty > 0 ? `−${num(r.risk_penalty, 0)}` : '0'} />
              </div>
            )}
          </button>
        );
      })}
    </div>
  );
}

function Cell({
  k, v, cls = '', style,
}: { k: string; v: string; cls?: string; style?: React.CSSProperties }) {
  return (
    <span className="card-cell">
      <span className="card-cell-k">{k}</span>
      <span className={`card-cell-v ${cls}`} style={style}>{v}</span>
    </span>
  );
}
