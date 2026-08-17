import { useEffect, useState } from 'react';
import { api } from '../api';
import type { TokenDetail } from '../types';

/** One token, as each source sees it — side by side, never merged.
 *
 * The table has two value columns on purpose. A single "price" column would
 * force a choice between the two sources, and that choice would be invisible
 * to the reader; two columns make a disagreement something you notice rather
 * than something the software silently resolved. */
export function TokenCrossCheck({ address, onClose }: { address: string; onClose: () => void }) {
  const [detail, setDetail] = useState<TokenDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const d = await api.robinhoodToken(address);
        if (!cancelled) { setDetail(d); setError(null); }
      } catch (e) {
        if (!cancelled) setError(String((e as Error).message ?? e));
      }
    })();
    return () => { cancelled = true; };
  }, [address]);

  const cc = detail?.crosscheck;

  return (
    <div className="rh-cross">
      <div className="rh-cross-head">
        <span className="rh-cross-title">Recoupement des sources</span>
        <button className="rh-cross-close" onClick={onClose} aria-label="Fermer">✕</button>
      </div>

      {error && <div className="error-box">{error}</div>}
      {!detail && !error && <p className="muted small">Interrogation de la deuxième source…</p>}

      {cc && (
        <>
          <div className={`rh-agree ${cc.agreement}`}>
            <span aria-hidden="true">{cc.emoji}</span> {cc.label_fr}
          </div>

          <table className="rh-cross-table">
            <thead>
              <tr>
                <th> </th>
                <th>GeckoTerminal</th>
                <th>DexScreener</th>
                <th>Écart</th>
              </tr>
            </thead>
            <tbody>
              {cc.comparisons.map((c) => (
                <tr key={c.field} className={c.agreement}>
                  <td className="k">{c.label_fr}</td>
                  <td>{fmt(c.geckoterminal)}</td>
                  <td>{fmt(c.dexscreener)}</td>
                  <td className="drift">
                    {c.drift_pct === null ? '—' : `${c.drift_pct.toFixed(1)} %`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {cc.caveats.length > 0 && (
            <ul className="rh-caveats">
              {cc.caveats.map((c, i) => <li key={i}>⚠️ {c}</li>)}
            </ul>
          )}

          <p className="feed-note">
            {detail?.pools.geckoterminal.length ?? 0} pool(s) chez GeckoTerminal,
            {' '}{detail?.pools.dexscreener.length ?? 0} chez DexScreener.
            {' '}Les deux valeurs sont conservées : aucune moyenne n'est calculée,
            {' '}car une moyenne de deux prix qui divergent ne décrit ni l'un ni l'autre.
            {detail?.errors.length ? ` ${detail.errors.join(', ')}` : ''}
          </p>
        </>
      )}
    </div>
  );
}

function fmt(v: number | null): string {
  if (v === null) return '—';
  if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`;
  if (Math.abs(v) >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  // Significant digits rather than fixed decimals below 1. Found by running
  // it: a 32 % price disagreement rendered as "0.03 | 0.05", which reads as a
  // rounding artefact — the table would have been hiding the exact thing it
  // exists to show.
  if (v !== 0 && Math.abs(v) < 1) return v.toPrecision(3);
  return v.toFixed(2);
}
