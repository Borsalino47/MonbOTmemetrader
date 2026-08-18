import { age, compact, num, price } from '../format';
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { DECISION_LABELS, DecisionBadge } from './DecisionBadge';
import { SEVERITY_FR, labelFor } from '../i18n/fr';
import { TokenCrossCheck } from './TokenCrossCheck';
import type {
  RhScoreBlock,
  RobinhoodToken,
  RobinhoodTokensResponse,
  SafetyReport,
} from '../types';

/** New tokens on Robinhood Chain, newest first, filterable by age.
 *
 * Every figure is either what the source returned or an em dash. There is no
 * opportunity score here and no BUY — discovery answers "what appeared and
 * when", and the scores are separate engines in later phases.
 *
 * Safety is the exception, and it sits *above* the market figures on purpose:
 * a token that cannot be sold has no interesting volume, and putting the
 * numbers first would invite reading them first. */
export function RobinhoodTokens() {
  const [data, setData] = useState<RobinhoodTokensResponse | null>(null);
  const [bucket, setBucket] = useState<string>('');
  const [sort, setSort] = useState<'age' | 'early' | 'decision'>('age');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.robinhoodTokens(bucket || undefined, sort));
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }, [bucket, sort]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      await load();
      if (cancelled) return;
      // Poll only while the first search is still running, then stop: a list
      // that reloads on a timer would fight the user's scrolling.
      setData((d) => {
        if (d?.state === 'PENDING') timer = window.setTimeout(tick, 900);
        return d;
      });
    };
    void tick();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [load]);

  async function search() {
    setBusy(true);
    setError(null);
    try {
      setData(await api.robinhoodSearch());
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  }

  const buckets = data?.buckets ?? [];
  const tokens = data?.tokens ?? [];

  return (
    <div className="panel">
      <div className="rh-tokens-head">
        <h3>🆕 Nouveaux tokens</h3>
        <button className="action small" onClick={search} disabled={busy}>
          {busy ? '…' : 'Rechercher'}
        </button>
      </div>

      <div className="rh-sorts">
        {/* Spec §33: ranking by setup quality is not the same list as ranking
            by what already went up, and the two must be nameable. */}
        <button className={sort === 'age' ? 'active' : ''} onClick={() => setSort('age')}>
          🆕 Les plus récents
        </button>
        <button className={sort === 'early' ? 'active' : ''} onClick={() => setSort('early')}>
          🔥 Meilleurs setups
        </button>
        <button className={sort === 'decision' ? 'active' : ''} onClick={() => setSort('decision')}>
          🟢 À acheter
        </button>
      </div>

      {/* What the engine actually asked for, counted. UNDECIDED is shown apart
          from ⚫ : « personne n'a regardé » et « nous refusons » ne sont pas la
          même phrase. */}
      {data?.decisions && (
        <div className="rh-decision-counts">
          {(['BUY', 'WATCH', 'AVOID'] as const).map((a) => (
            <span key={a} className={`rh-dc ${a}`}>
              {DECISION_LABELS[a].emoji} {DECISION_LABELS[a].fr}
              <strong>{data.decisions?.[a] ?? 0}</strong>
            </span>
          ))}
          {(data.decisions.UNDECIDED ?? 0) > 0 && (
            <span className="rh-dc UNDECIDED">
              · non décidés<strong>{data.decisions.UNDECIDED}</strong>
            </span>
          )}
        </div>
      )}

      <div className="rh-buckets">
        <button className={bucket === '' ? 'active' : ''} onClick={() => setBucket('')}>
          Tous
        </button>
        {buckets.map((b) => (
          <button
            key={b.id}
            className={bucket === b.id ? 'active' : ''}
            onClick={() => setBucket(b.id)}
          >
            {b.label_fr}
            <span className="rh-bucket-n">{b.count}</span>
          </button>
        ))}
      </div>

      {error && <div className="error-box">{error}</div>}

      {data?.state === 'PENDING' && <p className="muted">Première recherche en cours…</p>}

      {data?.state === 'FAILED' && (
        <div className="rh-empty bad">
          <strong>Recherche impossible</strong>
          <span>
            L'indexeur n'a pas répondu{data.errors?.length ? ` (${data.errors[0]})` : ''}.
            Ce n'est pas « aucun token » — c'est une source injoignable.
          </span>
        </div>
      )}

      {data?.state === 'EMPTY' && (
        <div className="rh-empty">
          <strong>Aucun token dans cette fenêtre</strong>
          <span>
            La recherche a abouti et n'a rien trouvé d'assez récent ou d'assez liquide.
            La chaîne est peut-être simplement calme.
          </span>
        </div>
      )}

      {tokens.length > 0 && (
        <div className="rh-token-list">
          {tokens.map((t) => <TokenRow key={t.contract_address} t={t} />)}
        </div>
      )}

      {data?.coverage_note && <p className="feed-note">{data.coverage_note}</p>}
    </div>
  );
}

