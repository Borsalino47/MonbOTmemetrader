import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { DASH, num } from '../format';
import type { RobinhoodBucket, RobinhoodPerformanceResponse } from '../types';

/** What the price actually did after each Robinhood decision.
 *
 * The only screen in the Robinhood half that can show an engine to be wrong.
 * Everything else here is an opinion produced under weights nobody has
 * validated; this is the measurement.
 *
 * The decision table comes first on purpose: 🟢 ACHETER beating 🟡 SURVEILLER
 * beating ⚫ NE PAS ACHETER is the single claim the whole engine rests on, and
 * if the order is absent the floors are wrong. Every rate is rendered with its
 * count beside it, and a bucket under the sample floor is labelled rather than
 * hidden — on a chain this young, insufficient is the ordinary case, and a
 * blank screen would read as breakage. */
export function RobinhoodPerformance() {
  const [data, setData] = useState<RobinhoodPerformanceResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [useNet, setUseNet] = useState(true);

  const load = useCallback(async () => {
    try {
      setData(await api.robinhoodPerformance(useNet));
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }, [useNet]);

  useEffect(() => { void load(); }, [load]);

  async function grade() {
    setBusy(true);
    try {
      await api.robinhoodTrack();
      await load();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  }

  if (error) return <div className="error-box">Vérification indisponible : {error}</div>;
  if (!data) return <div className="loading">Chargement…</div>;

  const p = data.performance;

  return (
    <div className="panel">
      <div className="section-title">
        Ce que le prix a réellement fait après chaque décision Robinhood
      </div>

      <div className="perf-controls">
        <label className="perf-toggle">
          <input type="checkbox" checked={useNet} onChange={(e) => setUseNet(e.target.checked)} />
          Net du coût aller-retour ({num(data.costs.round_trip_pct, 1)}&#8239;%)
        </label>
        <button className="action small" onClick={grade} disabled={busy}>
          {busy ? 'Évaluation…' : 'Évaluer maintenant'}
        </button>
      </div>

      <div className="rh-journal-counts">
        <Stat k="Décisions enregistrées" v={String(data.counts.decisions)} />
        <Stat k="Fenêtres évaluées" v={String(data.counts.graded_windows)} />
        <Stat k="Non résolues" v={String(data.counts.unresolvable_windows)} />
      </div>

      {p.overall.every((b) => b.n === 0) ? (
        <div className="empty" style={{ padding: 12 }}>
          Aucune fenêtre n'est encore écoulée. Une fenêtre non écoulée est absente,
          jamais réglée au prix actuel : c'est une réponse, pas une donnée manquante.
        </div>
      ) : (
        <>
          <BucketTable
            title="Par décision — le tableau qui compte"
            subtitle="🟢 ACHETER doit faire mieux que 🟡 SURVEILLER, qui doit faire mieux que ⚫ NE PAS ACHETER. Sinon ce sont les planchers qui sont faux."
            buckets={p.by_action}
            horizons={p.horizons}
          />
          <BucketTable
            title="Par score de précocité"
            buckets={p.by_early_band}
            horizons={p.horizons}
          />
          <BucketTable
            title={`Par score d’explosion — la ligne ${p.explosion_claim_horizon} est son bulletin`}
            subtitle="C'est la seule fenêtre sur laquelle ce score avance une affirmation."
            buckets={p.by_explosion_band}
            horizons={p.horizons}
          />
          <BucketTable title="Par risque de rug" buckets={p.by_rug_risk} horizons={p.horizons} />
          <BucketTable title="Par âge du pool" buckets={p.by_age_bucket} horizons={p.horizons} />
        </>
      )}

      {p.notes.map((n, i) => <p className="feed-note" key={i}>{n}</p>)}
      <p className="feed-note">{data.costs.note}</p>
    </div>
  );
}

function BucketTable({
  title, subtitle, buckets, horizons,
}: {
  title: string;
  subtitle?: string;
  buckets: RobinhoodBucket[];
  horizons: string[];
}) {
  if (buckets.length === 0) return null;
  return (
    <div className="perf-block">
      <h3>{title}</h3>
      {subtitle && <p className="feed-note">{subtitle}</p>}
      {horizons.map((h) => {
        const rows = buckets.filter((b) => b.horizon === h);
        if (rows.length === 0) return null;
        return (
          <div className="table-wrap" key={h}>
            <div className="perf-horizon">{h}</div>
            <table>
              <thead>
                <tr>
                  <th className="left">Tranche</th>
                  <th>n</th>
                  <th>Réussite</th>
                  <th>Médiane</th>
                  <th>Meilleur</th>
                  <th>Pire</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((b) => (
                  <tr key={`${b.horizon}-${b.bucket}`}>
                    <td className="left">{b.bucket}</td>
                    <td>{b.n}</td>
                    <td>
                      {b.success_rate === null ? DASH : `${num(b.success_rate * 100, 0)} %`}
                      {/* The count is never far from the rate, and a bucket
                          below the floor says so rather than being hidden.
                          The separator is a real space rather than a CSS
                          margin: a margin looks right but concatenates when
                          the cell is read out or copied, giving "0 %n faible". */}
                      {b.insufficient_sample && b.n > 0 && (
                        <>
                          {' '}
                          <span className="muted" style={{ fontSize: 10 }}>(n faible)</span>
                        </>
                      )}
                    </td>
                    <td>{b.median_change_pct === null ? DASH : `${num(b.median_change_pct, 1)} %`}</td>
                    <td>{b.best_pct === null ? DASH : `${num(b.best_pct, 1)} %`}</td>
                    <td>{b.worst_pct === null ? DASH : `${num(b.worst_pct, 1)} %`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })}
    </div>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div className="pump-stat">
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </div>
  );
}
