"""Candle cache — stop re-downloading bars that cannot have changed.

THE WASTE THIS REMOVES

The scanner loops every 60 seconds and rebuilds every asset's features from
scratch, which means it re-fetches every timeframe on every pass. For the
intraday timeframes that is roughly right. For the daily it is absurd: 400 daily
candles per asset, every minute, for a series whose newest bar changes once a
day. Measured on the Robinhood universe, the daily alone was a third of all
requests and none of it bought a single new number.

THE RULE, AND WHY IT IS SAFE

A series fetched at time `t` contains every bar that had closed by `t`. Those
bars are immutable — a closed candle never changes. The only thing that can make
the answer different is the *next* bar closing, so the entry is valid until
exactly that moment:

    valid_until = (t // timeframe_ms + 1) * timeframe_ms

Within that window the cache returns the identical closed history the provider
would have returned. The one part that does go stale is the still-forming candle,
and every consumer in this project drops it (`OHLCVSeries.closed()` is called at
the single entry point of the feature pipeline) — which is why this is a cache
and not a correctness hazard.

WHAT IS DELIBERATELY NOT CACHED

* **Order books.** A depth snapshot is a statement about right now; serving a
  60-second-old one as current would be a lie about the most time-sensitive
  number in the system.
* **24h tickers.** One request covers the whole venue, so there is nothing to
  save, and they are the freshest input the scanner has.
* **`doctor`.** It exists to prove the live API behaves as expected; answering it
  from a cache would prove nothing. It builds an uncached provider explicitly.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import OHLCVSeries, OrderBook, SymbolInfo, Ticker24h, Timeframe
from cryptopulse.providers.base import MarketDataProvider, OrderBookProvider, ProviderHealth

log = get_logger("providers.cache")

__all__ = ["CachingMarketDataProvider", "CacheStats", "wrap_with_cache"]


@dataclass(slots=True)
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    entries: int = 0

    @property
    def hit_rate(self) -> float | None:
        total = self.hits + self.misses
        return self.hits / total if total else None

    def to_dict(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "entries": self.entries,
            "hit_rate": None if self.hit_rate is None else round(self.hit_rate, 4),
        }


@dataclass(slots=True)
class _Entry:
    series: OHLCVSeries
    requested_limit: int
    valid_until_ms: int


class CachingMarketDataProvider(MarketDataProvider):
    """Wraps a provider and serves candle requests that cannot have changed."""

    def __init__(
        self,
        inner: MarketDataProvider,
        *,
        clock: Clock = SYSTEM_CLOCK,
        max_entries: int = 2000,
        enabled: bool = True,
    ) -> None:
        self.inner = inner
        self.clock = clock
        self.max_entries = max(1, max_entries)
        self.enabled = enabled
        self.stats = CacheStats()
        self._entries: OrderedDict[tuple[str, Timeframe], _Entry] = OrderedDict()

    # The wrapper must be indistinguishable from what it wraps: `is_synthetic`
    # reads `.name`, and `doctor` reads `.reference_symbol`.
    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def reference_symbol(self) -> str:
        return getattr(self.inner, "reference_symbol", "BTCUSDT")

    async def health(self) -> ProviderHealth:
        return await self.inner.health()

    async def list_symbols(self, quote_asset: str) -> list[SymbolInfo]:
        return await self.inner.list_symbols(quote_asset)

    async def get_tickers_24h(self, symbols: Sequence[str] | None = None) -> dict[str, Ticker24h]:
        # Never cached: one call covers the venue, and it is the freshest thing
        # the scanner reads.
        return await self.inner.get_tickers_24h(symbols)

    async def close(self) -> None:
        self._entries.clear()
        await self.inner.close()

    # -- the cache ----------------------------------------------------------- #

    def _valid_until(self, now_ms: int, timeframe: Timeframe) -> int:
        """The instant the next bar of this timeframe closes."""
        tf_ms = timeframe.ms
        return (now_ms // tf_ms + 1) * tf_ms

    async def get_ohlcv(self, symbol: str, timeframe: Timeframe, limit: int) -> OHLCVSeries:
        if not self.enabled:
            return await self.inner.get_ohlcv(symbol, timeframe, limit)

        key = (symbol.upper(), timeframe)
        now_ms = self.clock.now_ms()
        entry = self._entries.get(key)

        # A cached entry serves a request only when it is still inside its bar
        # *and* it was fetched with at least as deep a window as this caller
        # wants. A shallower entry is a miss, never a silently truncated answer.
        if entry is not None and now_ms < entry.valid_until_ms and entry.requested_limit >= limit:
            self._entries.move_to_end(key)
            self.stats.hits += 1
            return self._tail(entry.series, limit)

        series = await self.inner.get_ohlcv(symbol, timeframe, limit)
        self.stats.misses += 1
        self._entries[key] = _Entry(
            series=series,
            requested_limit=limit,
            valid_until_ms=self._valid_until(now_ms, timeframe),
        )
        self._entries.move_to_end(key)
        self._evict()
        self.stats.entries = len(self._entries)
        return series

    @staticmethod
    def _tail(series: OHLCVSeries, limit: int) -> OHLCVSeries:
        """The last `limit` candles. `slice(start, None)` keeps `last_is_open`."""
        n = len(series)
        if n <= limit:
            return series
        return series.slice(n - limit, None)

    def _evict(self) -> None:
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)  # oldest use first
            self.stats.evictions += 1

    def invalidate(self, symbol: str | None = None) -> int:
        """Drop cached candles. Used by tests and by anything that must re-read."""
        if symbol is None:
            dropped = len(self._entries)
            self._entries.clear()
        else:
            wanted = symbol.upper()
            keys = [k for k in self._entries if k[0] == wanted]
            for k in keys:
                del self._entries[k]
            dropped = len(keys)
        self.stats.entries = len(self._entries)
        return dropped

    def cache_stats(self) -> dict:
        self.stats.entries = len(self._entries)
        return self.stats.to_dict()


class CachingMarketDataAndBookProvider(CachingMarketDataProvider, OrderBookProvider):
    """The same wrapper for a venue that also serves depth.

    Order books are forwarded untouched. The class exists so that
    `isinstance(provider, OrderBookProvider)` still answers truthfully about the
    venue underneath — the scanner uses it to decide whether to spend a pass on
    depth at all.
    """

    async def get_order_book(self, symbol: str, depth: int = 100) -> OrderBook:
        return await self.inner.get_order_book(symbol, depth)


def wrap_with_cache(
    provider: MarketDataProvider, *, clock: Clock = SYSTEM_CLOCK, max_entries: int = 2000, enabled: bool = True
) -> MarketDataProvider:
    """Wrap a provider, preserving whether it can serve order books."""
    if isinstance(provider, OrderBookProvider):
        return CachingMarketDataAndBookProvider(
            provider, clock=clock, max_entries=max_entries, enabled=enabled
        )
    return CachingMarketDataProvider(provider, clock=clock, max_entries=max_entries, enabled=enabled)
