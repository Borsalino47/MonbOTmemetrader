import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { DASH, compact, num, pct, price, signClass } from '../format';
import type { HuntResponse } from '../types';

/** The wide search: which of the whole venue deserves a closer look.
 *
 * Presented as candidates, never as scores. The classic scanner ranks the top
 * pairs by volume, which excludes by construction anything nobody is watching
 * yet; this ranks by anomaly instead. A high priority here means "look closer",
 * and the page says so where it cannot be missed. */
export function HunterView({ onSelect }: { onSelect: (symbol: string) => void }) {
  const [data, setData] = useState<HuntResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setData(await api.hunt(40));
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (error === 'NO_SCAN_YET') {
    return (
      <div className="empty">
        La recherche lit l'instantané que produit un scan. Lancez un scan d'abord —
        elle ne coûte alors aucune requête supplémentaire.
      </div>
    );
  }
  if (error) return <div className="error-box">Recherche impossible : {error}</div>;
  if (!data) return <div className="loading">Chargement…</div>;

  const p = data.prescan;

  return (
    <div>
      <div className="scanner-head">
        <span className="section-title">
          Recherche — {p.returned} candidats sur {p.eligible} éligibles
        </span>
        <button className="filters-toggle" onClick={load} disabled={busy}>
          {busy ? 'Recherche…' : 'Relancer'}
        </button>
      </div>

      <div className="hunt-summary">
        <span><strong>{p.universe_size}</strong> paires lues</span>
        <span><strong>{p.eligible}</strong> négociables</span>
        <span><strong>{p.requests_used}</strong> requête réseau</span>
      </div>

      {!p.has_previous_reading && (
        <div className="hunt-warn">
          Première lecture de cette session : aucun instantané précédent, donc aucun signal
          d'accélération disponible. Le classement n'utilise que des faits statiques.
          Le prochain cycle produira les écarts.
        </div>
      )}

      <div className="cards">
        {p.candidates.map((c, i) => (
          <button key={c.symbol} className="card" onClick={() => onSelect(c.symbol)}>
            <div className="card-head">
              <span className="card-rank">{i + 1}</span>
              <span className="card-sym">{c.symbol}</span>
              <span className="card-price">{price(c.price)}</span>
            </div>

            <div className="card-verdict">
              <span className="hunt-priority">
                priorité <strong>{num(c.priority, 0)}</strong>
              </span>
              <span className={signClass(c.change_pct_24h)}>24h {pct(c.change_pct_24h)}</span>
            </div>

            {c.reasons.slice(0, 2).map((r, k) => (
              <div key={k} className="card-why">+ {r}</div>
            ))}
            {c.caveats.slice(0, 1).map((r, k) => (
              <div key={k} className="card-why risk">! {r}</div>
            ))}

            <div className="card-strip">
              <Cell k="Volume 24h" v={compact(c.quote_volume_24h)} />
              <Cell
                k="Écart / hier"
                v={c.volume_excess_vs_yesterday === null
                  ? DASH
                  : pct(c.volume_excess_vs_yesterday)}
                cls={signClass(c.volume_excess_vs_yesterday)}
              />
              <Cell
                k="Position 24h"
                v={c.range_position_24h === null ? DASH : `${(c.range_position_24h * 100).toFixed(0)}%`}
              />
              <Cell
                k="Spread"
                v={c.spread_bps === null ? DASH : `${num(c.spread_bps, 0)} bps`}
              />
            </div>
          </button>
        ))}
      </div>

      <div className="disclaimer">
        <strong>La priorité n'est pas un score.</strong> Elle répond à « ce token
        mérite-t-il une analyse coûteuse ? », et à rien d'autre. Le jugement appartient
        à l'analyse approfondie, qui voit l'historique des prix que cette étape ne
        télécharge délibérément pas. Le score de découverte versionné arrive en phase 05.
        <div style={{ marginTop: 6 }}>
          « Écart / hier » compare l'activité de maintenant au même moment hier — c'est
          ce que mesure la différence entre deux compteurs glissants sur 24 h. Ce n'est
          pas le volume de la dernière minute.
        </div>
      </div>
    </div>
  );
}

function Cell({ k, v, cls = '' }: { k: string; v: string; cls?: string }) {
  return (
    <span className="card-cell">
      <span className="card-cell-k">{k}</span>
      <span className={`card-cell-v ${cls}`}>{v}</span>
    </span>
  );
}
