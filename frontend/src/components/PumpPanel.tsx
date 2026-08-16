import { useEffect, useState } from 'react';
import { api } from '../api';
import { DASH, num, pct, price } from '../format';
import type { PumpEpisode, PumpResponse } from '../types';

/** This token's own accelerations, and whether the present resembles them.
 *
 * Three questions, in the order they are worth asking:
 *
 *   1. Has this token ever moved like this?      -> the episode list
 *   2. When it did, how far and how fast?        -> the statistics
 *   3. Does now look like those starts?          -> the similarity block
 *
 * The third is the only forward-looking one and the only one that can mislead,
 * so it is the one that refuses to print anything below the sample floor. The
 * API omits the rates entirely in that case rather than sending them for the UI
 * to grey out — a number on screen gets read however it is styled.
 *
 * Timing is rendered to the hour because detection runs on 1h candles. The
 * response carries `resolution_minutes` and this component states it rather
 * than showing a minute figure it cannot support. */
export function PumpPanel({ symbol }: { symbol: string }) {
  const [data, setData] = useState<PumpResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    api.pumps(symbol)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(String(e.message ?? e)); });
    return () => { cancelled = true; };
  }, [symbol]);

  if (error) {
    return (
      <div className="panel">
        <h3>Historique des pumps</h3>
        <div className="error-box">Historique indisponible : {error}</div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="panel">
        <h3>Historique des pumps</h3>
        <div className="loading">Lecture de l'historique 1h…</div>
      </div>
    );
  }

  const { history, stats, similarity, current_setup: now } = data;
  const episodes = [...history.episodes].sort((a, b) => b.peak_ms - a.peak_ms);

  return (
    <>
      <div className="panel">
        <h3>Historique des pumps</h3>

        <div className="kv">
          <span className="k">Épisodes trouvés</span>
          <span className="v">
            {history.episodes_found} en {num(history.days_covered, 0)} jours
          </span>
        </div>
        <div className="kv">
          <span className="k">Précision temporelle</span>
          <span className="v">
            {history.resolution_minutes === null
              ? DASH
              : `${history.resolution_minutes} min (bougies ${history.timeframe})`}
          </span>
        </div>
        <p className="pump-def">Un épisode = {history.definition_fr}.</p>

        {history.episodes_found === 0 ? (
          <div className="empty" style={{ padding: 12 }}>
            Aucune accélération de cette taille sur la période lue. C'est une réponse,
            pas une absence de données.
          </div>
        ) : (
          <>
            <SizeBars by={stats.by_size} total={history.episodes_found} />

            {stats.insufficient_sample && (
              <p className="pump-note">
                {stats.n} épisodes seulement, sous le seuil de {stats.min_sample}.
                Les valeurs ci-dessous décrivent ce petit échantillon ; elles ne
                caractérisent pas le token.
              </p>
            )}

            <div className="pump-stats">
              <Stat k="Gain médian" v={pct(stats.median_gain_pct)} />
              <Stat k="Plus gros" v={pct(stats.largest_gain_pct)} />
              <Stat
                k="Durée médiane"
                v={stats.median_minutes_to_peak === null
                  ? DASH
                  : hours(stats.median_minutes_to_peak)}
              />
              <Stat
                k="Repli après"
                v={stats.mean_drawdown_after_pct === null
                  ? DASH
                  : pct(stats.mean_drawdown_after_pct)}
              />
              <Stat
                k="RVOL au départ"
                v={stats.median_rvol_at_start === null
                  ? DASH
                  : `${num(stats.median_rvol_at_start, 1)}x`}
              />
              <Stat k="Épisodes mesurés" v={String(stats.n)} />
            </div>

            <div className="pump-list">
              {episodes.slice(0, 12).map((e) => <EpisodeRow key={e.peak_ms} e={e} />)}
            </div>
            {episodes.length > 12 && (
              <p className="pump-note">
                12 épisodes les plus récents affichés sur {episodes.length}.
              </p>
            )}
          </>
        )}

      </div>

      <div className="panel">
        <h3>Le moment présent ressemble-t-il à ces départs ?</h3>

        <div className="pump-stats">
          <Stat k="RVOL maintenant" v={now.rvol === null ? DASH : `${num(now.rvol, 1)}x`} />
          <Stat k="Volume vs avant" v={pct(now.volume_change_pct)} />
          <Stat
            k="Position dans le range"
            v={now.range_position === null ? DASH : num(now.range_position, 2)}
          />
          <Stat k="ATR" v={now.atr_pct === null ? DASH : `${num(now.atr_pct)}%`} />
        </div>

        <div className="kv" style={{ marginTop: 8 }}>
          <span className="k">Départs passés comparables</span>
          <span className="v">
            {similarity.comparable} / {similarity.examined}
          </span>
        </div>

        {similarity.insufficient_sample ? (
          <div className="empty" style={{ padding: 12, marginTop: 8 }}>
            Pas assez de cas comparables pour afficher un taux
            ({similarity.comparable} sur les {similarity.min_sample} nécessaires).
            Aucun pourcentage n'est affiché : sur quelques semaines d'historique,
            c'est le cas normal, pas une erreur.
          </div>
        ) : (
          <>
            <div className="pump-reached">
              {Object.entries(similarity.reached).map(([bucket, share]) => (
                <div className="pump-reached-row" key={bucket}>
                  <span className="k">{bucket}</span>
                  <div className="bar">
                    <div className="fill" style={{ width: `${share * 100}%` }} />
                  </div>
                  <span className="pts">{(share * 100).toFixed(0)}%</span>
                </div>
              ))}
            </div>
            <div className="pump-stats" style={{ marginTop: 8 }}>
              <Stat k="Gain médian ensuite" v={pct(similarity.median_gain_pct)} />
              <Stat
                k="Délai médian"
                v={similarity.median_minutes_to_peak === null
                  ? DASH
                  : hours(similarity.median_minutes_to_peak)}
              />
              <Stat k="Repli médian" v={pct(similarity.median_drawdown_after_pct)} />
            </div>
          </>
        )}

        {similarity.not_comparable > 0 && (
          <p className="pump-note">
            {similarity.not_comparable} épisode(s) trop anciens dans la série pour
            avoir un contexte enregistré : ils sont comptés comme non comparables,
            pas comme des échecs.
          </p>
        )}

        <p className="pump-caveat">
          Ce bloc décrit ce qui a suivi des départs ressemblants sur ce token.
          Ce n'est pas une probabilité, et le passé d'un token n'engage pas son avenir.
        </p>
      </div>
    </>
  );
}

