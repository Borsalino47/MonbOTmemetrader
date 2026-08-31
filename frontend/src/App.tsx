import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api';
import { AlertsView } from './components/AlertsView';
import { AssetDrawer } from './components/AssetDrawer';
import { MoonshotView } from './components/MoonshotView';
import { MoonshotPerformance } from './components/MoonshotPerformance';
import { PerformanceView } from './components/PerformanceView';
import { ScannerTable } from './components/ScannerTable';
import { TopOpportunities } from './components/TopOpportunities';
import { age, clock } from './format';
import type { AlertItem, Health, ScoreRow } from './types';

type Tab = 'scanner' | 'moonshot' | 'alerts' | 'performance';

const REFRESH_MS = 15_000;

export default function App() {
  const [tab, setTab] = useState<Tab>('scanner');
  const [health, setHealth] = useState<Health | null>(null);
  const [rows, setRows] = useState<ScoreRow[]>([]);
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [noScanYet, setNoScanYet] = useState(false);
  const [perfAxis, setPerfAxis] = useState<'setup' | 'moonshot'>('setup');

  // Filters
  const [minScore, setMinScore] = useState(0);
  const [maxMaturity, setMaxMaturity] = useState(100);
  const [minRvol, setMinRvol] = useState(0);
  const [stateFilter, setStateFilter] = useState('');
  const [minLiquidity, setMinLiquidity] = useState('');
  const [hideVetoed, setHideVetoed] = useState(true);
  const [search, setSearch] = useState('');

  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);

  const load = useCallback(async () => {
    try {
      const h = await api.health();
      if (!mounted.current) return;
      setHealth(h);

      const [scan, al] = await Promise.allSettled([api.scan({ limit: 300 }), api.alerts(50)]);

      if (!mounted.current) return;
      if (scan.status === 'fulfilled') {
        setRows(scan.value.results);
        setNoScanYet(false);
        setError(null);
      } else if (String(scan.reason?.message).includes('NO_SCAN_YET')) {
        setNoScanYet(true);
        setError(null);
      } else {
        setError(String(scan.reason?.message ?? scan.reason));
      }
      if (al.status === 'fulfilled') setAlerts(al.value.alerts);
    } catch (e) {
      if (mounted.current) setError(String((e as Error).message ?? e));
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [load]);

  async function runScan() {
    setScanning(true);
    setError(null);
    try {
      await api.runScan();
      await load();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setScanning(false);
    }
  }

  const filtered = useMemo(() => {
    const liqRank: Record<string, number> = {
      DANGEROUS: 0, UNKNOWN: 1, POOR: 2, ACCEPTABLE: 3, GOOD: 4, EXCELLENT: 5,
    };
    const wantedStates = stateFilter ? stateFilter.split(',') : null;
    return rows.filter((r) => {
      if (r.final_score < minScore) return false;
      if (r.pump_maturity.score > maxMaturity) return false;
      if (wantedStates && !wantedStates.includes(r.setup.state)) return false;
      if (minLiquidity && liqRank[r.liquidity.status] < liqRank[minLiquidity]) return false;
      if (hideVetoed && (r.safety.hard_veto || r.liquidity.veto)) return false;
      if (search && !r.symbol.toLowerCase().includes(search.toLowerCase())) return false;
      if (minRvol > 0) {
        const rvol = r.metrics?.rvol;
        if (rvol === null || rvol === undefined || rvol < minRvol) return false;
      }
      return true;
    });
  }, [rows, minScore, maxMaturity, minRvol, stateFilter, minLiquidity, hideVetoed, search]);

  const top = useMemo(
    () => rows.filter((r) => !r.safety.hard_veto && !r.liquidity.veto && r.data_confidence.score >= 50).slice(0, 6),
    [rows],
  );

  const last = health?.last_scan;
  const dataAge = last?.market_data_age_seconds ?? null;
  // Staleness is decided server-side, where the timeframe length is known.
  const isStale = last?.data_stale ?? false;
  const providerDown = health?.provider_health?.some((p) => !p.available) ?? false;
  // "The process is up" and "the radar is scanning" are different claims, and the
  // second is the one that matters.
  const radarStatus = health?.health?.status ?? (providerDown ? 'DOWN' : 'OK');
  const radarClass = radarStatus === 'OK' ? 'ok' : radarStatus === 'DOWN' ? 'bad' : 'warn';

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <h1>CRYPTO PULSE AI</h1>
          <span className="ver">{health?.engine_version ?? '—'}</span>
        </div>

        <nav className="nav">
          <button className={tab === 'scanner' ? 'active' : ''} onClick={() => setTab('scanner')}>Scanner</button>
          <button className={tab === 'moonshot' ? 'active' : ''} onClick={() => setTab('moonshot')}>
            ×{(health?.moonshot?.target_multiple ?? 10).toFixed(0)} Radar
            {health?.moonshot?.candidates_last_scan ? ` (${health.moonshot.candidates_last_scan})` : ''}
          </button>
          <button className={tab === 'alerts' ? 'active' : ''} onClick={() => setTab('alerts')}>
            Alerts{alerts.length > 0 ? ` (${alerts.length})` : ''}
          </button>
          <button className={tab === 'performance' ? 'active' : ''} onClick={() => setTab('performance')}>
            Performance
          </button>
        </nav>

        <div className="status-strip">
          <div className="stat" title={health?.health?.reasons?.join(' · ') ?? ''}>
            <span className="k">Radar</span>
            <span className={`v ${radarClass}`}>
              <span className={`dot ${radarClass}`} />
              {health?.health?.status ?? (providerDown ? 'DOWN' : 'OK')}
            </span>
          </div>
          <div className="stat">
            <span className="k">Source</span>
            <span className={`v ${health?.synthetic_data ? 'warn' : 'ok'}`}>{health?.provider ?? '—'}</span>
          </div>
          <div className="stat">
            <span className="k">Last update</span>
            <span className="v">{last ? clock(last.finished_at_ms) : '—'}</span>
          </div>
          <div className="stat">
            <span className="k">Scan age</span>
            <span className={`v ${isStale ? 'bad' : ''}`}>{last ? age(last.age_seconds) : '—'}</span>
          </div>
          <div className="stat">
            <span className="k">Data age</span>
            <span className={`v ${isStale ? 'bad' : ''}`}>{age(dataAge)}</span>
          </div>
          <div className="stat">
            <span className="k">Scanned</span>
            <span className="v">{last ? `${last.succeeded}/${last.scanned}` : '—'}</span>
          </div>
          <div className="stat">
            <span className="k">Failed</span>
            <span className={`v ${last && last.failed > 0 ? 'warn' : ''}`}>{last?.failed ?? '—'}</span>
          </div>
          <div className="stat">
            <span className="k">Universe</span>
            <span className="v" title={health?.universe?.notes?.join(' ') ?? ''}>
              {health?.universe?.mode === 'robinhood'
                ? `Robinhood ${health?.universe?.count ?? '—'}`
                : 'Top volume'}
            </span>
          </div>
          <div className="stat">
            <span className="k">Regime</span>
            <span className="v">{health?.market_regime?.trend ?? '—'}</span>
          </div>
          <div className="stat">
            <span className="k">Mode</span>
            <span className="v ok">{health?.paper_mode ? 'PAPER' : 'LIVE'}</span>
          </div>
          <button className="action" onClick={runScan} disabled={scanning}>
            {scanning ? 'Scanning…' : 'Scan now'}
          </button>
        </div>
      </header>

      {health?.synthetic_data && (
        <div className="banner synthetic">
          <strong>SYNTHETIC DATA</strong>
          <span>{health.synthetic_warning} Every price, volume and score below is generated, not observed.</span>
        </div>
      )}

      {health?.universe?.mode === 'robinhood' && (
        <div className="banner note">
          <strong>ROBINHOOD UNIVERSE</strong>
          <span>
            Only assets believed tradable on Robinhood Crypto are scanned
            ({health.universe.count ?? 0} resolved on {health.provider}
            {health.universe.missing?.length ? `, ${health.universe.missing.length} not carried here` : ''}).
            The listing is hand-maintained and not verified against Robinhood, and every price below comes
            from the data venue — <strong>not</strong> from Robinhood, whose spread and fill will differ.
          </span>
        </div>
      )}

      {radarStatus === 'DOWN' && (
        <div className="banner stale">
          <strong>RADAR NOT SCANNING</strong>
          <span>
            {health?.health?.reasons?.join(' · ')}. Everything below is the last state it managed to
            compute, not the current market.
          </span>
        </div>
      )}

      {isStale && (
        <div className="banner stale">
          <strong>STALE DATA</strong>
          <span>The newest candle is {age(dataAge)} old. These values are not live.</span>
        </div>
      )}

      <main className="main">
        {error && <div className="error-box">Error: {error}</div>}

        {noScanYet && tab === 'scanner' && (
          <div className="table-wrap">
            <div className="empty">
              No scan has completed yet. Press <strong>Scan now</strong>, or wait for the background loop
              ({health?.scan_interval_seconds ?? '—'}s interval).
            </div>
          </div>
        )}

        {tab === 'scanner' && !noScanYet && (
          <>
            <TopOpportunities rows={top} onSelect={setSelected} />

            <div className="section-title">Live market scanner</div>
            <div className="toolbar">
              <div className="filter">
                <label>Opportunity ≥</label>
                <input type="range" min={0} max={100} value={minScore}
                       onChange={(e) => setMinScore(Number(e.target.value))} />
                <span className="val">{minScore}</span>
              </div>
              <div className="filter">
                <label>Maturity ≤</label>
                <input type="range" min={0} max={100} value={maxMaturity}
                       onChange={(e) => setMaxMaturity(Number(e.target.value))} />
                <span className="val">{maxMaturity}</span>
              </div>
              <div className="filter">
                <label>RVOL ≥</label>
                <input type="range" min={0} max={5} step={0.25} value={minRvol}
                       onChange={(e) => setMinRvol(Number(e.target.value))} />
                <span className="val">{minRvol}x</span>
              </div>
              <div className="filter">
                <label>Status</label>
                <select value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}>
                  <option value="">All</option>
                  <option value="ARMED">ARMED</option>
                  <option value="BREAKOUT">BREAKOUT</option>
                  <option value="RETEST">RETEST</option>
                  <option value="CONTINUATION">CONTINUATION</option>
                  <option value="ARMED,BREAKOUT,RETEST,CONTINUATION">Actionable</option>
                  <option value="WATCH">WATCH</option>
                  <option value="OBSERVE">OBSERVE</option>
                </select>
              </div>
              <div className="filter">
                <label>Liquidity ≥</label>
                <select value={minLiquidity} onChange={(e) => setMinLiquidity(e.target.value)}>
                  <option value="">Any</option>
                  <option value="ACCEPTABLE">ACCEPTABLE</option>
                  <option value="GOOD">GOOD</option>
                  <option value="EXCELLENT">EXCELLENT</option>
                </select>
              </div>
              <div className="filter">
                <label>Search</label>
                <input type="text" value={search} placeholder="BTC…"
                       onChange={(e) => setSearch(e.target.value)} style={{ width: 90 }} />
              </div>
              <label className="filter check">
                <input type="checkbox" checked={hideVetoed} onChange={(e) => setHideVetoed(e.target.checked)} />
                <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>Hide vetoed</span>
              </label>
              <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-faint)' }}>
                {filtered.length} of {rows.length}
              </span>
            </div>

            <ScannerTable rows={filtered} onSelect={setSelected} />
          </>
        )}

        {tab === 'moonshot' && <MoonshotView onSelect={setSelected} />}

        {tab === 'alerts' && <AlertsView alerts={alerts} onSelect={setSelected} />}

        {tab === 'performance' && (
          <>
            <div className="toolbar">
              <div className="filter">
                <label>Horizon</label>
                <select value={perfAxis} onChange={(e) => setPerfAxis(e.target.value as 'setup' | 'moonshot')}>
                  <option value="setup">Setup axis — hours</option>
                  <option value="moonshot">×10 axis — weeks</option>
                </select>
              </div>
              <span style={{ fontSize: 11, color: 'var(--text-faint)' }}>
                Two theses, two verdicts. They are graded against different labels on different
                timeframes and never share a result.
              </span>
            </div>
            {perfAxis === 'setup' ? <PerformanceView /> : <MoonshotPerformance />}
          </>
        )}

        <div className="disclaimer">
          <strong>Opportunity Score is a 0–100 ranking, not a probability.</strong>{' '}
          A score of 84 means this setup ranks above one scoring 60 under the current fixed weights
          ({health?.engine_version ?? 'SCORE_ENGINE_V1'}). It does not mean an 84% chance of anything.
          The weighting is a starting hypothesis and has not been statistically validated against outcomes.
          The same holds, more strongly, for the ×{(health?.moonshot?.target_multiple ?? 10).toFixed(0)} radar:
          it ranks resemblance to states that have preceded large expansions, it has never been graded
          against one, and most of what it surfaces will not do it.
          Paper mode is {health?.paper_mode ? 'ON — no order is ever placed' : 'OFF'}.
        </div>
      </main>

      {selected && <AssetDrawer symbol={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
