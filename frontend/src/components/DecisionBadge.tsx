import type { TradeAction } from '../types';

/** The six decisions, rendered the same way everywhere.
 *
 * Icon + text + colour, always all three. Colour alone fails for anyone who
 * cannot distinguish these hues, and it fails for everyone on a phone in
 * sunlight — which is exactly when a 🔴 VENDRE has to be readable.
 *
 * The labels live here rather than coming from the API so that a decision
 * arriving from any endpoint renders identically. The API sends `label_fr` too;
 * this is the fallback and the definition of the palette. */
export const DECISION_LABELS: Record<TradeAction, { emoji: string; fr: string; hint: string }> = {
  BUY:    { emoji: '🟢', fr: 'ACHETER', hint: 'nouveau signal d\'entrée' },
  HOLD:   { emoji: '🔵', fr: 'CONSERVER', hint: 'position saine, rien à faire' },
  WATCH:  { emoji: '🟡', fr: 'SURVEILLER', hint: 'quelque chose demande attention' },
  REDUCE: { emoji: '🟠', fr: 'RÉDUIRE / PROTÉGER', hint: 'les gains méritent d\'être protégés' },
  SELL:   { emoji: '🔴', fr: 'VENDRE', hint: 'signal de sortie' },
  AVOID:  { emoji: '⚫', fr: 'NE PAS ACHETER', hint: 'conditions d\'entrée non réunies' },
};

export function DecisionBadge({
  action, size = 'md', strength,
}: { action: TradeAction; size?: 'sm' | 'md' | 'lg'; strength?: string | null }) {
  const d = DECISION_LABELS[action];
  return (
    <span className={`dec-badge ${action} ${size}`}>
      <span className="dec-emoji" aria-hidden="true">{d.emoji}</span>
      <span className="dec-text">{d.fr}</span>
      {strength && <span className="dec-strength">{strength}</span>}
    </span>
  );
}

/** The full card for a decision that asks the user to do something.
 *
 * Only BUY and SELL get this treatment. A card this loud for CONSERVER would
 * train the eye to skip cards, which is exactly what must not happen to the red
 * one. */
export function DecisionCard({
  action, symbol, price, strength, reasons, risks, trigger, invalidation, children, extra,
}: {
  action: TradeAction;
  symbol: string;
  price: string;
  strength?: string | null;
  reasons: string[];
  risks: string[];
  trigger?: string | null;
  invalidation?: string | null;
  children?: React.ReactNode;
  extra?: React.ReactNode;
}) {
  return (
    <div className={`dec-card ${action}`}>
      <div className="dec-card-head">
        <DecisionBadge action={action} size="lg" />
        <span className="dec-card-sym">{symbol}</span>
        <span className="dec-card-price">{price}</span>
      </div>

      {strength && (
        <div className="dec-card-strength">
          Force du signal : <strong>{strength}</strong>
        </div>
      )}

      {extra}

      {reasons.length > 0 && (
        <>
          <div className="dec-card-label">
            Pourquoi {DECISION_LABELS[action].fr.toLowerCase()} ?
          </div>
          <ul className="dec-card-list">
            {reasons.slice(0, 5).map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </>
      )}

      {(trigger || invalidation) && (
        <div className="dec-levels">
          <div className="dec-level">
            <span className="k">Déclencheur</span>
            <span className="v">{trigger ?? '—'}</span>
          </div>
          <div className="dec-level">
            <span className="k">Invalidation</span>
            <span className="v">{invalidation ?? '—'}</span>
          </div>
        </div>
      )}

      {risks.length > 0 && (
        <>
          <div className="dec-card-label">Risques</div>
          <ul className="dec-card-list risk">
            {risks.slice(0, 4).map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </>
      )}

      {children}

      <p className="dec-card-caveat">
        Aucun ordre n'est passé automatiquement. L'application recommande ;
        c'est vous qui exécutez, puis qui confirmez ici.
      </p>
    </div>
  );
}