function EpisodeRow({ e }: { e: PumpEpisode }) {
  const when = new Date(e.start_ms).toLocaleDateString('fr-FR', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  });
  return (
    <div className="pump-row">
      <span className="pump-when">{when}</span>
      <span className="pump-gain pos">+{e.gain_pct.toFixed(1)}%</span>
      <span className="pump-dur">{hours(e.minutes_to_peak)}</span>
      <span className="pump-px">{price(e.start_price)} → {price(e.peak_price)}</span>
      <span className={`pump-dd ${e.drawdown_after_pct === null ? 'muted' : 'neg'}`}>
        {e.drawdown_after_pct === null ? DASH : `${e.drawdown_after_pct.toFixed(1)}%`}
      </span>
    </div>
  );
}

function SizeBars({ by, total }: { by: Record<string, number>; total: number }) {
  const entries = Object.entries(by);
  if (entries.length === 0) return null;
  return (
    <div className="pump-sizes">
      <p className="pump-note" style={{ marginTop: 0 }}>
        Épisodes ayant <em>atteint au moins</em> cette taille. Les paliers se
        cumulent : un +12% compte dans 3%+, 5%+ et 10%+.
      </p>
      {entries.map(([bucket, n]) => (
        <div className="pump-reached-row" key={bucket}>
          <span className="k">{bucket}</span>
          <div className="bar">
            <div className="fill" style={{ width: `${(n / total) * 100}%` }} />
          </div>
          <span className="pts">{n}</span>
        </div>
      ))}
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

/** Rendered to the hour because the detection is hourly. Showing "97 min" would
 *  claim a precision the 1h candles cannot support. */
function hours(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const h = minutes / 60;
  return h < 10 ? `${h.toFixed(1)} h` : `${Math.round(h)} h`;
}
