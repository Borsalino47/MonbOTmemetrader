import { useEffect, useState } from 'react';
import { api } from '../api';
import { DASH, num, pct } from '../format';
import type { ResultsResponse, TradeBucket } from '../types';

/** 📊 Mes résultats — what was decided, and what followed.
 *
 * Two questions kept visibly apart: did the engine recommend well, and did the
 * user do well. They are not the same number, and merging them would hide
 * whichever of the two was carrying the result.
 *
 * Almost everything here will read "échantillon insuffisant" for months. That
 * is the correct output on a journal that is days old, not a failure, and the
 * screen says so rather than showing a percentage over four trades. */
export function ResultsView() {
  const [data, setData] = useState<ResultsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.results()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(String(e.message ?? e)); });
    return () => { cancelled = true; };
  }, []);

  if (error) return <div className="error-box">Résultats indisponibles : {error}</div>;
  if (!data) return <div className="loading">Chargement…</div>;

  const s = data.signals;

  return (
    <div className="results">
      <h3 className="res-h">Les recommandations</h3>
      <div className="res-counts">
        <Count n={s.buy_total} label="signaux ACHETER" />
        <Count n={s.taken} label="suivis" />
        <Count n={s.skipped} label="ignorés" />
        <Count n={s.unanswered} label="sans réponse" muted />
      </div>
      {s.unanswered > 0 && (
        <p className="res-note">
          « Sans réponse » n'est pas « ignoré » : ce sont des recommandations auxquelles
          vous n'avez pas encore répondu, et elles ne comptent d'aucun côté.
        </p>
      )}

      <h3 className="res-h">Mes positions</h3>
      <div className="res-counts">
        <Count n={data.positions.opened} label="ouvertes au total" />
        <Count n={data.positions.closed} label="clôturées" />
      </div>
      <BucketCard title="Toutes positions clôturées" b={data.positions.overall} />

      {data.positions.by_strength.length > 0 && (
        <>
          <h3 className="res-h">Par force du signal</h3>
          {data.positions.by_strength.map((b) => (
            <BucketCard key={b.key} title={b.key} b={b} />
          ))}
        </>
      )}

      <h3 className="res-h">Suivis contre ignorés</h3>
      <div className="res-pair">
        <BucketCard title="Signaux suivis" b={data.taken_vs_skipped.taken} />
        <BucketCard title="Signaux ignorés" b={data.taken_vs_skipped.skipped} />
      </div>
      {data.taken_vs_skipped.mean_difference_pct !== null ? (
        <p className="res-delta">
          Écart moyen : {pct(data.taken_vs_skipped.mean_difference_pct)} en faveur des
          signaux suivis.
        </p>
      ) : (
        <p className="res-note">
          Aucun écart n'est calculé : il faut que les deux côtés atteignent
          {' '}{data.min_sample} observations. Une différence entre deux chiffres peu
          fiables n'est pas un résultat.
        </p>
      )}
      <p className="res-note">{data.taken_vs_skipped.note}</p>

      <h3 className="res-h">Les signaux VENDRE ont-ils évité des baisses ?</h3>
      <div className="res-sells">
        {Object.entries(data.sell_signals.by_horizon).map(([horizon, b]) => (
          <div className="res-sell" key={horizon}>
            <span className="k">{horizon}</span>
            <span className="v">
              {b.insufficient_sample ? DASH : pct(b.median_pct)}
            </span>
            <span className="n">n={b.n}</span>
          </div>
        ))}
      </div>
      <p className="res-note">{data.sell_signals.note}</p>

      {data.notes.map((n, i) => <p className="res-note" key={i}>{n}</p>)}
    </div>
  );
}

function BucketCard({ title, b }: { title: string; b: TradeBucket }) {
  return (
    <div className="res-bucket">
      <div className="res-bucket-head">
        <span className="res-bucket-title">{title}</span>
        <span className="res-bucket-n">n={b.n}</span>
      </div>

      {b.insufficient_sample ? (
        <p className="res-insufficient">
          {b.n} observation{b.n > 1 ? 's' : ''}, sous le seuil de {b.min_sample}.
          Aucun taux n'est affiché : un pourcentage sur une poignée de cas n'est pas
          une mesure.
        </p>
      ) : (
        <div className="res-grid">
          <Stat k="Taux de réussite" v={b.win_rate === null ? DASH : `${(b.win_rate * 100).toFixed(0)}%`} />
          <Stat k="Gain moyen" v={pct(b.mean_win_pct)} />
          <Stat k="Perte moyenne" v={pct(b.mean_loss_pct)} />
          <Stat k="Résultat moyen" v={pct(b.mean_pct)} />
          <Stat k="Profit factor" v={b.profit_factor === null ? DASH : num(b.profit_factor, 2)} />
          <Stat k="MFE moyen" v={pct(b.mean_mfe_pct)} />
          <Stat k="MAE moyen" v={pct(b.mean_mae_pct)} />
          <Stat
            k="Durée médiane"
            v={b.median_minutes_held === null ? DASH : `${Math.round(b.median_minutes_held / 60)} h`}
          />
        </div>
      )}
    </div>
  );
}

function Count({ n, label, muted }: { n: number; label: string; muted?: boolean }) {
  return (
    <div className={`res-count ${muted ? 'muted' : ''}`}>
      <span className="res-count-n">{n}</span>
      <span className="res-count-l">{label}</span>
    </div>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div className="res-stat">
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </div>
  );
}
