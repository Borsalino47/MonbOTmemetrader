import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { RobinhoodToken, RobinhoodTokensResponse } from '../types';

/** New tokens on Robinhood Chain, newest first, filterable by age.
 *
 * Every figure here is either what the indexer returned or an em dash. There is
 * no score on this screen and no BUY: discovery answers "what appeared and when",
 * and mixing a verdict in would make it impossible to tell later whether a bad
 * call came from the search or from the scoring. */
export function RobinhoodTokens() {
  const [data, setData] = useState<RobinhoodTokensResponse | null>(null);
  const [bucket, setBucket] = useState<string>('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.robinhoodTokens(bucket || undefined));
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }, [bucket]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      await load();
      if (cancelled) return;
      // Poll only while the first search is still running, then stop: a list
      // that reloads on a timer would fight the user's scrolling.
      setData((d) => {
        if (d?.state === 'PENDING') timer = window.setTimeout(tick, 900);
        return d;
      });
    };
    void tick();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [load]);

  async function search() {
    setBusy(true);
    setError(null);
    try {
      setData(await api.robinhoodSearch());
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  }

  const buckets = data?.buckets ?? [];
  const tokens = data?.tokens ?? [];

  return (
    <div className="panel">
      <div className="rh-tokens-head">
        <h3>🆕 Nouveaux tokens</h3>
        <button className="action small" onClick={search} disabled={busy}>
          {busy ? '…' : 'Rechercher'}
        </button>
      </div>

      <div className="rh-buckets">
        <button className={bucket === '' ? 'active' : ''} onClick={() => setBucket('')}>
          Tous
        </button>
        {buckets.map((b) => (
          <button
            key={b.id}
            className={bucket === b.id ? 'active' : ''}
            onClick={() => setBucket(b.id)}
          >
            {b.label_fr}
            <span className="rh-bucket-n">{b.count}</span>
          </button>
        ))}
      </div>

      {error && <div className="error-box">{error}</div>}

      {data?.state === 'PENDING' && <p className="muted">Première recherche en cours…</p>}

      {data?.state === 'FAILED' && (
        <div className="rh-empty bad">
          <strong>Recherche impossible</strong>
          <span>
            L'indexeur n'a pas répondu{data.errors?.length ? ` (${data.errors[0]})` : ''}.
            Ce n'est pas « aucun token » — c'est une source injoignable.
          </span>
        </div>
      )}

      {data?.state === 'EMPTY' && (
        <div className="rh-empty">
          <strong>Aucun token dans cette fenêtre</strong>
          <span>
            La recherche a abouti et n'a rien trouvé d'assez récent ou d'assez liquide.
            La chaîne est peut-être simplement calme.
          </span>
        </div>
      )}

      {tokens.length > 0 && (
        <div className="rh-token-list">
          {tokens.map((t) => <TokenRow key={t.contract_address} t={t} />)}
        </div>
      )}

      {data?.coverage_note && <p className="feed-note">{data.coverage_note}</p>}
    </div>
  );
}

function TokenRow({ t }: { t: RobinhoodToken }) {
  return (
    <div className="rh-token">
      <div className="rh-token-head">
        <span className="rh-sym">{t.symbol ?? '—'}</span>
        <span className="rh-age">{fmtAge(t.pool_age_seconds)}</span>
      </div>
      <div className="rh-addr" title={t.contract_address}>
        {short(t.contract_address)}
        {t.dex ? <span className="rh-dex">{t.dex}</span> : null}
      </div>
      <div className="rh-token-grid">
        <Cell k="Prix" v={fmtUsd(t.price_usd, 6)} />
        <Cell
          k="Liquidité"
          v={fmtUsd(t.liquidity_usd)}
          // "partielle" qualifies a total assembled from some of the pools. With
          // no value at all there is nothing to qualify, and "— (partielle)"
          // read as though part of a number were showing.
          note={t.liquidity_is_partial && t.liquidity_usd !== null ? 'partielle' : undefined}
        />
        <Cell k="Volume 1 h" v={fmtUsd(t.volume_h1)} />
        <Cell k="Acheteurs 1 h" v={fmtNum(t.buyers_h1)} />
        <Cell k="Achats/ventes" v={fmtNum(t.buy_sell_ratio_h1, 2)} />
        <Cell k="Accélération" v={fmtNum(t.volume_acceleration, 2, '×')} />
      </div>
    </div>
  );
}

function Cell({ k, v, note }: { k: string; v: string; note?: string }) {
  return (
    <div className="rh-cell">
      <span className="k">{k}</span>
      <span className={`v ${v === '—' ? 'unknown' : ''}`}>
        {v}
        {note ? <span className="rh-partial"> ({note})</span> : null}
      </span>
    </div>
  );
}

/* Formatters. Every one of them renders null as an em dash and never as 0 —
   the single rule this whole screen exists to respect. */

function fmtAge(s: number | null): string {
  if (s === null) return '—';
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

function fmtUsd(v: number | null, digits = 0): string {
  if (v === null) return '—';
  if (v !== 0 && Math.abs(v) < 0.01) return `$${v.toPrecision(3)}`;
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(digits)}`;
}

function fmtNum(v: number | null, digits = 0, suffix = ''): string {
  return v === null ? '—' : `${v.toFixed(digits)}${suffix}`;
}

function short(addr: string): string {
  return addr.length > 14 ? `${addr.slice(0, 8)}…${addr.slice(-6)}` : addr;
}
