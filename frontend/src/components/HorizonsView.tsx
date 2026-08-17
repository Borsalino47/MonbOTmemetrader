import { useEffect, useState } from 'react';
import { api } from '../api';
import { DASH, num } from '../format';
import type { HorizonBucket, HorizonResponse } from '../types';

function pct(v: number | null | undefined): string {
  return v === null || v === undefined || !Number.isFinite(v) ? DASH : `${num(v * 100, 1)} %`;
}

function changeClass(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return 'muted';
  return v > 0 ? 'pos' : v < 0 ? 'neg' : 'muted';
}

/** Did the signals hold up? What the price actually did 15m / 1h / 4h / 24h later.
 *
 * Distinct from the Performance tab on purpose. Performance asks "would this
 * trade have won?"; this asks "what did the market do?". A signal can lose on
 * the barrier and still have been up 2% an hour in, and that difference is
 * information about timing which no win rate can express. */
export function HorizonsView() {
  const [data, setData] = useState<HorizonResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tracking, setTracking] = useState(false);

  async function load() {
    try {
      setData(await api.horizons());
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, []);

  async function trackNow() {
    setTracking(true);
    try {
      await api.trackHorizons();
      await load();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setTracking(false);
    }
  }

  if (error) return <div className="error-box">Could not load verification: {error}</div>;
  if (!data) return <div className="loading">Chargement…</div>;

  const p = data.performance;
  const graded = p.overall.reduce((sum, b) => sum + b.n, 0);

  return (
    <div>
      <div className="toolbar">
        <div className="filter">
          <label>Fenêtres</label>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>{p.horizons.join(' · ')}</span>
        </div>
        <div className="filter">
          <label>Coût aller-retour</label>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>
            {num(data.costs.round_trip_cost_pct as number, 2)}%
          </span>
        </div>
        <button className="action" onClick={trackNow} disabled={tracking} style={{ marginLeft: 'auto' }}>
          {tracking ? 'Vérification…' : 'Vérifier maintenant'}
        </button>
      </div>

      <div className="panel">
        <h3>Comment un signal est vérifié</h3>
        <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>
          <div>Entry — {data.tracker.entry_rule}.</div>
          <div>Réussite — {data.tracker.success_criterion}. Un signal qui finit à l’équilibre n’est pas un gain.</div>
          <div style={{ marginTop: 6 }}>
            A window that has not fully elapsed is absent from this table, never settled at
            whatever price happens to be current.
          </div>
        </div>
      </div>

      {graded === 0 ? (
        <div className="table-wrap">
          <div className="empty">
            No follow-up window has closed yet. The first result appears 15 minutes after the
            first signal, the last a full day later. Nothing here is estimated in the meantime.
          </div>
        </div>
      ) : (
        <>
          {p.overall.some((b) => b.n > 0 && b.insufficient_sample) && (
            <div className="banner stale" style={{ borderRadius: 6, marginBottom: 14 }}>
              <strong>ÉCHANTILLON INSUFFISANT</strong>
              <span>
                Some windows have fewer than {p.min_sample} results. Those rows describe what
                happened; they are not yet evidence about what will happen.
              </span>
            </div>
          )}

          <HorizonTable
            title="Ce que le prix a réellement fait après chaque signal"
            subtitle="La variation est mesurée du prix d’entrée à la clôture de la fenêtre, nette de frais."
            buckets={p.overall}
            keyLabel="Fenêtre"
            showKey={false}
          />

          <HorizonTable
            title="Par score d’opportunité — un score plus élevé fait-il réellement mieux ?"
            subtitle="Si les tranches ne se séparent pas, le score ne porte aucune information sur les résultats."
            buckets={p.by_score_band}
            keyLabel="Tranche de score"
          />

          <HorizonTable
            title="Par état du setup"
            buckets={p.by_state}
            keyLabel="État"
          />

          {p.by_provider.length > 1 && (
            <HorizonTable title="Par source de données" buckets={p.by_provider} keyLabel="Source" />
          )}
        </>
      )}

      {p.notes.length > 0 && (
        <div className="disclaimer">
          {p.notes.map((n, i) => <div key={i} style={{ marginBottom: 5 }}>{n}</div>)}
        </div>
      )}
    </div>
  );
}

function HorizonTable({
  title, subtitle, buckets, keyLabel, showKey = true,
}: {
  title: string;
  subtitle?: string;
  buckets: HorizonBucket[];
  keyLabel: string;
  showKey?: boolean;
}) {
  const rows = buckets.filter((b) => b.n > 0 || !showKey);
  if (rows.length === 0) return null;

  return (
    <div className="panel">
      <h3>{title}</h3>
      {subtitle && (
        <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 10, lineHeight: 1.55 }}>
          {subtitle}
        </div>
      )}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {showKey && <th className="left">{keyLabel}</th>}
              <th className="left">Fenêtre</th>
              <th>n</th>
              <th>Réussite</th>
              <th>Médiane</th>
              <th>Moyenne</th>
              <th>Meilleur</th>
              <th>Pire</th>
              <th>Perte max moy.</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((b) => (
              <tr
                key={`${b.key}-${b.horizon}`}
                style={b.n === 0 || b.insufficient_sample ? { opacity: 0.5 } : undefined}
              >
                {showKey && <td className="left">{b.key}</td>}
                <td className="left" style={{ fontFamily: 'var(--mono)' }}>{b.horizon}</td>
                <td>{b.n}</td>
                <td>
                  {pct(b.success_rate)}
                  {b.n > 0 && b.insufficient_sample && (
                    <span className="muted" style={{ fontSize: 10, marginLeft: 6 }}>n faible</span>
                  )}
                </td>
                <td className={changeClass(b.median_change_pct)}>{num(b.median_change_pct)}%</td>
                <td className={changeClass(b.avg_change_pct)}>{num(b.avg_change_pct)}%</td>
                <td className="pos">{num(b.best_change_pct)}%</td>
                <td className="neg">{num(b.worst_change_pct)}%</td>
                <td className="neg">{num(b.avg_max_drawdown_pct)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
