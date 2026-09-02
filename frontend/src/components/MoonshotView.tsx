import { useEffect, useState } from 'react';
import { api } from '../api';
import { DASH, compact, maturityColor, num, price, scoreColor } from '../format';
import type { MoonshotResponse, MoonshotStage } from '../types';

interface Props {
  onSelect: (symbol: string) => void;
}

/**
 * The ×10 radar.
 *
 * Two design rules, both about not misleading the reader:
 *
 * 1. Unknown is rendered as unknown. `capacity` is null whenever no valuation
 *    source is configured, and it shows as "—" with the reason underneath. A
 *    zero bar would read as "no room to run", which is the opposite of what a
 *    missing market cap means.
 * 2. The stage is as prominent as the score. A 78 in EXHAUSTION and a 78 in
 *    ACCUMULATION are opposite situations, and the number alone cannot say so.
 */

const STAGE_HELP: Record<MoonshotStage, string> = {
  UNKNOWN: 'Not enough history on the higher timeframe to judge.',
  NEUTRAL: 'Nothing here resembles a pre-expansion state.',
  DORMANT: 'Based and coiled, but no volume arriving yet.',
  ACCUMULATION: 'Volume building into flat price — the quiet part.',
  IGNITION: 'The regime is shifting now and the move is still young.',
  EXPANSION: 'The markup is already under way. Not an early entry.',
  EXHAUSTION: 'Extended and climactic. Whatever this was, you are late to it.',
};

