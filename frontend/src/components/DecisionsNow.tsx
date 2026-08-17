import { num, pct, price as fmtPrice, signClass } from '../format';
import type { DecisionsResponse, Position, TradeDecision } from '../types';
import { DECISION_LABELS, DecisionBadge } from './DecisionBadge';

/** "Décisions maintenant" — the state of everything in under three seconds.
 *
 * Ordered by what needs a human: sells first, then buys, then positions that
 * are fine. That order is deliberate and is not "most interesting first" — an
 * exit signal has a deadline and a buy signal has a wider one, while a healthy
 * position needs nothing at all and is there only so the list feels complete.
 *
 * ⚫ NE PAS ACHETER is not shown here. Seventeen tokens that fail the entry
 * conditions is the normal state of a market, and rendering them would bury the
 * one row that matters under a screenful of correct refusals. The count is
 * shown; the list lives in the scanner. */
export function DecisionsNow({
  data, onSelect, onOpenTab,
}: {
  data: DecisionsResponse | null;
  onSelect: (symbol: string) => void;
  onOpenTab: (tab: string) => void;
}) {
  if (!data) return null;

  const positions = [...data.positions].sort(
    (a, b) => rank(b.current_decision) - rank(a.current_decision),
  );
  const buys = data.entries.filter((e) => e.action === 'BUY');
  const watches = data.entries.filter((e) => e.action === 'WATCH').slice(0, 3);
  const avoided = data.counts.AVOID ?? 0;

  const nothing = positions.length === 0 && buys.length === 0 && watches.length === 0;

  return (
    <section className="home-block">
      <div className="home-head">
        <h2>Décisions maintenant</h2>
        <button className="home-more" onClick={() => onOpenTab('me')}>Mes positions</button>
      </div>

      {data.awaiting_answer.length > 0 && (
        <button className="dec-pending" onClick={() => onOpenTab('me')}>
          {data.awaiting_answer.length === 1
            ? '1 recommandation attend votre réponse'
            : `${data.awaiting_answer.length} recommandations attendent votre réponse`}
        </button>
      )}

      {nothing ? (
        <p className="home-empty">
          Rien à faire pour le moment : aucune position ouverte et aucun setup ne passe
          les conditions d'entrée. C'est une réponse valable — le scanner ne promeut pas
          le moins mauvais d'une mauvaise liste.
        </p>
      ) : (
        <div className="dec-now">
          {positions.map((p) => <PositionRow key={p.id} p={p} onSelect={onSelect} />)}
          {buys.map((d) => <EntryRow key={d.symbol} d={d} onSelect={onSelect} />)}
          {watches.map((d) => <EntryRow key={d.symbol} d={d} onSelect={onSelect} />)}
        </div>
      )}

      {avoided > 0 && (
        <p className="dec-avoided">
          {DECISION_LABELS.AVOID.emoji} {avoided} token{avoided > 1 ? 's' : ''} ne
          passe{avoided > 1 ? 'nt' : ''} pas les conditions d'entrée. C'est l'état normal
          d'un marché ; la liste complète est dans le Scanner.
        </p>
      )}
    </section>
  );
}

function PositionRow({ p, onSelect }: { p: Position; onSelect: (s: string) => void }) {
  const action = p.current_decision ?? 'HOLD';
  return (
    <button className={`dec-row ${action}`} onClick={() => onSelect(p.symbol)}>
      <DecisionBadge action={action} size="sm" />
      <span className="dec-row-body">
        <span className="dec-row-sym">{p.symbol}</span>
        <span className="dec-row-why">
          Position {' '}
          <span className={signClass(p.pnl_pct)}>{pct(p.pnl_pct)}</span>
          {p.health_score !== null && ` · santé ${num(p.health_score, 0)}/100`}
        </span>
      </span>
    </button>
  );
}

function EntryRow({ d, onSelect }: { d: TradeDecision; onSelect: (s: string) => void }) {
  return (
    <button className={`dec-row ${d.action}`} onClick={() => onSelect(d.symbol)}>
      <DecisionBadge action={d.action} size="sm" strength={d.strength_fr} />
      <span className="dec-row-body">
        <span className="dec-row-sym">{d.symbol}</span>
        <span className="dec-row-why">
          {d.action === 'BUY'
            ? (d.reasons[0] ?? fmtPrice(d.price))
            : (d.blocking[0] ?? d.reasons[0] ?? '—')}
        </span>
      </span>
    </button>
  );
}

/** Sells before reduces before watches before holds: the order in which a
 *  human has to deal with them, not the order they were computed in. */
function rank(action: string | null): number {
  return { SELL: 4, REDUCE: 3, WATCH: 2, HOLD: 1 }[action ?? 'HOLD'] ?? 0;
}
