"""Provider selection. One switch, one place.

Changing `CP_PROVIDER_MARKET_DATA` in .env swaps the whole data source without
touching a line of scanner code.
"""

from __future__ import annotations

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.providers.base import MarketDataProvider, OrderBookProvider, ValuationProvider

__all__ = ["build_market_provider", "build_valuation_provider", "is_synthetic"]


def build_market_provider(
    settings: CryptoPulseSettings, clock: Clock = SYSTEM_CLOCK, *, use_cache: bool = True
) -> MarketDataProvider | OrderBookProvider:
    """The configured venue, wrapped in the candle cache unless told otherwise.

    `use_cache=False` exists for `doctor`, whose whole job is to prove the live
    API behaves as expected — an answer served from a cache would prove nothing.
    """
    provider = _build_raw(settings, clock)
    if not use_cache or not settings.providers.cache_enabled:
        return provider
    from cryptopulse.providers.cache import wrap_with_cache

    return wrap_with_cache(
        provider, clock=clock, max_entries=settings.providers.cache_max_entries, enabled=True
    )


def _build_raw(
    settings: CryptoPulseSettings, clock: Clock
) -> MarketDataProvider | OrderBookProvider:
    choice = settings.providers.market_data
    if choice == "binance":
        from cryptopulse.providers.binance import BinanceSpotProvider

        return BinanceSpotProvider(settings.providers, clock=clock)
    if choice == "kraken":
        from cryptopulse.providers.kraken import KrakenProvider

        return KrakenProvider(settings.providers, clock=clock)
    if choice == "fixture":
        from cryptopulse.providers.fixture import FixtureProvider

        return FixtureProvider(clock=clock)
    raise ValueError(f"unknown market data provider {choice!r}")


def build_valuation_provider(
    settings: CryptoPulseSettings, clock: Clock = SYSTEM_CLOCK
) -> ValuationProvider | None:
    """The market-cap source, or None.

    None is a supported state, not a failure: the moonshot layer reports an
    unknown market cap as unknown and renormalises around it.
    """
    choice = settings.providers.valuation
    if choice == "none":
        return None
    if choice == "coingecko":
        from cryptopulse.providers.coingecko import CoinGeckoValuationProvider

        return CoinGeckoValuationProvider(settings.providers, clock=clock)
    raise ValueError(f"unknown valuation provider {choice!r}")


def is_synthetic(provider) -> bool:
    """True when the running provider emits generated data rather than market data."""
    from cryptopulse.providers.fixture import SYNTHETIC_SOURCE

    return getattr(provider, "name", "") == SYNTHETIC_SOURCE
