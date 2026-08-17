import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api';
import { AlertsView } from './components/AlertsView';
import { AssetDrawer } from './components/AssetDrawer';
import { AssetCards } from './components/AssetCards';
import { HomeView } from './components/HomeView';
import { HorizonsView } from './components/HorizonsView';
import { HunterView } from './components/HunterView';
import { FeedBadge } from './components/FeedBadge';
import { MeView } from './components/MeView';
import { InstallPrompt } from './components/InstallPrompt';
import { PerformanceView } from './components/PerformanceView';
import { ScannerTable } from './components/ScannerTable';
import { TopOpportunities } from './components/TopOpportunities';
import { age, clock } from './format';
import type {
  AlertItem, DecisionsResponse, FeedVerification, Health, ScanResponse, ScoreRow,
} from './types';

type Tab = 'home' | 'scanner' | 'hunter' | 'me' | 'alerts' | 'verification' | 'performance';

/** Simple hides the measured columns; expert shows the same row with more of it.
 *  Persisted because it is a preference about the person, not about the session. */
type Mode = 'simple' | 'expert';

const MODE_KEY = 'cryptopulse.mode';

function loadMode(): Mode {
  try {
    return localStorage.getItem(MODE_KEY) === 'expert' ? 'expert' : 'simple';
  } catch {
    return 'simple';   // private browsing, or storage disabled
  }
}

const REFRESH_MS = 15_000;

