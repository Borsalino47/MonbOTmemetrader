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
import { MarketSwitch } from './components/MarketSwitch';
import { RobinhoodView } from './components/RobinhoodView';
import { PerformanceView } from './components/PerformanceView';
import { ScannerTable } from './components/ScannerTable';
import { TopOpportunities } from './components/TopOpportunities';
import { age, clock } from './format';
import type {
  AlertItem, DecisionsResponse, FeedVerification, Health, MarketId, ProviderSummary,
  RobinhoodStatus, ScanResponse, ScoreRow,
} from './types';

type Tab = 'home' | 'scanner' | 'hunter' | 'me' | 'alerts' | 'verification' | 'performance';

/** Simple hides the measured columns; expert shows the same row with more of it.
 *  Persisted because it is a preference about the person, not about the session. */
type Mode = 'simple' | 'expert';

const MODE_KEY = 'cryptopulse.mode';
/** The chosen market survives a close (spec §3): reopening on ROBINHOOD is the
 *  whole point of remembering it. */
const MARKET_KEY = 'cryptopulse.market';

function loadMode(): Mode {
  try {
    return localStorage.getItem(MODE_KEY) === 'expert' ? 'expert' : 'simple';
  } catch {
    return 'simple';   // private browsing, or storage disabled
  }
}

function loadMarket(): MarketId {
  try {
    return localStorage.getItem(MARKET_KEY) === 'ROBINHOOD_CHAIN'
      ? 'ROBINHOOD_CHAIN' : 'BINANCE_SPOT';
  } catch {
    return 'BINANCE_SPOT';
  }
}

const REFRESH_MS = 15_000;

