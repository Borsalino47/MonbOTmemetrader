import type { MarketId, ProviderSummary } from '../types';

/** [ 🟡 BINANCE ] [ 🟣 ROBINHOOD ] — the primary navigation (spec §3, §48).
 *
 * This is not a filter and not a tab: it changes which universe the whole
 * screen is about. It therefore sits above the tab bar, is the largest control
 * in the header, and switches instantly — the state it needs is already in
 * memory, so no request stands between the tap and the new screen.
 *
 * Each button carries its own live state underneath. The two markets are
 * verified independently, and showing one badge for both would be the exact
 * mistake spec §6 forbids: a green Binance saying nothing about the chain. */
export function MarketSwitch({
  market, onChange, providers,
}: {
  market: MarketId;
  onChange: (m: MarketId) => void;
  providers: ProviderSummary[];
}) {
  return (
    <div className="market-switch" role="tablist" aria-label="Marché">
      {providers.map((p) => {
        const active = p.id === market;
        return (
          <button
            key={p.id}
            role="tab"
            aria-selected={active}
            className={`market-btn ${active ? 'active' : ''} ${p.id}`}
            onClick={() => onChange(p.id)}
          >
            <span className="market-top">
              {/* Emoji + text + state, never colour alone: on a phone in
                  sunlight the tint is the first thing to disappear. */}
              <span className="market-emoji" aria-hidden="true">{p.emoji}</span>
              <span className="market-label">{p.label}</span>
            </span>
            <span className="market-state">
              <span aria-hidden="true">{p.state_emoji}</span>{' '}
              {shortState(p)}
            </span>
          </button>
        );
      })}
    </div>
  );
}

/** The header has room for a word, not a sentence. The full label lives in the
 *  badge on the screen below, so nothing is lost — only shortened. */
function shortState(p: ProviderSummary): string {
  if (p.state === 'VERIFIED') return 'vérifié';
  if (p.state === 'PARTIAL') return 'partiel';
  if (p.state === 'PENDING') return 'vérification…';
  if (p.state === 'SKIPPED_SYNTHETIC') return 'démo';
  return 'non vérifié';
}
