import type { FeedVerification } from '../types';

/** 🟡 / 🟢 / 🔴 — has the live feed been proven live?
 *
 * Three states rather than two, because "not yet checked" and "checked and
 * wrong" are different facts and collapsing them would make every launch look
 * broken for the first few seconds.
 *
 * Shown separately from the DEMO banner. They answer different questions: DEMO
 * says the numbers are generated, this says whether a real feed has been
 * verified. A synthetic run is legitimately both — generated *and* with nothing
 * to verify — so this badge stays out of the way in that case. */
export function FeedBadge({
  feed, onRetry, busy,
}: { feed: FeedVerification; onRetry?: () => void; busy?: boolean }) {
  if (feed.state === 'SKIPPED_SYNTHETIC') return null;

  return (
    <div className={`feed-badge ${feed.state}`}>
      <span className="feed-emoji" aria-hidden="true">{feed.emoji}</span>
      <span className="feed-body">
        <span className="feed-label">{feed.label_fr}</span>
        {feed.state !== 'PENDING' && (
          <span className="feed-detail">
            {feed.passed}/{feed.total} contrôles · {feed.provider}
            {feed.error ? ` · ${feed.error}` : ''}
          </span>
        )}
      </span>
      {feed.state === 'FAILED' && onRetry && (
        <button className="feed-retry" onClick={onRetry} disabled={busy}>
          {busy ? '…' : 'Réessayer'}
        </button>
      )}
    </div>
  );
}

/** The full panel: which checks passed, and what a failure means for the screen. */
export function FeedPanel({ feed }: { feed: FeedVerification }) {
  return (
    <div className="panel">
      <h3>Vérification du flux</h3>
      <div className={`feed-badge ${feed.state}`} style={{ marginBottom: 10 }}>
        <span className="feed-emoji">{feed.emoji}</span>
        <span className="feed-body"><span className="feed-label">{feed.label_fr}</span></span>
      </div>

      {feed.checks.map((c, i) => (
        <div className="kv" key={i}>
          <span className="k">{c.ok ? '✓' : '✗'} {c.name}</span>
          <span className="v">{c.detail || (c.ok ? 'ok' : '—')}</span>
        </div>
      ))}

      <p className="feed-note">{feed.note}</p>
    </div>
  );
}
