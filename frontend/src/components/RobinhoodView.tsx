import { RobinhoodTokens } from './RobinhoodTokens';
import type { RobinhoodStatus } from '../types';

/** The ROBINHOOD universe as it honestly stands today.
 *
 * Two independent sources, shown as two independent states: the RPC proves the
 * chain (badge + check list), and a DEX indexer supplies the tokens. Either can
 * be healthy while the other is down, so neither vouches for the other.
 *
 * What still does not exist — holders, safety, the scores, the decision — is
 * listed as a roadmap rather than drawn as empty data.
 *
 * The alternative — rendering the planned sections with empty lists — would be
 * indistinguishable from a chain where nothing is happening, which is the one
 * reading this project must never allow (spec §56, and invariant "never
 * fabricate a value"). So the sections are listed as a roadmap, visibly
 * greyed, rather than drawn as empty data. */
export function RobinhoodView({
  status, onRetry, busy,
}: {
  status: RobinhoodStatus | null;
  onRetry: () => void;
  busy: boolean;
}) {
  if (!status) {
    return (
      <div className="panel">
        <h3>ROBINHOOD CHAIN</h3>
        <p className="muted">Chargement de l'état de la chaîne…</p>
      </div>
    );
  }

  const v = status.verification;

  return (
    <>
      <div className="panel rh-head">
        <div className="rh-title">
          <h3>ROBINHOOD CHAIN</h3>
          <span className="rh-chain">chain id {status.chain_id}</span>
        </div>

        <div className={`feed-badge ${v.state}`}>
          <span className="feed-emoji" aria-hidden="true">{v.emoji}</span>
          <span className="feed-body">
            <span className="feed-label">{v.label_fr}</span>
            {v.state !== 'PENDING' && (
              <span className="feed-detail">
                {v.passed}/{v.total} contrôles · {v.endpoint_host}
                {v.latest_block ? ` · bloc ${v.latest_block.toLocaleString('fr-FR')}` : ''}
                {v.error ? ` · ${v.error}` : ''}
              </span>
            )}
          </span>
          {(v.state === 'FAILED' || v.state === 'PARTIAL') && (
            <button className="feed-retry" onClick={onRetry} disabled={busy}>
              {busy ? '…' : 'Réessayer'}
            </button>
          )}
        </div>

        {v.checks.length > 0 && (
          <div className="rh-checks">
            {v.checks.map((c, i) => (
              <div className="kv" key={i}>
                <span className="k">
                  {c.ok ? '✓' : '✗'} {c.name}
                  {c.core && !c.ok ? ' (bloquant)' : ''}
                </span>
                <span className="v">{c.detail || (c.ok ? 'ok' : '—')}</span>
              </div>
            ))}
          </div>
        )}
        <p className="feed-note">{v.note}</p>
      </div>

      <RobinhoodTokens />

      <div className="panel rh-pending">
        <h3>Pas encore disponible</h3>
        <p className="rh-explain">
          Ces sections arrivent aux phases suivantes, chacune avec sa source
          réelle. Rien n'est affiché tant qu'une source fiable n'est pas
          branchée : un chiffre inventé serait pire qu'une case vide.
        </p>
        <ul className="rh-roadmap">
          <li><span className="rh-icon">👥</span> Détenteurs, top 10, déployeur</li>
          <li><span className="rh-icon">⚠️</span> Sécurité + risque de rug (veto absolu)</li>
          <li><span className="rh-icon">📈</span> Score de précocité, explosion 15 min</li>
          <li><span className="rh-icon">🔥</span> Décision ACHETER / SURVEILLER / NE PAS ACHETER</li>
        </ul>
        <p className="muted small">{status.note}</p>
      </div>
    </>
  );
}