export function MoonshotView({ onSelect }: Props) {
  const [data, setData] = useState<MoonshotResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [candidatesOnly, setCandidatesOnly] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const body = await api.moonshot(candidatesOnly ? { candidates_only: true } : undefined);
        if (alive) { setData(body); setError(null); }
      } catch (e) {
        if (alive) setError(String((e as Error).message ?? e));
      }
    };
    load();
    const t = setInterval(load, 20_000);
    return () => { alive = false; clearInterval(t); };
  }, [candidatesOnly]);

  if (error === 'NO_SCAN_YET') {
    return <div className="table-wrap"><div className="empty">No scan has completed yet.</div></div>;
  }
  if (error) return <div className="error-box">Error: {error}</div>;
  if (!data) return <div className="table-wrap"><div className="empty">Loading…</div></div>;

  const target = data.meta.target_multiple;
  const noValuation = data.meta.valuation_source === 'none';

  return (
    <>
      <div className="section-title">
        ×{target.toFixed(0)} radar — resemblance to a pre-expansion state
        <span className="ver" style={{ marginLeft: 8 }}>
          {data.meta.engine_version} · {data.meta.timeframe}
        </span>
      </div>

      <div className="banner note">
        <strong>THIS IS A RANKING, NOT A FORECAST</strong>
        <span>
          {data.meta.disclaimer} A ten-fold move is a rare event; most of what appears here will not
          do it. The score says an asset currently <em>looks like</em> assets have looked before large
          expansions — measured on the {data.meta.timeframe} chart, under weights that have never been
          fitted to a graded outcome.
        </span>
      </div>

      {noValuation && (
        <div className="banner stale">
          <strong>MARKET CAP UNKNOWN</strong>
          <span>
            No valuation source is configured, so the capacity reading — whether a ×{target.toFixed(0)} is
            arithmetically payable at all — is missing for every row below, and the score is computed
            from the other two readings. Set <code>CP_PROVIDER_VALUATION=coingecko</code> to fill it in.
          </span>
        </div>
      )}

      <div className="toolbar">
        <label className="filter check">
          <input
            type="checkbox"
            checked={candidatesOnly}
            onChange={(e) => setCandidatesOnly(e.target.checked)}
          />
          <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>
            Early stages only (accumulation / ignition)
          </span>
        </label>
        <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-faint)' }}>
          {data.results.length} shown · {data.meta.matched} matched
          {data.meta.no_reading.length > 0 && ` · ${data.meta.no_reading.length} without enough history`}
        </span>
      </div>

      {data.results.length === 0 ? (
        <div className="table-wrap">
          <div className="empty">
            Nothing resembles a pre-expansion state right now. That is a normal answer and a useful
            one — the scanner does not promote a best-of-a-bad-list.
          </div>
        </div>
      ) : (
        <div className="moon-grid">
          {data.results.map((r) => {
            const m = r.moonshot;
            return (
              <div key={r.symbol} className={`moon-card ${m.stage}`} onClick={() => onSelect(r.symbol)}>
                <div className="opp-head">
                  <span className="opp-sym">{r.symbol}</span>
                  <span className="opp-score" style={{ color: scoreColor(m.score) }}>
                    {num(m.score, 0)}
                    <span style={{ fontSize: 11, color: 'var(--text-faint)' }}>/100</span>
                  </span>
                </div>

                <div title={STAGE_HELP[m.stage]}>
                  <span className={`pill stage-${m.stage}`}>{m.stage}</span>{' '}
                  <span className={`pill ${r.liquidity}`}>{r.liquidity}</span>{' '}
                  <span className="pill" style={{ color: maturityColor(r.pump_maturity) }}>
                    maturity {num(r.pump_maturity, 0)}
                  </span>
                </div>

                <div className="moon-bars">
                  <Bar label="Ignition" value={m.ignition} help="Is the behaviour changing right now?" />
                  <Bar
                    label="Headroom"
                    value={m.headroom}
                    help={`How far below a price it already printed — saturates at ×${target.toFixed(0)}`}
                  />
                  <Bar
                    label="Capacity"
                    value={m.capacity}
                    help="Is a ×N payable at this market cap? Needs a valuation source."
                  />
                </div>

                <div className="opp-metrics">
                  <div className="m">
                    <span className="k">Price</span>
                    <span className="v">{price(r.price)}</span>
                  </div>
                  <div className="m">
                    <span className="k">To window high</span>
                    <span className="v">
                      {m.multiple_to_window_high === null ? DASH : `${num(m.multiple_to_window_high, 1)}x`}
                    </span>
                  </div>
                  <div className="m">
                    <span className="k">Market cap</span>
                    <span className="v">
                      {r.valuation?.market_cap_usd
                        ? compact(r.valuation.market_cap_usd)
                        : r.valuation?.market_cap_upper_bound_usd
                          ? `< ${compact(r.valuation.market_cap_upper_bound_usd)}`
                          : DASH}
                    </span>
                  </div>
                </div>

                <ul className="opp-why">
                  {m.reasons.slice(0, 3).map((w, i) => <li key={i}>{w}</li>)}
                </ul>

                {(m.caveats.length > 0 || m.unknowns.length > 0) && (
                  <ul className="opp-why risks">
                    {m.caveats.slice(0, 2).map((w, i) => <li key={`c${i}`}>{w}</li>)}
                    {m.unknowns.slice(0, 2).map((w, i) => <li key={`u${i}`}>Not measured: {w}</li>)}
                  </ul>
                )}
              </div>
            );
          })}
        </div>
      )}

      {data.meta.no_reading.length > 0 && (
        <div className="disclaimer">
          <strong>No reading for {data.meta.no_reading.length} asset(s):</strong>{' '}
          {data.meta.no_reading.slice(0, 12).join(', ')}
          {data.meta.no_reading.length > 12 && '…'}. These lack the {data.meta.timeframe} history needed
          to judge a base, so they are reported as unknown rather than scored low.
        </div>
      )}
    </>
  );
}

function Bar({ label, value, help }: { label: string; value: number | null; help: string }) {
  const known = value !== null && Number.isFinite(value);
  return (
    <div className="moon-bar" title={help}>
      <span className="k">{label}</span>
      <span className="track">
        {known && <span className="fill" style={{ width: `${value}%`, background: scoreColor(value!) }} />}
      </span>
      <span className={`v ${known ? '' : 'muted'}`}>{known ? num(value!, 0) : 'unknown'}</span>
    </div>
  );
}
