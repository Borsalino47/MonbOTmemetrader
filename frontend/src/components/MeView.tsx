import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { DASH, age, num, pct, price, signClass } from '../format';
import type { DecisionsResponse, Position, PositionEvent, TradeSignal } from '../types';
import { DECISION_LABELS, DecisionBadge, DecisionCard } from './DecisionBadge';
import { ResultsView } from './ResultsView';
import { ValidationsView } from './ValidationsView';

type Section = 'positions' | 'decisions' | 'results';

/** Everything that is *yours* rather than the market's: open positions,
 *  recommendations waiting for an answer, and the decisions you have taken.
 *
 * Merged into one tab on purpose. Three separate tabs would push the bottom
 * navigation to nine entries on a 412px screen, and the split between "my
 * positions" and "my decisions" is not one anyone needs to make while holding
 * a phone. */
export function MeView({ onSelect }: { onSelect: (symbol: string) => void }) {
  const [section, setSection] = useState<Section>('positions');
  const [data, setData] = useState<DecisionsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      setData(await api.decisions());
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  async function refreshPositions() {
    setBusy(true);
    try {
      await api.watchPositions();
      await reload();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  }

  if (error) return <div className="error-box">Indisponible : {error}</div>;
  if (!data) return <div className="loading">Chargement…</div>;

  return (
    <div className="me">
      <div className="me-tabs">
        <button
          className={section === 'positions' ? 'active' : ''}
          onClick={() => setSection('positions')}
        >
          💼 Positions{data.positions.length ? ` (${data.positions.length})` : ''}
        </button>
        <button
          className={section === 'decisions' ? 'active' : ''}
          onClick={() => setSection('decisions')}
        >
          📋 Mes choix
        </button>
        <button
          className={section === 'results' ? 'active' : ''}
          onClick={() => setSection('results')}
        >
          📊 Résultats
        </button>
      </div>

      {section === 'decisions' ? (
        <ValidationsView onSelect={onSelect} />
      ) : section === 'results' ? (
        <ResultsView />
      ) : (
        <>
          {data.awaiting_answer.map((s) => (
            <BuyConfirm key={s.id} signal={s} onDone={reload} />
          ))}

          <div className="me-head">
            <h3>Positions ouvertes</h3>
            <button className="home-refresh" onClick={refreshPositions} disabled={busy}>
              {busy ? 'Analyse…' : 'Réanalyser'}
            </button>
          </div>

          {data.positions.length === 0 ? (
            <div className="empty">
              Aucune position ouverte. Quand l'application affichera 🟢 ACHETER et que vous
              aurez acheté vous-même, répondez « oui » et la position apparaîtra ici,
              surveillée toutes les 15 secondes.
            </div>
          ) : (
            <div className="pos-list">
              {data.positions.map((p) => (
                <PositionCard key={p.id} position={p} onSelect={onSelect} onChanged={reload} />
              ))}
            </div>
          )}

          <p className="me-note">
            Aucun ordre n'est jamais passé automatiquement. L'application analyse et
            recommande ; vous achetez et vendez vous-même, puis vous confirmez ici — c'est
            cette confirmation qui rend vos résultats mesurables.
          </p>
        </>
      )}
    </div>
  );
}

/** "Avez-vous acheté ?" — the question that turns a recommendation into data.
 *
 * NON is a first-class answer, not a dismissal: a refused signal is followed to
 * its outcome exactly like an accepted one, which is the only way to ever learn
 * whether the hesitation was right. */
function BuyConfirm({ signal, onDone }: { signal: TradeSignal; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [fill, setFill] = useState('');
  const [amount, setAmount] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function answer(taken: boolean) {
    setBusy(true);
    setError(null);
    try {
      if (taken) {
        await api.openPosition({
          symbol: signal.symbol,
          signal_id: signal.id,
          actual_entry_price: fill ? Number(fill) : null,
          amount_invested: amount ? Number(amount) : null,
        });
      } else {
        await api.answerSignal(signal.id, false);
      }
      onDone();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <DecisionCard
      action={signal.action === 'BUY' ? 'BUY' : 'SELL'}
      symbol={signal.symbol}
      price={price(signal.price)}
      strength={signal.strength === 'VERY_STRONG' ? 'TRÈS FORTE'
        : signal.strength === 'STRONG' ? 'FORTE'
        : signal.strength ? 'CORRECTE' : null}
      reasons={signal.reasons}
      risks={signal.risks}
      trigger={signal.trigger_price ? price(signal.trigger_price) : null}
      invalidation={signal.invalidation_price ? price(signal.invalidation_price) : null}
      extra={
        <div className="dec-scores">
          <Score k="Opportunité" v={signal.opportunity_score} />
          <Score k="Explosion 15m" v={signal.explosion_score} />
          <Score k="Sécurité" v={signal.safety_score} />
          <Score k="Confiance" v={signal.data_confidence} />
          <Score k="Maturité" v={signal.pump_maturity} />
        </div>
      }
    >
      <div className="confirm">
        <div className="confirm-q">AVEZ-VOUS ACHETÉ ?</div>

        {open && (
          <div className="confirm-fields">
            <input
              inputMode="decimal" value={fill} onChange={(e) => setFill(e.target.value)}
              placeholder="Prix réel payé (facultatif)"
            />
            <input
              inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)}
              placeholder="Montant investi (facultatif)"
            />
          </div>
        )}

        <div className="confirm-actions">
          <button className="confirm-yes" onClick={() => answer(true)} disabled={busy}>
            {busy ? '…' : 'OUI'}
          </button>
          <button className="confirm-no" onClick={() => answer(false)} disabled={busy}>
            NON
          </button>
          {!open && (
            <button className="confirm-detail" onClick={() => setOpen(true)}>
              + détails
            </button>
          )}
        </div>

        {error && <div className="error-box">{error}</div>}
        <p className="confirm-help">
          « NON » est enregistré aussi, et le signal reste suivi : c'est la seule
          façon de savoir plus tard si l'hésitation avait raison.
        </p>
      </div>
    </DecisionCard>
  );
}

function PositionCard({
  position, onSelect, onChanged,
}: { position: Position; onSelect: (s: string) => void; onChanged: () => void }) {
  const [events, setEvents] = useState<PositionEvent[] | null>(null);
  const [closing, setClosing] = useState(false);
  const [exit, setExit] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const action = position.current_decision ?? 'HOLD';
  const urgent = action === 'SELL' || action === 'REDUCE';

  async function loadEvents() {
    if (events) { setEvents(null); return; }
    try {
      setEvents((await api.positionEvents(position.id)).events);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }

  async function close() {
    setBusy(true);
    setError(null);
    try {
      await api.closePosition(position.id, {
        actual_exit_price: exit ? Number(exit) : null,
        reason: action === 'SELL' ? 'signal VENDRE confirmé' : 'clôture manuelle',
      });
      onChanged();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`pos-card ${action} ${urgent ? 'urgent' : ''}`}>
      <div className="pos-head">
        <DecisionBadge action={action} size="md" />
        <button className="pos-sym" onClick={() => onSelect(position.symbol)}>
          {position.symbol}
        </button>
      </div>

      <div className="pos-grid">
        <Cell k="Entrée" v={price(position.actual_entry_price ?? position.entry_price)} />
        <Cell k="Actuel" v={price(position.last_price)} />
        <Cell k="PnL" v={pct(position.pnl_pct)} cls={signClass(position.pnl_pct)} />
        <Cell k="Sommet" v={price(position.peak_price)} />
        <Cell
          k="Repli sommet"
          v={pct(position.drawdown_from_peak_pct)}
          cls={signClass(position.drawdown_from_peak_pct)}
        />
        <Cell k="Santé" v={position.health_score === null ? DASH : `${num(position.health_score, 0)}/100`} />
        <Cell k="Gain max" v={pct(position.mfe_pct)} cls="pos" />
        <Cell k="Perte max" v={pct(position.mae_pct)} cls="neg" />
        <Cell k="Depuis" v={age((Date.now() - position.opened_ms) / 1000)} />
      </div>

      {position.invalidation_price !== null && (
        <div className="pos-inval">
          Invalidation : <strong>{price(position.invalidation_price)}</strong>
          {' — '}
          {position.last_price !== null && position.last_price <= position.invalidation_price
            ? 'FRANCHIE, la raison de l\'achat n\'existe plus'
            : 'non atteinte'}
        </div>
      )}

      {position.pnl_basis === 'observed_price' && (
        <p className="pos-basis">
          Rendement calculé sur le prix observé par le scanner : vous n'avez pas
          enregistré votre prix d'achat réel.
        </p>
      )}

      <div className="pos-actions">
        <button className="pos-events" onClick={loadEvents}>
          {events ? 'Masquer l\'historique' : 'Historique des décisions'}
        </button>
        <button className="pos-close" onClick={() => setClosing(!closing)}>
          J'ai vendu
        </button>
      </div>

      {closing && (
        <div className="confirm">
          <div className="confirm-q">AVEZ-VOUS VENDU ?</div>
          <div className="confirm-fields">
            <input
              inputMode="decimal" value={exit} onChange={(e) => setExit(e.target.value)}
              placeholder="Prix réel de vente (facultatif)"
            />
          </div>
          <div className="confirm-actions">
            <button className="confirm-yes" onClick={close} disabled={busy}>
              {busy ? '…' : 'OUI, clôturer'}
            </button>
            <button className="confirm-no" onClick={() => setClosing(false)}>ANNULER</button>
          </div>
          <p className="confirm-help">
            Sans prix indiqué, le prix observé maintenant est utilisé — et la fiche
            précise lequel des deux a servi au calcul.
          </p>
        </div>
      )}

      {events && (
        <div className="pos-timeline">
          {events.length === 0 ? (
            <p className="pos-basis">Aucun changement de décision enregistré pour l'instant.</p>
          ) : events.map((e) => (
            <div className="pos-event" key={e.id}>
              <span className="pos-event-when">
                {e.at ? new Date(e.at).toLocaleTimeString('fr-FR', {
                  hour: '2-digit', minute: '2-digit',
                }) : DASH}
              </span>
              <span className="pos-event-dec">
                {DECISION_LABELS[e.decision].emoji} {DECISION_LABELS[e.decision].fr}
              </span>
              <span className="pos-event-px">{price(e.price)}</span>
            </div>
          ))}
        </div>
      )}

      {error && <div className="error-box">{error}</div>}
    </div>
  );
}

function Cell({ k, v, cls }: { k: string; v: string; cls?: string }) {
  return (
    <div className="pos-cell">
      <span className="k">{k}</span>
      <span className={`v ${cls ?? ''}`}>{v}</span>
    </div>
  );
}

function Score({ k, v }: { k: string; v: number | null }) {
  return (
    <div className="dec-score">
      <span className="k">{k}</span>
      <span className="v">{v === null ? DASH : `${num(v, 0)}/100`}</span>
    </div>
  );
}
