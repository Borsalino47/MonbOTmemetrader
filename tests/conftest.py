from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.types import Candle, OHLCVSeries, Provenance, Timeframe

FIXED_NOW_MS = 1_750_000_000_000  # deterministic reference instant


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(FIXED_NOW_MS)


@pytest.fixture
def settings() -> CryptoPulseSettings:
    s = CryptoPulseSettings()
    s.providers.market_data = "fixture"
    s.database.url = "sqlite:///:memory:"
    return s


def make_series(
    closes,
    *,
    symbol: str = "TESTUSDT",
    timeframe: Timeframe = Timeframe.M5,
    volumes=None,
    end_time_ms: int = FIXED_NOW_MS,
    last_is_open: bool = False,
    highs=None,
    lows=None,
) -> OHLCVSeries:
    """Build a series from a close list, with plausible OHLC around each close."""
    closes = np.asarray(closes, dtype=np.float64)
    n = closes.size
    tf_ms = timeframe.ms
    last_open = (end_time_ms // tf_ms) * tf_ms
    opens = np.concatenate([[closes[0]], closes[:-1]])
    if highs is None:
        highs = np.maximum(opens, closes) * 1.001
    if lows is None:
        lows = np.minimum(opens, closes) * 0.999
    if volumes is None:
        volumes = np.full(n, 1000.0)
    volumes = np.asarray(volumes, dtype=np.float64)

    candles = []
    for i in range(n):
        ot = last_open - (n - 1 - i) * tf_ms
        candles.append(
            Candle(
                open_time_ms=ot,
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=float(volumes[i]),
                close_time_ms=ot + tf_ms - 1,
                quote_volume=float(volumes[i] * closes[i]),
                trades=100,
                is_closed=not (last_is_open and i == n - 1),
            )
        )
    return OHLCVSeries.from_candles(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        provenance=Provenance(source="test", as_of_ms=candles[-1].close_time_ms, fetched_at_ms=end_time_ms),
    )


@pytest.fixture
def series_factory():
    return make_series
