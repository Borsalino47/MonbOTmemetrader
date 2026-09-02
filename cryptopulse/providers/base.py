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
    AssetValuation,
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
    "OrderBookProvider",
    "ValuationProvider",
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


class ValuationProvider(ABC):
    """Supply-side facts an exchange cannot supply: market cap, FDV, supply.

    Kept separate from `MarketDataProvider` because it is a different kind of
    source with a different failure mode. A venue being down stops a scan; a
    valuation source being down only means the capacity reading is unknown, and
    the scanner is required to carry on and say so.
    """

    name: str

    @abstractmethod
    async def health(self) -> ProviderHealth: ...

    @abstractmethod
    async def get_valuations(self, base_assets: Sequence[str]) -> dict[str, AssetValuation]:
        """Valuations keyed by BASE asset (BTC, not BTCUSDT).

        An asset the source does not rank must still appear in the result when a
        bound on its cap is known — "smaller than everything ranked" is a fact,
        not a gap. Assets about which nothing at all is known are omitted.
        """

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


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
