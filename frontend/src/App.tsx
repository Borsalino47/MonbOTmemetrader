import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api';
import { AlertsView } from './components/AlertsView';
import { AssetDrawer } from './components/AssetDrawer';
import { HorizonsView } from './components/HorizonsView';
import { PerformanceView } from './components/PerformanceView';
import { ScannerTable } from './components/ScannerTable';
import { TopOpportunities } from './components/TopOpportunities';
import { age, clock } from './format';
import type { AlertItem, Health, ScanResponse, ScoreRow } from './types';

type Tab = 'scanner' | 'alerts' | 'verification' | 'performance';

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
  // Where the rows currently on screen came from. Restored rows are shown
  // immediately after a restart so the dashboard is never blank, but they are
  // never presented as live.
  const [scanMeta, setScanMeta] = useState<ScanResponse['meta'] | null>(null);

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
        setScanMeta(scan.value.meta);
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
  // The server decides LIVE vs DEMO. The dashboard must never infer it from a
  // provider name it happens to recognise — a new synthetic source would slip past.
  // Server-decided, and it follows the rows on screen: a restored synthetic
  // snapshot stays DEMO even when a real feed is what the next scan will use.
  const isDemo = scanMeta ? scanMeta.data_mode === 'DEMO' : health?.data_mode === 'DEMO';
  const fromJournal = scanMeta?.source === 'journal';

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <h1>CRYPTO PULSE AI</h1>
          <span className="ver">{health?.engine_version ?? '—'}</span>
        </div>

        <nav className="nav">
          <button className={tab === 'scanner' ? 'active' : ''} onClick={() => setTab('scanner')}>Scanner</button>
          <button className={tab === 'alerts' ? 'active' : ''} onClick={() => setTab('alerts')}>
            Alerts{alerts.length > 0 ? ` (${alerts.length})` : ''}
          </button>
          <button className={tab === 'verification' ? 'active' : ''} onClick={() => setTab('verification')}>
            Verification
          </button>
          <button className={tab === 'performance' ? 'active' : ''} onClick={() => setTab('performance')}>
            Performance
          </button>
        </nav>

        <div className="status-strip">
          <div className="stat">
            <span className="k">API</span>
            <span className={`v ${providerDown ? 'bad' : 'ok'}`}>
              <span className={`dot ${providerDown ? 'bad' : 'ok'}`} />
              {providerDown ? 'DOWN' : 'OK'}
            </span>
          </div>
          <div className="stat">
            <span className="k">Data</span>
            <span className={`v ${isDemo ? 'bad' : 'ok'}`} title={health?.data_mode_detail ?? ''}>
              <span className={`dot ${isDemo ? 'bad' : 'ok'}`} />
              {health?.data_mode ?? '—'}
            </span>
          </div>
          <div className="stat">
            <span className="k">Source</span>
            <span className={`v ${isDemo ? 'warn' : 'ok'}`}>{health?.provider ?? '—'}</span>
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

      {isDemo && (
        <div className="banner synthetic">
          <strong>DEMO — NOT REAL MARKET DATA</strong>
          <span>
            {health?.data_mode_detail} Every price, volume, score and verdict below is generated.
            Run <code>python -m cryptopulse.cli doctor</code> to check whether a real exchange feed
            is reachable, then set <code>CP_PROVIDER_MARKET_DATA=binance</code> (or <code>kraken</code>).
          </span>
        </div>
      )}

      {fromJournal && (
        <div className="banner journal">
          <strong>DERNIER SCAN ENREGISTRÉ</strong>
          <span>
            Affiché depuis la base pendant qu'un scan frais tourne — {age(scanMeta?.age_seconds)} d'ancienneté.
            Ce ne sont pas des prix en direct. Les colonnes carnet d'ordres n'ont jamais été
            enregistrées et restent inconnues.
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

        {tab === 'alerts' && <AlertsView alerts={alerts} onSelect={setSelected} />}

        {tab === 'verification' && <HorizonsView />}

        {tab === 'performance' && <PerformanceView />}

        <div className="disclaimer">
          <strong>Opportunity Score is a 0–100 ranking, not a probability.</strong>{' '}
          A score of 84 means this setup ranks above one scoring 60 under the current fixed weights
          ({health?.engine_version ?? 'SCORE_ENGINE_V1'}). It does not mean an 84% chance of anything.
          The weighting is a starting hypothesis and has not been statistically validated against outcomes.
          Paper mode is {health?.paper_mode ? 'ON — no order is ever placed' : 'OFF'}.
        </div>
      </main>

      {selected && <AssetDrawer symbol={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
