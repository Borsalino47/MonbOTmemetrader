import type { RobinhoodStatus } from '../types';

/** The ROBINHOOD universe as it honestly stands today.
 *
 * The chain layer is real and verifiable — that is what the badge and the
 * check list show. The market layer (new tokens, prices, liquidity, buyers)
 * is not implemented yet, and this screen says so in those words.
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

      {!status.market_data_available && (
        <div className="panel rh-pending">
          <h3>Données de marché — NON DISPONIBLE</h3>
          <p className="rh-explain">
            La chaîne est vérifiable dès maintenant. Les données de marché
            arrivent aux phases suivantes, chacune avec sa source réelle. Rien
            n'est affiché tant qu'une source fiable n'est pas branchée : une
            liste vide se lirait comme une chaîne calme, ce qui serait faux.
          </p>
          <ul className="rh-roadmap">
            <li><span className="rh-icon">🆕</span> Nouveaux tokens &lt; 15 min / 1 h / 24 h</li>
            <li><span className="rh-icon">💧</span> Prix, liquidité, pools</li>
            <li><span className="rh-icon">👥</span> Acheteurs / vendeurs, holders</li>
            <li><span className="rh-icon">⚠️</span> Safety + Rug Risk (veto absolu)</li>
            <li><span className="rh-icon">📈</span> Early Score, Explosion 15 min</li>
            <li><span className="rh-icon">🔥</span> Décision ACHETER / SURVEILLER / NE PAS ACHETER</li>
          </ul>
          <p className="muted small">{status.note}</p>
        </div>
      )}
    </>
  );
}