function TokenRow({ t }: { t: RobinhoodToken }) {
  const [open, setOpen] = useState(false);
  const [crossOpen, setCrossOpen] = useState(false);
  const s = t.safety;
  const p = t.presentation;
  return (
    <div className={`rh-token ${t.hard_veto ? 'vetoed' : ''}`}>
      <div className="rh-token-head">
        <span className="rh-sym">{t.symbol ?? '—'}</span>
        <span className="rh-age">{fmtAge(t.pool_age_seconds)}</span>
      </div>

      {/* The decision is the most visible thing on the card, immediately under
          the symbol (spec §6). Safety used to sit here — it still comes before
          any *market figure*, but not before the answer those figures produced:
          the question "why is this not a buy?" has to be answerable before the
          eye reaches a single number. */}
      {t.decision && (
        <div className={`rh-decision ${t.decision.action}`}>
          <DecisionBadge
            action={t.decision.action}
            size="md"
            strength={t.decision.strength_fr}
          />

          {/* The question the card exists to answer, immediately under the
              answer itself (spec §6-7). Every line carries the reading and the
              threshold it was measured against, so the refusal is arguable
              rather than merely stated. */}
          {p && p.why_not_buy.length > 0 && (
            <div className="rh-whynot">
              <strong>Pourquoi pas ACHETER ?</strong>
              <ul>
                {p.why_not_buy.map((r, i) => (
                  <li key={i} className={r.kind}>
                    <span className="rh-whynot-label">{r.label_fr}</span>
                    {r.value_fr && r.minimum_fr ? (
                      <span className="rh-whynot-nums">
                        {r.value_fr} <span className="muted">/ minimum {r.minimum_fr}</span>
                      </span>
                    ) : (
                      <span className="rh-whynot-nums">{r.detail_fr}</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {t.decision.action === 'BUY' && t.decision.reasons.length > 0 && (
            <ul className="rh-decision-why">
              {t.decision.reasons.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          )}
        </div>
      )}

      {/* How much history actually exists. Near the top rather than several
          lines down: on a token four minutes old this is the fact that
          qualifies every other number on the card (spec §4). */}
      {p && (
        <div className={`rh-history ${p.history.very_recent ? 'recent' : ''}`}>
          <span className="k">Historique disponible</span>
          <span className="v">{p.history.available_fr}</span>
          {p.history.warning && <p className="rh-history-warning">⚠ {p.history.warning}</p>}
        </div>
      )}


      {/* Safety comes before any market figure. A token that cannot be sold has
          no interesting volume, and putting the numbers first would invite
          reading them first. Icon + text + colour, never colour alone. */}
      {/* "RUG RISK FAIBLE" on a token where four checks came back empty reads
          as reassurance it has not earned. The headline says what was actually
          established, and the coverage sits beside it (spec §9-10). */}
      {s && p && (
        <button className={`rh-risk ${s.rug_risk} ${p.safety.partial ? 'partial' : ''}`}
                onClick={() => setOpen(!open)}>
          <span aria-hidden="true">{p.safety.emoji}</span>
          <span className="rh-risk-label">{p.safety.headline_fr}</span>
          <span className="rh-risk-score">
            Risque {p.safety.risk_level_fr} · couverture {num(p.safety.coverage_pct ?? 0, 0)}&#8239;%
          </span>
          <span className="rh-risk-more">{open ? '▲' : '▼'}</span>
        </button>
      )}

      {s && open && <SafetyDetail s={s} />}

      {s && p && p.safety.partial && (
        <div className="rh-partial">
          ⚠ SÉCURITÉ PARTIELLEMENT VÉRIFIÉE — {p.safety.unknown_count ?? 0} contrôle(s) inconnu(s).
          <ul>
            {p.safety.unknown_checks.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
          <p className="muted small">
            Un contrôle non effectué n'est jamais lu comme un contrôle réussi.
          </p>
        </div>
      )}

      {/* Too little depth is not a secondary defect: it is the reason an exit
          may not be executable at the price on screen (spec §8). */}
      {p && p.liquidity.severity !== 'ok' && (
        <div className={`rh-liq ${p.liquidity.severity}`}>
          <strong>
            {p.liquidity.severity === 'critical' ? '🚨 ' : '⚠ '}
            {p.liquidity.headline_fr}
          </strong>
          <div className="rh-liq-nums">
            {p.liquidity.display_fr}
            <span className="muted"> / minimum requis {p.liquidity.minimum_fr}</span>
          </div>
          {p.liquidity.consequences.length > 0 && (
            <ul>
              {p.liquidity.consequences.map((c, i) => <li key={i}>{c}</li>)}
            </ul>
          )}
        </div>
      )}

      {/* The instruction, and directly beneath it every reason it rests on.
          The blocking list comes from the *decision*, not from safety alone:
          a purchase can also be refused for a pool too thin to exit, a vetoed
          explosion, or two sources that disagree, and a ⚫ whose reason is
          invisible is indistinguishable from a bug (invariant 73). */}
      {/* Every predictive score with the reliability of the data under it.
          The tone comes from the *weaker* of the two: a 92 measured on two
          minutes of history is a strong number, not a strong signal, and the
          card must not let the first read as the second (spec §2, §11). */}
      {p && (
        <div className="rh-scores">
          <ScoreWithReliability block={p.scores.explosion} icon="🚀" />
          <ScoreWithReliability block={p.scores.early} icon="🌱" />
          <ScorePill
            label="Maturité"
            value={t.maturity ? (t.maturity.known ? t.maturity.label : 'inconnue') : '—'}
            tone={t.maturity?.is_late ? 'late' : 'ok'}
          />
        </div>
      )}

      {/* What is true about the pool rather than about the token: a thin pool
          moves easily *because* it is thin, which is not an opportunity
          (spec §20). */}
      {p?.caveats.map((c, i) => (
        <p className="rh-caveat" key={`cav${i}`}>⚠ {c}</p>
      ))}

      {/* What is absent, counted and nameable. These absences are what lower
          the reliability above; a missing datum is never a favourable one
          (spec §15). */}
      {p && p.missing_data.count > 0 && (
        <details className="rh-missing">
          <summary>Données manquantes : {p.missing_data.count}</summary>
          <ul>
            {p.missing_data.fields.map((f, i) => <li key={i}>{f}</li>)}
          </ul>
          <p className="muted small">{p.missing_data.note}</p>
        </details>
      )}

      {/* Everything below is the expert half: the same figures as before, but
          under the decision rather than above it (spec §17). */}
      <details className="rh-expert">
        <summary>Détail complet</summary>

        {t.early && <EarlyDetail t={t} />}
        <div className="rh-addr" title={t.contract_address}>
          {short(t.contract_address)}
          {t.dex ? <span className="rh-dex">{t.dex}</span> : null}
        </div>
        <div className="rh-token-grid">
        <Cell k="Prix" v={fmtUsd(t.price_usd, 6)} />
        <Cell
          k="Liquidité"
          v={fmtUsd(t.liquidity_usd)}
          // "partielle" qualifies a total assembled from some of the pools. With
          // no value at all there is nothing to qualify, and "— (partielle)"
          // read as though part of a number were showing.
          note={t.liquidity_is_partial && t.liquidity_usd !== null ? 'partielle' : undefined}
        />
        <Cell k="Volume 1 h" v={fmtUsd(t.volume_h1)} />
        <Cell k="Acheteurs 1 h" v={fmtNum(t.buyers_h1)} />
        <Cell k="Achats/ventes" v={fmtNum(t.buy_sell_ratio_h1, 2)} />
        <Cell k="Accélération" v={fmtNum(t.volume_acceleration, 2, '×')} />
        </div>
      </details>

      {/* The cross-check costs a request, so it is opened deliberately rather
          than fetched for every row on every refresh. */}
      <button className="rh-cross-toggle" onClick={() => setCrossOpen(!crossOpen)}>
        {crossOpen ? 'Masquer le recoupement' : 'Recouper avec une 2ᵉ source'}
      </button>
      {crossOpen && (
        <TokenCrossCheck address={t.contract_address} onClose={() => setCrossOpen(false)} />
      )}
    </div>
  );
}

/** A predictive score with the reliability of the data under it.
 *
 * The two are always rendered together (spec §2). The pill's colour comes from
 * `tone`, which the backend derives from the *weaker* of the two: 92/100 on two
 * minutes of history is `caution`, not `strong`. A number never earns green —
 * green belongs to a decision (spec §11-12). */
function ScoreWithReliability({ block, icon }: { block: RhScoreBlock; icon: string }) {
  const r = block.reliability;
  return (
    <div className={`rh-score ${block.tone}`}>
      <div className="rh-score-head">
        <span aria-hidden="true">{icon}</span>
        <span className="rh-score-label">
          {block.label_fr}{block.horizon_fr ? ` ${block.horizon_fr}` : ''}
        </span>
      </div>
      {/* "92/100", never "92 %". There is no calibration behind this number
          and so there is no probability to show (spec §1). */}
      <div className="rh-score-value">{block.display}</div>
      <div className="rh-score-rel">
        <span aria-hidden="true">{r.emoji}</span> Fiabilité&nbsp;: {
          r.score === null ? '—' : `${num(r.score, 0)}/100`
        } — {r.label_fr}
      </div>
      {block.warning && <div className="rh-score-warn">⚠ {block.warning}</div>}
      <div className="rh-score-note">{block.note}</div>
    </div>
  );
}

function ScorePill({ label, value, tone }: { label: string; value: string; tone: string }) {
  return (
    <span className={`rh-pill ${tone}`}>
      <span className="rh-pill-k">{label}</span>
      <span className="rh-pill-v">{value}</span>
    </span>
  );
}

function EarlyDetail({ t }: { t: RobinhoodToken }) {
  const e = t.early!;
  return (
    <div className="rh-early-detail">
      {e.components.map((c) => (
        <div className="rh-comp" key={c.name}>
          <div className="rh-comp-head">
            <span>{c.label_fr}</span>
            <span className={c.unavailable ? 'unknown' : ''}>
              {c.unavailable ? 'indisponible' : `${num(c.points, 1)} / ${c.max_points}`}
            </span>
          </div>
          {c.reasons.map((r, i) => <div className="rh-comp-why" key={`r${i}`}>· {r}</div>)}
          {c.caveats.map((r, i) => <div className="rh-comp-risk" key={`c${i}`}>⚠ {r}</div>)}
        </div>
      ))}

      {t.explosion && (
        <div className="rh-explosion">
          <div className="rh-comp-head">
            <span>Explosion {t.explosion.horizon_minutes} min — {t.explosion.label}</span>
          </div>
          {t.explosion.reasons.map((r, i) => <div className="rh-comp-why" key={`er${i}`}>· {r}</div>)}
          {t.explosion.caveats.map((r, i) => <div className="rh-comp-risk" key={`ec${i}`}>⚠ {r}</div>)}
          <p className="feed-note">{t.explosion.note}</p>
        </div>
      )}

      {t.maturity && !t.maturity.known && (
        <p className="rh-comp-risk">
          ⚠ Maturité inconnue : la valeur affichée est neutre, pas une mesure.
        </p>
      )}
      {t.confidence && t.confidence.missing.length > 0 && (
        <p className="rh-comp-risk">
          ⚠ Données manquantes : {t.confidence.missing.join(' · ')}
        </p>
      )}

      <p className="feed-note">
        {e.engine_version} · empreinte {e.weights_fingerprint}. Un score sur 100 est un
        classement sous des pondérations fixes, jamais une probabilité — et ces
        pondérations sont une hypothèse qu'aucun historique Robinhood n'a encore validée.
      </p>
    </div>
  );
}

function SafetyDetail({ s }: { s: SafetyReport }) {
  return (
    <div className="rh-safety-detail">
      <div className="rh-safety-counts">
        {(['CRITICAL', 'MAJOR', 'MINOR', 'UNKNOWN'] as const).map((k) => (
          <span key={k} className={`rh-count ${k}`}>
            {labelFor(SEVERITY_FR, k)} {s.counts[k] ?? 0}
          </span>
        ))}
      </div>
      {s.findings.length === 0 && <p className="muted small">Aucun signal négatif détecté.</p>}
      {s.findings.map((f) => (
        <div className="rh-finding" key={f.code}>
          <span className={`rh-sev ${f.severity}`}>{labelFor(SEVERITY_FR, f.severity)}</span>
          <span className="rh-finding-label">
            {f.label_fr}{f.detail ? <span className="muted"> — {f.detail}</span> : null}
          </span>
        </div>
      ))}
      <p className="feed-note">
        {s.engine_version} · empreinte {s.weights_fingerprint} ·
        {' '}couverture {num(s.coverage * 100, 0)} % des contrôles.
        {' '}Les pondérations sont une hypothèse, jamais une probabilité.
      </p>
    </div>
  );
}

function Cell({ k, v, note }: { k: string; v: string; note?: string }) {
  return (
    <div className="rh-cell">
      <span className="k">{k}</span>
      <span className={`v ${v === '—' ? 'unknown' : ''}`}>
        {v}
        {note ? <span className="rh-partial"> ({note})</span> : null}
      </span>
    </div>
  );
}

/* Formatters. Every one of them renders null as an em dash and never as 0 —
   the single rule this whole screen exists to respect. */

function fmtAge(s: number | null): string {
  return age(s);
}

/** Dollars, delegating the digits to the shared formatters.
 *
 * The tiny-price branch used `toPrecision(3)`, which writes `0.000420` — found
 * by running the search, where a French screen showed an English decimal on
 * every meme-coin price. `price()` already picks the significance a venue
 * quotes at and applies the comma. */
function fmtUsd(v: number | null, digits = 0): string {
  if (v === null) return '\u2014';
  if (v !== 0 && Math.abs(v) < 0.01) return `${price(v)}\u202f$`;
  if (Math.abs(v) >= 1_000) return `${compact(v)}\u202f$`;
  return `${num(v, digits)}\u202f$`;
}

function fmtNum(v: number | null, digits = 0, suffix = ''): string {
  return v === null ? '—' : `${num(v, digits)}${suffix}`;
}

function short(addr: string): string {
  return addr.length > 14 ? `${addr.slice(0, 8)}…${addr.slice(-6)}` : addr;
}
