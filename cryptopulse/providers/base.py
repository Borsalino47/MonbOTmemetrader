"""Provider interfaces.

Swapping Binance for another exchange means writing one class against
`MarketDataProvider`. Nothing above this layer knows the name of an exchange.

`health()` is part of the contract because the dashboard has to show API status
honestly — a provider that is down must be able to say so rather than having the
scanner infer it from an exception.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from cryptopulse.core.types import (
    DexPair,
    OHLCVSeries,
    OrderBook,
    SymbolInfo,
    Ticker24h,
    Timeframe,
)

__all__ = [
    "ProviderHealth",
    "MarketDataProvider",
    "TokenDiscoveryProvider",
    "OrderBookProvider",
    "DEXProvider",
    "OnChainProvider",
    "SocialDataProvider",
]


@dataclass(slots=True)
class ProviderHealth:
    name: str
    available: bool
    latency_ms: float | None = None
    detail: str | None = None
    checked_at_ms: int | None = None
    rate_limit_remaining_pct: float | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "available": self.available,
            "status": "OK" if self.available else "SOURCE_UNAVAILABLE",
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 1),
            "detail": self.detail,
            "checked_at_ms": self.checked_at_ms,
            "rate_limit_remaining_pct": self.rate_limit_remaining_pct,
        }


class MarketDataProvider(ABC):
    """OHLCV, tickers and the tradable universe."""

    name: str
    # A symbol that is certain to exist on this venue, used by `doctor` for its
    # round-trip. Venues disagree on naming (BTCUSDT vs XBTUSDT), so each
    # provider names its own rather than the caller guessing.
    reference_symbol: str = "BTCUSDT"

    @abstractmethod
    async def health(self) -> ProviderHealth: ...

    @abstractmethod
    async def list_symbols(self, quote_asset: str) -> list[SymbolInfo]:
        """Tradable symbols for a quote asset. Raises SourceUnavailable on failure."""

    @abstractmethod
    async def get_tickers_24h(self, symbols: Sequence[str] | None = None) -> dict[str, Ticker24h]:
        """24h rolling statistics. `None` means "everything the venue lists"."""

    @abstractmethod
    async def get_ohlcv(self, symbol: str, timeframe: Timeframe, limit: int) -> OHLCVSeries:
        """Most recent `limit` candles, oldest first.

        Implementations must set `last_is_open` correctly — the caller relies on
        it to avoid scoring a candle that is still forming.
        """

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class OrderBookProvider(ABC):
    name: str

    @abstractmethod
    async def get_order_book(self, symbol: str, depth: int = 100) -> OrderBook: ...


class DEXProvider(ABC):
    """Phase 2. Declared now so the scanner interface is stable."""

    name: str

    @abstractmethod
    async def search_pairs(self, query: str) -> list[DexPair]: ...

    @abstractmethod
    async def get_pair(self, chain: str, pair_address: str) -> DexPair: ...

    @abstractmethod
    async def list_new_pairs(self, chain: str, max_age_hours: float) -> list[DexPair]: ...


class OnChainProvider(ABC):
    """Phase 2: holder concentration, contract checks, liquidity locks."""

    name: str

    @abstractmethod
    async def token_risk(self, chain: str, token_address: str) -> dict: ...


class SocialDataProvider(ABC):
    """Phase 3. No implementation is planned until the deterministic core is validated."""

    name: str

    @abstractmethod
    async def mentions(self, symbol: str, window_hours: int) -> dict: ...


class TokenDiscoveryProvider(ABC):
    """A source that can enumerate a whole venue cheaply, for the Token Hunter.

    Separate from `MarketDataProvider` on purpose. That interface is about one
    symbol at a time and its price history; this one is about *breadth* — every
    tradeable asset a venue knows, in as few requests as possible, carrying only
    the fields a wide pre-scan can afford.

    A CEX satisfies this with its 24h ticker endpoint. A chain indexer will
    satisfy it with a pools listing, which is why the return type is a plain
    mapping rather than anything exchange-shaped: the Robinhood Chain and DEX
    sources planned for later must be able to implement it without either side
    learning about the other.
    """

    name: str

    @abstractmethod
    async def discover_universe(self, quote_asset: str) -> dict[str, Ticker24h]:
        """Every tradeable pair the source knows, keyed by symbol.

        Implementations must spend as few requests as possible — this is called
        on every cycle, and its cheapness is what makes a wide search viable.
        Raises `SourceUnavailable` on failure rather than returning a partial
        universe, because a silently truncated universe looks exactly like a
        quiet market.
        """
