import { useEffect, useState } from 'react';
import { api } from '../api';
import { DASH, num, price } from '../format';
import type { Decision, Validation, ValidationsResponse } from '../types';

const LABELS: Record<Decision, string> = {
  VALIDATED: '✅ Validés',
  WATCHLIST: '⭐ Surveillés',
  ANALYSE: '🔬 À analyser',
  REJECTED: '❌ Rejetés',
};

/** The user's own decisions — the one dataset here that is not the scanner's.
 *
 * Shown as a history rather than as a list of "current picks", because the
 * record is append-only and a sequence of decisions on one token says more than
 * its last state. The filter chips narrow it; nothing collapses it.
 *
 * No success rate is computed. Every one of these decisions was taken in the
 * last few days on generated candles, and a rate over a handful of judgements
 * would be read as a verdict on the user's own skill — the most misleading
 * number this product could possibly show. */
export function ValidationsView({ onSelect }: { onSelect: (symbol: string) => void }) {
  const [data, setData] = useState<ValidationsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Decision | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.validations(300)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(String(e.message ?? e)); });
    return () => { cancelled = true; };
  }, []);

  if (error) return <div className="error-box">Décisions indisponibles : {error}</div>;
  if (!data) return <div className="loading">Chargement…</div>;

  const rows = filter ? data.validations.filter((v) => v.decision === filter) : data.validations;

  return (
    <div className="validations">
      <div className="val-counts">
        {(Object.keys(LABELS) as Decision[]).map((d) => (
          <button
            key={d}
            className={`val-count ${d} ${filter === d ? 'active' : ''}`}
            onClick={() => setFilter(filter === d ? null : d)}
          >
            <span className="val-count-n">{data.counts[d] ?? 0}</span>
            <span className="val-count-l">{LABELS[d]}</span>
          </button>
        ))}
      </div>

      {data.synthetic > 0 && (
        <p className="val-warn">
          {data.synthetic === data.total
            ? `Toutes vos décisions (${data.total}) ont été prises en mode DÉMO, sur des bougies générées.`
            : `${data.synthetic} de vos ${data.total} décisions ont été prises en mode DÉMO, sur des bougies générées.`}
          {' '}Elles sont comptées à part et ne disent rien de votre jugement d'un marché réel.
        </p>
      )}

      {rows.length === 0 ? (
        <div className="empty">
          {filter
            ? `Aucune décision « ${LABELS[filter]} » pour le moment.`
            : "Aucune décision enregistrée. Ouvrez la fiche d'un token et utilisez « Ma décision »."}
        </div>
      ) : (
        <div className="val-list">
          {rows.map((v) => <Row key={v.id} v={v} onSelect={onSelect} />)}
        </div>
      )}

      <p className="val-note">
        Aucun taux de réussite n'est affiché ici. Un pourcentage sur quelques décisions
        se lirait comme un jugement sur votre propre flair, et ce serait le chiffre le
        plus trompeur que cette application puisse montrer. L'onglet Vérification dit ce
        que le prix a fait ; celui-ci dit seulement ce que vous avez décidé, et quand.
      </p>
    </div>
  );
}

function Row({ v, onSelect }: { v: Validation; onSelect: (symbol: string) => void }) {
  return (
    <button className={`val-row ${v.decision}`} onClick={() => onSelect(v.symbol)}>
      <span className={`val-dot ${v.decision}`} />
      <span className="val-body">
        <span className="val-top">
          <span className="val-sym">{v.symbol}</span>
          <span className="val-when">
            {v.decided_at ? new Date(v.decided_at).toLocaleString('fr-FR', {
              day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
            }) : DASH}
          </span>
        </span>
        <span className="val-metrics">
          <span>{price(v.price)}</span>
          <span>Opp {v.final_score === null ? DASH : num(v.final_score, 0)}</span>
          <span>Exp {v.explosion_score === null ? DASH : num(v.explosion_score, 0)}</span>
          {v.synthetic && <span className="val-demo">DÉMO</span>}
        </span>
        {v.note && <span className="val-usernote">« {v.note} »</span>}
        {/* The sentence the user read at the time, not what the engine would
            say now. That distinction is the whole reason it is stored. */}
        {v.why[0] && <span className="val-why">{v.why[0]}</span>}
      </span>
    </button>
  );
}
