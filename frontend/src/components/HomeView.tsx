import { age, num, pct, scoreColor, signClass } from '../format';
import type { AlertItem, Health, ScoreRow } from '../types';
import { InstallStatus } from './InstallPrompt';
import { VerdictBadge } from './VerdictBadge';

interface Props {
  rows: ScoreRow[];
  alerts: AlertItem[];
  health: Health | null;
  /** Age of restored rows when the screen is showing the journal, else null.
   *  Passed in because on this tab the state line is the ONLY warning shown. */
  journalAgeSeconds: number | null;
  onOpenTab: (tab: string) => void;
  onSelect: (symbol: string) => void;
  scanning: boolean;
  onScan: () => void;
}

/** The five-second view: is anything happening, and can I trust it?
 *
 * Deliberately answers those two questions before anything else. The state
 * line is first because a number computed from a dead feed is worse than no
 * number, and the top setups come next because that is what the product is for.
 *
 * Sections whose engines are not built yet are shown as pending rather than
 * hidden. Hiding them would suggest they were forgotten; faking them with an
 * empty list would suggest the search ran and found nothing. */
export function HomeView({
  rows, alerts, health, journalAgeSeconds, onOpenTab, onSelect, scanning, onScan,
}: Props) {
  const top = rows
    .filter((r) => !r.safety.hard_veto && !r.liquidity.veto && r.data_confidence.score >= 50)
    .slice(0, 3);

  const strong = rows.filter((r) => r.verdict?.level === 'STRONG').length;
  const risky = rows.filter((r) => r.verdict?.level === 'AVOID').length;
  const loud = alerts.filter((a) => a.level === 'HIGH' || a.level === 'CRITICAL_SETUP');

  const last = health?.last_scan;
  const isDemo = health?.data_mode === 'DEMO';
  const isStale = last?.data_stale ?? false;
  const fromJournal = journalAgeSeconds !== null;
  const feedBad =
    isDemo || isStale || fromJournal || (health?.provider_health?.some((p) => !p.available) ?? false);

  return (
    <div className="home">
      {/* Can I trust what follows? Answered before anything numeric. */}
      <button
        className={`home-state ${feedBad ? 'bad' : 'ok'}`}
        onClick={() => onOpenTab('performance')}
      >
        <span className="home-state-dot" />
        <span className="home-state-text">
          {isDemo
            ? 'DÉMO — chiffres générés, pas un marché'
            : fromJournal
              ? `Dernier scan enregistré, ${age(journalAgeSeconds)} — pas des prix en direct`
              : isStale
                ? `Flux en retard — données vieilles de ${age(last?.market_data_age_seconds)}`
                : `Flux à jour — ${health?.provider ?? '—'}, ${age(last?.age_seconds)}`}
        </span>
        <span className="home-state-meta">
          {last ? `${last.succeeded}/${last.scanned}` : '—'}
        </span>
      </button>

      {/* What needs attention right now. */}
      <section className="home-block">
        <div className="home-head">
          <h2>Ce qui bouge</h2>
          <button className="home-refresh" onClick={onScan} disabled={scanning}>
            {scanning ? 'Scan…' : 'Scanner'}
          </button>
        </div>

        <div className="home-counts">
          <Count n={loud.length} label="alertes fortes" tone={loud.length ? 'warn' : 'muted'} />
          <Count n={strong} label="forte opportunité" tone={strong ? 'good' : 'muted'} />
          <Count n={rows.length} label="actifs suivis" tone="muted" />
          <Count n={risky} label="à éviter" tone={risky ? 'bad' : 'muted'} />
        </div>
      </section>

      {/* The product's actual answer: best setups, not biggest movers. */}
      <section className="home-block">
        <div className="home-head">
          <h2>Top 3 maintenant</h2>
          <button className="home-more" onClick={() => onOpenTab('scanner')}>Tout voir</button>
        </div>

        {top.length === 0 ? (
          <p className="home-empty">
            Aucun actif ne passe les filtres. C'est une réponse valable — le scanner ne
            promeut pas le moins mauvais d'une mauvaise liste.
          </p>
        ) : (
          <div className="home-top">
            {top.map((r, i) => (
              <button key={r.symbol} className="home-card" onClick={() => onSelect(r.symbol)}>
                <span className="home-rank">{i + 1}</span>
                <span className="home-card-body">
                  <span className="home-card-top">
                    <span className="home-sym">{r.symbol}</span>
                    <span className="home-score" style={{ color: scoreColor(r.final_score) }}>
                      {num(r.final_score, 0)}<small>/100</small>
                    </span>
                  </span>
                  {r.verdict && <VerdictBadge verdict={r.verdict} size="sm" />}
                  <span className="home-why">{r.why[0] ?? r.setup.rationale}</span>
                  <span className="home-card-metrics">
                    <span className={signClass(r.metrics?.change_1h_pct)}>
                      1h {pct(r.metrics?.change_1h_pct)}
                    </span>
                    <span>RVOL {r.metrics?.rvol ? `${num(r.metrics.rvol, 1)}x` : '—'}</span>
                    <span>Maturité {num(r.pump_maturity.score, 0)}</span>
                  </span>
                </span>
              </button>
            ))}
          </div>
        )}
      </section>

      {/* Everything else, one tap away. */}
      <section className="home-block">
        <h2>Explorer</h2>
        <div className="home-tiles">
          <Tile
            label="Alertes"
            hint={alerts.length ? `${alerts.length} récentes` : 'aucune récente'}
            onClick={() => onOpenTab('alerts')}
          />
          <Tile
            label="Vérification"
            hint="ce que le prix a fait après"
            onClick={() => onOpenTab('verification')}
          />
          <Tile
            label="Performance"
            hint="taux de réussite réel"
            onClick={() => onOpenTab('performance')}
          />
          <Tile
            label="Recherche token"
            hint="tout le venue, 0 requête"
            onClick={() => onOpenTab('hunter')}
          />
          {/* Pump history belongs to one token, so this opens a token rather than
              a list. When nothing passes the filters there is no token to open,
              and the scanner is where one gets chosen. */}
          <Tile
            label="Historique pumps"
            hint={top.length ? `${top[0].symbol} et chaque fiche` : 'ouvrez une fiche token'}
            onClick={() => (top.length ? onSelect(top[0].symbol) : onOpenTab('scanner'))}
          />
          <Tile label="Tokens validés" hint="phase 07" pending />
        </div>
        <p className="home-note">
          La dernière attend son moteur. Elle est affichée grisée plutôt que masquée :
          rien n'est oublié, et rien n'est simulé en attendant.
        </p>
        <InstallStatus />
      </section>
    </div>
  );
}

function Count({ n, label, tone }: { n: number; label: string; tone: string }) {
  return (
    <div className={`home-count ${tone}`}>
      <span className="home-count-n">{n}</span>
      <span className="home-count-l">{label}</span>
    </div>
  );
}

function Tile({
  label, hint, onClick, pending = false,
}: { label: string; hint: string; onClick?: () => void; pending?: boolean }) {
  return (
    <button
      className={`home-tile ${pending ? 'pending' : ''}`}
      onClick={onClick}
      disabled={pending}
      aria-disabled={pending}
    >
      <span className="home-tile-label">{label}</span>
      <span className="home-tile-hint">{hint}</span>
    </button>
  );
}
