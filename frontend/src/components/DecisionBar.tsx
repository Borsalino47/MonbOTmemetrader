import { useEffect, useState } from 'react';
import { api } from '../api';
import type { AssetDetail, Decision, Validation } from '../types';

/** The four decisions, and the record they leave behind.
 *
 * This is the only place in the product where the *user* produces data rather
 * than reading it. Everything else measures the scanner; these rows are what
 * will eventually make it possible to ask whether the person using it does
 * better than the scanner does — a question no amount of internal scoring can
 * answer on its own.
 *
 * What is sent is what is on this screen: price, all three scores, the verdict,
 * the reasons and the invalidation, as rendered. The server fills only what the
 * client left blank. A decision analysed a month from now must be readable in
 * the words that produced it, not in whatever a reweighted engine would say
 * today.
 *
 * Recording a decision is deliberately not undoable. Changing one's mind adds a
 * second decision, because the sequence is the interesting part and an undo
 * button would quietly delete it. */
export function DecisionBar({ data, symbol }: { data: AssetDetail | null; symbol: string }) {
  const [current, setCurrent] = useState<Validation | null>(null);
  const [history, setHistory] = useState<Validation[]>([]);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setCurrent(null);
    setHistory([]);
    setNote('');
    setError(null);
    api.validations(200)
      .then((r) => {
        if (cancelled) return;
        const mine = r.validations.filter((v) => v.symbol === symbol);
        setHistory(mine);
        setCurrent(mine[0] ?? null);
      })
      .catch(() => { /* an unreachable history must not block a decision */ });
    return () => { cancelled = true; };
  }, [symbol]);

  async function decide(decision: Decision) {
    setBusy(decision);
    setError(null);
    try {
      const row = await api.validate({
        symbol,
        decision,
        note: note.trim() || null,
        // Everything below is what this screen was showing. Sent explicitly so
        // the record matches the render even if a scan lands in between.
        signal_timestamp_ms: data?.timestamp_ms ?? null,
        price: data?.price ?? null,
        final_score: data?.final_score ?? null,
        explosion_score: data?.explosion?.explosion_score ?? null,
        pump_maturity: data?.pump_maturity.score ?? null,
        data_confidence: data?.data_confidence.score ?? null,
        setup_state: data?.setup.state ?? null,
        verdict_level: data?.verdict?.level ?? null,
        engine_version: data?.engine_version ?? null,
        why: data?.why_this_asset?.slice(0, 12) ?? null,
        risks: data?.what_can_invalidate_it?.filter(Boolean).slice(0, 12) ?? null,
        trigger: data?.setup.trigger ?? null,
        invalidation: data?.what_can_invalidate_it?.[0] ?? null,
      });
      setCurrent(row);
      setHistory((h) => [row, ...h]);
      setNote('');
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="panel decision-panel">
      <h3>Ma décision</h3>

      {current && (
        <div className={`decision-current ${current.decision}`}>
          <span className="decision-current-label">{LABELS[current.decision]}</span>
          <span className="decision-current-when">
            {current.decided_at ? new Date(current.decided_at).toLocaleString('fr-FR') : ''}
          </span>
        </div>
      )}

      <div className="decision-grid">
        {(Object.keys(LABELS) as Decision[]).map((d) => (
          <button
            key={d}
            className={`decision-btn ${d} ${current?.decision === d ? 'active' : ''}`}
            onClick={() => decide(d)}
            disabled={busy !== null}
          >
            {busy === d ? '…' : LABELS[d]}
          </button>
        ))}
      </div>

      <input
        className="decision-note"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Note (facultatif) — pourquoi cette décision ?"
        maxLength={400}
      />

      {error && <div className="error-box">Décision non enregistrée : {error}</div>}

      <p className="decision-help">
        La décision est enregistrée avec ce qui est affiché maintenant : prix, scores,
        raisons et invalidation. Changer d'avis ajoute une décision, cela n'efface pas
        la précédente — c'est la suite des décisions qui est intéressante.
      </p>

      {history.length > 1 && (
        <div className="decision-history">
          {history.slice(1, 6).map((v) => (
            <div className="decision-hist-row" key={v.id}>
              <span className={`decision-dot ${v.decision}`} />
              <span className="decision-hist-label">{LABELS[v.decision]}</span>
              <span className="decision-hist-meta">
                {v.decided_at ? new Date(v.decided_at).toLocaleDateString('fr-FR') : ''}
                {v.final_score !== null && ` · ${v.final_score.toFixed(0)}/100`}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const LABELS: Record<Decision, string> = {
  VALIDATED: '✅ Valider',
  WATCHLIST: '⭐ Surveiller',
  ANALYSE: '🔬 Analyser',
  REJECTED: '❌ Rejeter',
};
