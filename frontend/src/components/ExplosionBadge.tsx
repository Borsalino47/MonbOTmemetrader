import { num } from '../format';
import type { Explosion } from '../types';

/** The explosion score, shown with the window it is talking about.
 *
 * The window is never dropped, not even in the compact form. "72/100" alone is
 * meaningless here — the whole content of this number is the claim that
 * something happens within a stated number of minutes, and a score without its
 * horizon is a score about nothing.
 *
 * Rendered as `72/100`, never `72%`. Nothing in this project is calibrated. */
export function ExplosionBadge({ explosion, size = 'md' }: { explosion: Explosion; size?: 'sm' | 'md' }) {
  const tone = toneOf(explosion);
  return (
    <span className={`exp-badge ${tone} ${size}`}>
      <span className="exp-label">{explosion.explosion_label}</span>
      <span className="exp-score">
        {num(explosion.explosion_score, 0)}<small>/100</small>
      </span>
      <span className="exp-window">{explosion.horizon_minutes} min</span>
    </span>
  );
}

/** The full panel: the number, what argues for it, and what argues against.
 *
 * The disclaimer is rendered unconditionally, including on the loud readings.
 * A high score with no caveat next to it is precisely how a ranking gets read
 * as a prediction, and this is the score most likely to be acted on quickly. */
export function ExplosionPanel({ explosion }: { explosion: Explosion }) {
  return (
    <div className="panel">
      <h3>Explosion {explosion.horizon_minutes} min</h3>
      <p className="exp-question">
        Ce token est-il sur le point de bouger dans les {explosion.horizon_minutes} prochaines
        minutes ? Question différente du score d'opportunité, jamais mélangée avec lui.
      </p>

      <div className="exp-head">
        <ExplosionBadge explosion={explosion} />
      </div>

      {explosion.vetoed ? (
        <div className="exp-veto">
          Score mis à zéro : {explosion.veto_reason}. Sur quinze minutes, une bougie de
          +10% sur un carnet vide est un piège, pas une occasion — un score simplement
          réduit laisserait ce token passer devant un token négociable.
        </div>
      ) : (
        <div className="exp-breakdown">
          {explosion.components.map((c) => (
            <div key={c.name} className={`breakdown-row ${c.points === 0 ? 'zero' : ''} ${c.available ? '' : 'unavailable'}`}>
              <div className="name">{FR[c.name] ?? c.name}</div>
              <div className="bar"><div className="fill" style={{ width: `${(c.points / c.max_points) * 100}%` }} /></div>
              <div className="pts">+{c.points.toFixed(1)} / {c.max_points}</div>
            </div>
          ))}
        </div>
      )}

      {explosion.why.length > 0 && (
        <ul className="exp-list">
          {explosion.why.slice(0, 5).map((w, i) => <li key={i}>{w}</li>)}
        </ul>
      )}
      {explosion.risks.length > 0 && (
        <ul className="exp-list">
          {explosion.risks.slice(0, 5).map((w, i) => <li key={i} className="risk">{w}</li>)}
        </ul>
      )}

      <p className="exp-caveat">
        Classement sur 100, pas une probabilité. Ces pondérations n'ont jamais été
        validées — mais c'est le seul score du projet dont la fenêtre est déjà mesurée :
        l'onglet Vérification enregistre ce que le prix a réellement fait
        {' '}{explosion.horizon_minutes} minutes après chaque signal.
      </p>
    </div>
  );
}

function toneOf(e: Explosion): string {
  if (e.vetoed) return 'blocked';
  if (e.explosion_score >= 70) return 'hot';
  if (e.explosion_score >= 45) return 'warm';
  return 'cold';
}

/** Component names, translated at the point of display only. The API keeps its
 *  English keys so a stored row is never re-read through a translation. */
const FR: Record<string, string> = {
  volume_burst: 'Volume qui arrive',
  thrust: 'Poussée du prix',
  proximity: 'Distance au niveau',
  compression: 'Range comprimé',
  book_pressure: 'Pression du carnet',
  earliness: 'Précocité',
};