export default function App() {
  const [tab, setTab] = useState<Tab>('home');
  const [mode, setMode] = useState<Mode>(loadMode);
  const [market, setMarket] = useState<MarketId>(loadMarket);
  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  const [robinhood, setRobinhood] = useState<RobinhoodStatus | null>(null);
  const [verifyingChain, setVerifyingChain] = useState(false);

  useEffect(() => {
    try { localStorage.setItem(MARKET_KEY, market); } catch { /* storage unavailable */ }
  }, [market]);

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

  // The provider list is cheap (in-memory server state) and drives the switch's
  // two state badges. Polled with the normal refresh, never blocking it.
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const p = await api.providers();
        if (!cancelled) setProviders(p.providers);
      } catch { /* the switch keeps its previous states */ }
    };
    void tick();
    const t = setInterval(tick, REFRESH_MS);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  // Lazy, exactly as the backend is (spec §42): nothing Robinhood is requested
  // until the user is actually looking at that market. The first call answers
  // PENDING immediately and the badge resolves a moment later, so switching
  // never waits on the network.
  useEffect(() => {
    if (market !== 'ROBINHOOD_CHAIN') return;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const s = await api.robinhoodStatus();
        if (cancelled) return;
        setRobinhood(s);
        if (s.verification.state === 'PENDING') timer = window.setTimeout(tick, 700);
      } catch {
        if (!cancelled) timer = window.setTimeout(tick, 2000);
      }
    };
    void tick();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [market]);

  async function retryChainVerification() {
    setVerifyingChain(true);
    try {
      setRobinhood(await api.verifyRobinhood());
    } catch { /* the badge keeps its previous state */ } finally {
      setVerifyingChain(false);
    }
  }

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

  // The switch and the chain badge are two renderings of one state, and they
  // must never disagree (invariant 61, found again here: the 15s provider poll
  // still showed 🟡 "vérification…" while the badge below already said 🔴).
  // The dedicated Robinhood poll is the fresher source, so it overrides.
  const switchProviders = useMemo(() => {
    if (!robinhood) return providers;
    return providers.map((p) => (p.id === 'ROBINHOOD_CHAIN'
      ? {
        ...p,
        state: robinhood.verification.state,
        state_emoji: robinhood.verification.emoji,
        state_label_fr: robinhood.verification.label_fr,
        live_verified: robinhood.live_verified,
      }
      : p));
  }, [providers, robinhood]);
  const fromJournal = scanMeta?.source === 'journal';

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <h1>CRYPTO PULSE AI</h1>
          <span className="ver">{health?.engine_version ?? '—'}</span>
        </div>

        {/* The market switch is the primary navigation between the two
            universes, so it sits above the tabs and is the biggest control
            in the header (spec §48). */}
        <MarketSwitch market={market} onChange={setMarket} providers={switchProviders} />

        {market === 'BINANCE_SPOT' && (
        <nav className="nav">
          <button className={tab === 'home' ? 'active' : ''} onClick={() => setTab('home')}>Accueil</button>
          <button className={tab === 'scanner' ? 'active' : ''} onClick={() => setTab('scanner')}>Scanner</button>
          <button className={tab === 'hunter' ? 'active' : ''} onClick={() => setTab('hunter')}>Recherche</button>
          <button className={tab === 'me' ? 'active' : ''} onClick={() => setTab('me')}>
            Mes positions
          </button>
          <button className={tab === 'alerts' ? 'active' : ''} onClick={() => setTab('alerts')}>
            Alertes{alerts.length > 0 ? ` (${alerts.length})` : ''}
          </button>
          <button className={tab === 'verification' ? 'active' : ''} onClick={() => setTab('verification')}>
            Vérification
          </button>
          <button className={tab === 'performance' ? 'active' : ''} onClick={() => setTab('performance')}>
            Performance
          </button>
        </nav>
        )}

        {/* Phones get one line, not ten stats: the same facts, in the space a
            phone actually has. The full strip returns on a wide screen. */}
        {market === 'BINANCE_SPOT' && (
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
        )}

        {market === 'BINANCE_SPOT' && (
        <div className="status-strip">
          <div className="stat">
            <span className="k">API</span>
            <span className={`v ${providerDown ? 'bad' : 'ok'}`}>
              <span className={`dot ${providerDown ? 'bad' : 'ok'}`} />
              {providerDown ? 'DOWN' : 'OK'}
            </span>
          </div>
          <div className="stat">
            <span className="k">Données</span>
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
            <span className="k">Dernière mise à jour</span>
            <span className="v">{last ? clock(last.finished_at_ms) : '—'}</span>
          </div>
          <div className="stat">
            <span className="k">Âge du scan</span>
            <span className={`v ${isStale ? 'bad' : ''}`}>{last ? age(last.age_seconds) : '—'}</span>
          </div>
          <div className="stat">
            <span className="k">Âge des données</span>
            <span className={`v ${isStale ? 'bad' : ''}`}>{age(dataAge)}</span>
          </div>
          <div className="stat">
            <span className="k">Analysés</span>
            <span className="v">{last ? `${last.succeeded}/${last.scanned}` : '—'}</span>
          </div>
          <div className="stat">
            <span className="k">Échecs</span>
            <span className={`v ${last && last.failed > 0 ? 'warn' : ''}`}>{last?.failed ?? '—'}</span>
          </div>
          <div className="stat">
            <span className="k">Régime de marché</span>
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
            {scanning ? 'Analyse…' : 'Lancer un scan'}
          </button>
        </div>
        )}
      </header>

      {/* The home screen opens with its own trust line, in the same amber, saying
          exactly this. Repeating it immediately below is noise on a phone. The
          warning is never absent: HomeView renders that line unconditionally,
          and every other tab still gets the full banner. */}
      {market === 'BINANCE_SPOT' && isDemo && tab !== 'home' && (
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

      {market === 'BINANCE_SPOT' && fromJournal && tab !== 'home' && (
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

      {market === 'BINANCE_SPOT' && isStale && (
        <div className="banner stale">
          <strong>PÉRIMÉ</strong>
          <span>La bougie la plus récente a {age(dataAge)}. Ces valeurs ne sont pas en direct.</span>
        </div>
      )}

      <InstallPrompt />

      <main className="main">
        {/* ROBINHOOD owns the whole screen when selected: this is a different
            universe, not a filter over the same rows. Nothing Binance —
            including its feed badge — is rendered underneath, because two
            markets' states side by side is precisely the confusion §4 forbids. */}
        {market === 'ROBINHOOD_CHAIN' ? (
          <RobinhoodView
            status={robinhood}
            onRetry={retryChainVerification}
            busy={verifyingChain}
          />
        ) : (
        <>
        {/* Above everything: whether what follows can be trusted as live is
            the first question, and it changes while the user is looking. */}
        {feed && (
          <FeedBadge feed={feed} onRetry={retryVerification} busy={verifying} />
        )}
        {error && <div className="error-box">Erreur : {error}</div>}

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
              No scan has completed yet. Press <strong>Lancer un scan</strong>, or wait for the background loop
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
                <label>Opportunité ≥</label>
                <input type="range" min={0} max={100} value={minScore}
                       onChange={(e) => setMinScore(Number(e.target.value))} />
                <span className="val">{minScore}</span>
              </div>
              <div className="filter">
                <label>Maturité ≤</label>
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
                <label>État</label>
                <select value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}>
                  <option value="">Tous</option>
                  <option value="ARMED">PRÊT</option>
                  <option value="BREAKOUT">CASSURE</option>
                  <option value="RETEST">RETEST</option>
                  <option value="CONTINUATION">CONTINUATION</option>
                  <option value="ARMED,BREAKOUT,RETEST,CONTINUATION">Actionnables</option>
                  <option value="WATCH">SURVEILLER</option>
                  <option value="OBSERVE">OBSERVER</option>
                </select>
              </div>
              <div className="filter">
                <label>Liquidité ≥</label>
                <select value={minLiquidity} onChange={(e) => setMinLiquidity(e.target.value)}>
                  <option value="">Toutes</option>
                  <option value="ACCEPTABLE">ACCEPTABLE</option>
                  <option value="GOOD">BONNE</option>
                  <option value="EXCELLENT">EXCELLENTE</option>
                </select>
              </div>
              <div className="filter">
                <label>Rechercher</label>
                <input type="text" value={search} placeholder="BTC…"
                       onChange={(e) => setSearch(e.target.value)} style={{ width: 90 }} />
              </div>
              <label className="filter check">
                <input type="checkbox" checked={hideVetoed} onChange={(e) => setHideVetoed(e.target.checked)} />
                <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>Masquer les vetos</span>
              </label>
              <span className="only-wide" style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-faint)' }}>
                {filtered.length} sur {rows.length}
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
        </>
        )}

        <div className="disclaimer">
          <strong>Le score d’opportunité est un classement sur 100, pas une probabilité.</strong>{' '}
          Un score de 84 signifie que ce setup se classe au-dessus d’un setup à 60 selon les pondérations
          fixes actuelles ({health?.engine_version ?? 'SCORE_ENGINE_V1'}). Cela ne veut pas dire
          « 84 % de chances » de quoi que ce soit. Ces pondérations sont une hypothèse de départ et
          n’ont jamais été validées statistiquement contre des résultats réels.
          Mode papier : {health?.paper_mode ? 'ACTIF — aucun ordre n’est jamais passé' : 'INACTIF'}.
        </div>
      </main>

      {/* Thumb-reachable navigation. Shown only on narrow screens, where the
          top bar is out of reach of a hand holding the phone. */}
      {market === 'BINANCE_SPOT' && (
      <nav className="bottom-nav">
        <BottomTab id="home" label="Accueil" current={tab} onPick={setTab} />
        <BottomTab id="scanner" label="Scanner" current={tab} onPick={setTab} />
        <BottomTab id="hunter" label="Recherche" current={tab} onPick={setTab} />
        <BottomTab id="me" label="Moi" current={tab} onPick={setTab} />
        <BottomTab id="alerts" label="Alertes" current={tab} onPick={setTab} badge={alerts.length} />
        <BottomTab id="verification" label="Vérif." current={tab} onPick={setTab} />
        <BottomTab id="performance" label="Perf." current={tab} onPick={setTab} />
      </nav>
      )}

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
