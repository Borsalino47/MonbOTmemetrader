"""Provider selection. One switch, one place.

Changing `CP_PROVIDER_MARKET_DATA` in .env swaps the whole data source without
touching a line of scanner code.
"""

from __future__ import annotations

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.providers.base import MarketDataProvider, OrderBookProvider

__all__ = ["build_market_provider", "is_synthetic"]


def build_market_provider(
    settings: CryptoPulseSettings, clock: Clock = SYSTEM_CLOCK
) -> MarketDataProvider | OrderBookProvider:
    choice = settings.providers.market_data
    if choice == "binance":
        from cryptopulse.providers.binance import BinanceSpotProvider

        return BinanceSpotProvider(settings.providers, clock=clock)
    if choice == "fixture":
        from cryptopulse.providers.fixture import FixtureProvider

        return FixtureProvider(clock=clock)
    raise ValueError(f"unknown market data provider {choice!r}")


def is_synthetic(provider) -> bool:
    """True when the running provider emits generated data rather than market data."""
    from cryptopulse.providers.fixture import SYNTHETIC_SOURCE

    return getattr(provider, "name", "") == SYNTHETIC_SOURCE