export default function App() {
  const [tab, setTab] = useState<Tab>('home');
  const [mode, setMode] = useState<Mode>(loadMode);

  useEffect(() => {
    try { localStorage.setItem(MODE_KEY, mode); } catch { /* storage unavailable */ }
  }, [mode]);
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
  const [decisions, setDecisions] = useState<DecisionsResponse | null>(null);
  const [feed, setFeed] = useState<FeedVerification | null>(null);
  const [verifying, setVerifying] = useState(false);

  // Filters
  const [minScore, setMinScore] = useState(0);
  const [maxMaturity, setMaxMaturity] = useState(100);
  const [minRvol, setMinRvol] = useState(0);
  const [stateFilter, setStateFilter] = useState('');
  const [minLiquidity, setMinLiquidity] = useState('');
  const [hideVetoed, setHideVetoed] = useState(true);
  const [search, setSearch] = useState('');
  // Filters are collapsed by default on a phone: six controls before the first
  // row would push the list itself off the screen.
  const [showFilters, setShowFilters] = useState(false);

  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);

  const load = useCallback(async () => {
    try {
      const h = await api.health();
      if (!mounted.current) return;
      setHealth(h);

      const [scan, al, dec] = await Promise.allSettled([
        api.scan({ limit: 300 }), api.alerts(50), api.decisions(),
      ]);

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
      // Decisions are allowed to fail on their own: the scanner is still
      // useful without them, and a blank decision section is better than a
      // blank page.
      if (dec.status === 'fulfilled') setDecisions(dec.value);
    } catch (e) {
      if (mounted.current) setError(String((e as Error).message ?? e));
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [load]);

  // The feed check is polled fast while it is running and then left alone. A
  // badge that says "vérification en cours" and only updates on the next slow
  // refresh reads as stuck, which is worse than showing nothing.
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const s = await api.startup();
        if (cancelled) return;
        setFeed(s.feed_verification);
        if (s.feed_verification.state === 'PENDING') {
          timer = window.setTimeout(tick, 700);
        }
      } catch {
        if (!cancelled) timer = window.setTimeout(tick, 2000);
      }
    };
    void tick();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, []);

  async function retryVerification() {
    setVerifying(true);
    try {
      const r = await fetch('/api/feed/verify', { method: 'POST' });
      if (r.ok) setFeed(await r.json());
    } catch { /* the badge keeps its previous state */ } finally {
      setVerifying(false);
    }
  }

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
          <button className={tab === 'home' ? 'active' : ''} onClick={() => setTab('home')}>Accueil</button>
          <button className={tab === 'scanner' ? 'active' : ''} onClick={() => setTab('scanner')}>Scanner</button>
          <button className={tab === 'hunter' ? 'active' : ''} onClick={() => setTab('hunter')}>Recherche</button>
          <button className={tab === 'me' ? 'active' : ''} onClick={() => setTab('me')}>
            Mes positions
          </button>
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

        {/* Phones get one line, not ten stats: the same facts, in the space a
            phone actually has. The full strip returns on a wide screen. */}
        <div className="status-compact">
          <span className={`dot ${isDemo || providerDown ? 'bad' : 'ok'}`} />
          {/* LIVE means "not generated data". It does not mean the feed has been
              verified — that is the badge below, and the two contradicting each
              other in the same header is exactly what must not happen. */}
          <span className={isDemo || feed?.state === 'FAILED' ? 'bad' : 'ok'}>
            {health?.data_mode ?? '—'}
            {feed?.state === 'FAILED' ? ' (non vérifié)' : ''}
          </span>
          <span className="sep">·</span>
          <span className={isStale ? 'bad' : ''}>{last ? age(last.age_seconds) : '—'}</span>
          <span className="sep">·</span>
          <span>{last ? `${last.succeeded}/${last.scanned}` : '—'}</span>
          <button
            className="mode-toggle"
            onClick={() => setMode(mode === 'simple' ? 'expert' : 'simple')}
          >
            {mode === 'simple' ? 'Simple' : 'Expert'}
          </button>
          <button className="action small" onClick={runScan} disabled={scanning}>
            {scanning ? '…' : 'Scan'}
          </button>
        </div>

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
          <button
            className="mode-toggle"
            onClick={() => setMode(mode === 'simple' ? 'expert' : 'simple')}
            title="Simple montre l'essentiel ; expert ajoute les colonnes mesurées"
          >
            {mode === 'simple' ? 'Simple' : 'Expert'}
          </button>
          <button className="action" onClick={runScan} disabled={scanning}>
            {scanning ? 'Scanning…' : 'Scan now'}
          </button>
        </div>
      </header>

      {/* The home screen opens with its own trust line, in the same amber, saying
          exactly this. Repeating it immediately below is noise on a phone. The
          warning is never absent: HomeView renders that line unconditionally,
          and every other tab still gets the full banner. */}
      {isDemo && tab !== 'home' && (
        <div className="banner synthetic">
          <strong>DÉMO</strong>
          <span className="banner-short">Chiffres générés — aucun ne vient d'un marché.</span>
          <span className="banner-long">
            {health?.data_mode_detail} Chaque prix, volume, score et verdict ci-dessous est généré.
            Lancez <code>python -m cryptopulse.cli doctor</code> pour savoir si un vrai flux est
            joignable, puis réglez <code>CP_PROVIDER_MARKET_DATA=binance</code> (ou <code>kraken</code>).
          </span>
        </div>
      )}

      {fromJournal && tab !== 'home' && (
        <div className="banner journal">
          <strong>ENREGISTRÉ</strong>
          <span className="banner-short">
            Dernier scan, {age(scanMeta?.age_seconds)} d'ancienneté. Pas des prix en direct.
          </span>
          <span className="banner-long">
            Affiché depuis la base pendant qu'un scan frais tourne — {age(scanMeta?.age_seconds)} d'ancienneté.
            Ce ne sont pas des prix en direct. Les colonnes carnet d'ordres n'ont jamais été
            enregistrées et restent inconnues.
          </span>
        </div>
      )}

      {isStale && (
        <div className="banner stale">
          <strong>PÉRIMÉ</strong>
          <span>La bougie la plus récente a {age(dataAge)}. Ces valeurs ne sont pas en direct.</span>
        </div>
      )}

      <InstallPrompt />

      <main className="main">
        {/* Above everything: whether what follows can be trusted as live is
            the first question, and it changes while the user is looking. */}
        {feed && (
          <FeedBadge feed={feed} onRetry={retryVerification} busy={verifying} />
        )}
        {error && <div className="error-box">Error: {error}</div>}

        {tab === 'home' && (
          <HomeView
            rows={rows}
            alerts={alerts}
            health={health}
            journalAgeSeconds={fromJournal ? (scanMeta?.age_seconds ?? 0) : null}
            onOpenTab={(t) => setTab(t as Tab)}
            onSelect={setSelected}
            scanning={scanning}
            onScan={runScan}
            decisions={decisions}
            feed={feed}
          />
        )}

        {noScanYet && (tab === 'scanner' || tab === 'home') && (
          <div className="table-wrap">
            <div className="empty">
              No scan has completed yet. Press <strong>Scan now</strong>, or wait for the background loop
              ({health?.scan_interval_seconds ?? '—'}s interval).
            </div>
          </div>
        )}

        {tab === 'scanner' && !noScanYet && (
          <>
            {/* Wide screens only: the home tab already shows the top setups, and
                repeating them here costs a phone screen before the list starts. */}
            <div className="only-wide">
              <TopOpportunities rows={top} onSelect={setSelected} />
            </div>

            <div className="scanner-head">
              <span className="section-title">Scanner — {filtered.length} / {rows.length}</span>
              <button className="filters-toggle" onClick={() => setShowFilters(!showFilters)}>
                {showFilters ? 'Masquer les filtres' : 'Filtres'}
              </button>
            </div>

            <div className={`toolbar ${showFilters ? 'open' : ''}`}>
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
              <span className="only-wide" style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-faint)' }}>
                {filtered.length} of {rows.length}
              </span>
            </div>

            {/* Two renderings of the same rows. CSS decides which is visible:
                a thirteen-column table is right on a desktop and wrong on a
                phone, where it becomes horizontal scrolling. */}
            <div className="only-wide">
              <ScannerTable rows={filtered} onSelect={setSelected} />
            </div>
            <div className="only-narrow">
              <AssetCards rows={filtered} onSelect={setSelected} expert={mode === 'expert'} />
            </div>

            {mode === 'simple' && (
              <p className="mode-note">
                Mode simple. Le score d'explosion 15 min et l'historique des pumps arrivent
                en phases 06 et 07 — ils ne sont pas masqués ici, ils n'existent pas encore.
              </p>
            )}
          </>
        )}

        {tab === 'alerts' && <AlertsView alerts={alerts} onSelect={setSelected} />}

        {tab === 'hunter' && <HunterView onSelect={setSelected} />}

        {tab === 'me' && <MeView onSelect={setSelected} />}

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

      {/* Thumb-reachable navigation. Shown only on narrow screens, where the
          top bar is out of reach of a hand holding the phone. */}
      <nav className="bottom-nav">
        <BottomTab id="home" label="Accueil" current={tab} onPick={setTab} />
        <BottomTab id="scanner" label="Scanner" current={tab} onPick={setTab} />
        <BottomTab id="hunter" label="Recherche" current={tab} onPick={setTab} />
        <BottomTab id="me" label="Moi" current={tab} onPick={setTab} />
        <BottomTab id="alerts" label="Alertes" current={tab} onPick={setTab} badge={alerts.length} />
        <BottomTab id="verification" label="Vérif." current={tab} onPick={setTab} />
        <BottomTab id="performance" label="Perf." current={tab} onPick={setTab} />
      </nav>

      {selected && <AssetDrawer symbol={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function BottomTab({
  id, label, current, onPick, badge,
}: {
  id: Tab;
  label: string;
  current: Tab;
  onPick: (t: Tab) => void;
  badge?: number;
}) {
  return (
    <button
      className={current === id ? 'active' : ''}
      onClick={() => onPick(id)}
      aria-current={current === id ? 'page' : undefined}
    >
      {label}
      {badge ? <span className="bottom-badge">{badge}</span> : null}
    </button>
  );
}
