import type { Verdict } from '../types';

/** The four-level verdict, rendered as a badge or as a full explanation block.
 *
 * The French label is the primary text: the product's owner reads French, and
 * these four words are the one thing a non-specialist is meant to take away.
 * The caveat travels with the expanded form, never with the badge alone —
 * a coloured badge with no caveat is exactly how a ranking gets read as a
 * prediction. */
export function VerdictBadge({ verdict, size = 'md' }: { verdict: Verdict; size?: 'sm' | 'md' }) {
  return (
    <span className={`verdict-badge ${verdict.level.toLowerCase()} ${size}`} title={verdict.headline}>
      <span className="verdict-emoji">{verdict.emoji}</span>
      <span className="verdict-label">{verdict.label_fr}</span>
    </span>
  );
}

export function VerdictPanel({ verdict }: { verdict: Verdict }) {
  return (
    <div className={`verdict-panel ${verdict.level.toLowerCase()}`}>
      <div className="verdict-head">
        <VerdictBadge verdict={verdict} />
        <span className="verdict-headline">{verdict.headline_fr}</span>
      </div>
      <ul className="verdict-reasons">
        {verdict.reasons_fr.map((r, i) => (
          <li key={i}>{r}</li>
        ))}
      </ul>
      <div className="verdict-caveat">{verdict.caveat_fr}</div>
    </div>
  );
}
