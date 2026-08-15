"""Offline provider producing **synthetic** candles.

WHAT THIS IS: a deterministic generator used by the test suite and by
`--provider fixture` for developing the UI without burning API quota (or, as in
the sandbox this was built in, without any egress at all).

WHAT THIS IS NOT: market data. Every series it emits is a seeded random walk with
a scripted scenario laid over it. Nothing here is a real price, a real volume, or
evidence that any strategy works.

Two guards make it impossible to mistake this for the real thing:

* `name = "SYNTHETIC-FIXTURE"` and every `Provenance.source` carries it, so the
  string appears in every API response and on the dashboard.
* `Provenance.quality` is always `DataQuality.PARTIAL` with the note
  `SYNTHETIC_DATA_NOT_REAL_MARKET`, which the confidence scorer penalises and
  the UI renders as a warning banner.

A backtest run against this provider is a test of the plumbing, never a result.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.errors import DataUnavailable
from cryptopulse.core.types import (
    Candle,
    DataQuality,
    OHLCVSeries,
    OrderBook,
    OrderBookLevel,
    Provenance,
    SymbolInfo,
    Ticker24h,
    Timeframe,
)
from cryptopulse.providers.base import MarketDataProvider, OrderBookProvider, ProviderHealth

__all__ = ["FixtureProvider", "Scenario", "SYNTHETIC_SOURCE", "generate_series"]

SYNTHETIC_SOURCE = "SYNTHETIC-FIXTURE"
SYNTHETIC_NOTE = "SYNTHETIC_DATA_NOT_REAL_MARKET"


@dataclass(frozen=True, slots=True)
class Scenario:
    """A scripted price/volume shape used to exercise the scoring paths."""

    name: str
    drift: float = 0.0
    vol: float = 0.004
    compression_at: float | None = None  # fraction of the series where the band tightens
    breakout_at: float | None = None  # fraction where price accelerates through
    volume_ramp_at: float | None = None
    pump_at: float | None = None  # fraction where a large vertical move happens


SCENARIOS: dict[str, Scenario] = {
    "compression_breakout": Scenario(
        "compression_breakout", drift=0.00004, vol=0.0035, compression_at=0.55, breakout_at=0.90, volume_ramp_at=0.82
    ),
    "early_accumulation": Scenario("early_accumulation", drift=0.00002, vol=0.003, compression_at=0.6, volume_ramp_at=0.9),
    "late_pump": Scenario("late_pump", drift=0.0001, vol=0.006, pump_at=0.72, volume_ramp_at=0.70),
    "downtrend": Scenario("downtrend", drift=-0.00012, vol=0.005),
    "chop": Scenario("chop", drift=0.0, vol=0.0035),
    "illiquid": Scenario("illiquid", drift=0.0, vol=0.012),
}

# Fixed roster so the dashboard has a stable set of rows in offline mode.
_UNIVERSE: list[tuple[str, str, float, str, float]] = [
    # symbol, base, seed price, scenario, 24h quote volume
    ("BTCUSDT", "BTC", 61250.0, "compression_breakout", 1.9e9),
    ("ETHUSDT", "ETH", 3120.0, "early_accumulation", 9.4e8),
    ("SOLUSDT", "SOL", 148.5, "compression_breakout", 3.1e8),
    ("BNBUSDT", "BNB", 585.0, "chop", 1.4e8),
    ("XRPUSDT", "XRP", 0.5240, "downtrend", 1.1e8),
    ("ADAUSDT", "ADA", 0.4180, "chop", 6.2e7),
    ("AVAXUSDT", "AVAX", 27.40, "early_accumulation", 5.4e7),
    ("LINKUSDT", "LINK", 14.85, "compression_breakout", 4.8e7),
    ("DOGEUSDT", "DOGE", 0.1265, "late_pump", 9.1e7),
    ("MATICUSDT", "MATIC", 0.5820, "downtrend", 3.3e7),
    ("ARBUSDT", "ARB", 0.7420, "early_accumulation", 2.6e7),
    ("OPUSDT", "OP", 1.842, "chop", 2.1e7),
    ("INJUSDT", "INJ", 21.30, "late_pump", 3.8e7),
    ("SEIUSDT", "SEI", 0.3910, "compression_breakout", 1.9e7),
    ("TIAUSDT", "TIA", 6.240, "downtrend", 2.4e7),
    ("PEPEUSDT", "PEPE", 0.00000812, "late_pump", 1.6e8),
    ("WIFUSDT", "WIF", 2.145, "early_accumulation", 7.2e7),
    ("JUPUSDT", "JUP", 0.8930, "chop", 1.4e7),
    ("RNDRUSDT", "RNDR", 7.410, "compression_breakout", 2.2e7),
    ("MICROUSDT", "MICRO", 0.00042, "illiquid", 180_000.0),  # trips the liquidity gate
]


def generate_series(
    symbol: str,
    timeframe: Timeframe,
    limit: int,
    *,
    seed_price: float,
    scenario: Scenario,
    end_time_ms: int,
    seed: int = 0,
    include_open_candle: bool = True,
) -> OHLCVSeries:
    """Deterministic synthetic OHLCV. Same inputs always produce the same output."""
    rng = np.random.default_rng(abs(hash((symbol, timeframe.value, seed))) % (2**32))
    n = limit
    tf_ms = timeframe.ms

    # Align the last candle boundary to the timeframe grid.
    last_open = (end_time_ms // tf_ms) * tf_ms
    open_times = np.array([last_open - (n - 1 - i) * tf_ms for i in range(n)], dtype=np.int64)

    returns = rng.normal(scenario.drift, scenario.vol, n)
    vol_mult = np.ones(n)

    def idx(frac: float | None) -> int | None:
        return None if frac is None else int(n * frac)

    ci, bi, vi, pi = (idx(scenario.compression_at), idx(scenario.breakout_at),
                      idx(scenario.volume_ramp_at), idx(scenario.pump_at))

    if ci is not None:
        end = bi if bi is not None else n
        returns[ci:end] *= 0.32  # volatility contraction
    if bi is not None:
        span = min(n - bi, max(3, n // 25))
        returns[bi : bi + span] += np.linspace(0.004, 0.011, span)
        vol_mult[bi : bi + span] *= np.linspace(1.8, 4.2, span)
    if pi is not None:
        span = min(n - pi, max(6, n // 12))
        returns[pi : pi + span] += np.linspace(0.010, 0.002, span)
        vol_mult[pi : pi + span] *= np.linspace(5.0, 1.6, span)
    if vi is not None:
        span = n - vi
        vol_mult[vi:] *= np.linspace(1.0, 2.6, span)

    close = seed_price * np.exp(np.cumsum(returns))
    open_ = np.concatenate([[seed_price], close[:-1]])
    body = np.abs(close - open_)
    wick = body * rng.uniform(0.2, 1.1, n) + close * scenario.vol * 0.35
    high = np.maximum(open_, close) + wick * rng.uniform(0.2, 1.0, n)
    low = np.minimum(open_, close) - wick * rng.uniform(0.2, 1.0, n)
    low = np.minimum(low, np.minimum(open_, close))
    high = np.maximum(high, np.maximum(open_, close))

    base_vol = seed_price and (1_000_000.0 / max(seed_price, 1e-9))
    volume = np.abs(rng.lognormal(math.log(max(base_vol, 1.0)), 0.35, n)) * vol_mult

    candles: list[Candle] = []
    for i in range(n):
        is_last = i == n - 1
        candles.append(
            Candle(
                open_time_ms=int(open_times[i]),
                open=float(open_[i]),
                high=float(high[i]),
                low=float(low[i]),
                close=float(close[i]),
                volume=float(volume[i]),
                close_time_ms=int(open_times[i]) + tf_ms - 1,
                quote_volume=float(volume[i] * close[i]),
                trades=int(max(1, volume[i] / 40)),
                is_closed=not (is_last and include_open_candle),
            )
        )

    return OHLCVSeries.from_candles(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        provenance=Provenance(
            source=SYNTHETIC_SOURCE,
            as_of_ms=int(open_times[-1]) + tf_ms - 1,
            fetched_at_ms=end_time_ms,
            quality=DataQuality.PARTIAL,
            note=SYNTHETIC_NOTE,
        ),
    )


class FixtureProvider(MarketDataProvider, OrderBookProvider):
    """Offline stand-in. Emits synthetic data, loudly labelled."""

    name = SYNTHETIC_SOURCE

    def __init__(self, clock: Clock = SYSTEM_CLOCK, seed: int = 7, include_open_candle: bool = True) -> None:
        self.clock = clock
        self.seed = seed
        self.include_open_candle = include_open_candle
        self._by_symbol = {row[0]: row for row in _UNIVERSE}

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            available=True,
            latency_ms=0.0,
            detail="SYNTHETIC data source — not a market feed. Values are generated, not observed.",
            checked_at_ms=self.clock.now_ms(),
            rate_limit_remaining_pct=100.0,
        )

    async def list_symbols(self, quote_asset: str) -> list[SymbolInfo]:
        return [
            SymbolInfo(symbol=s, base=b, quote="USDT", status="TRADING")
            for s, b, _, _, _ in _UNIVERSE
            if quote_asset.upper() == "USDT"
        ]

    async def get_tickers_24h(self, symbols: Sequence[str] | None = None) -> dict[str, Ticker24h]:
        wanted = list(symbols) if symbols else list(self._by_symbol)
        now = self.clock.now_ms()
        out: dict[str, Ticker24h] = {}
        for sym in wanted:
            row = self._by_symbol.get(sym)
            if row is None:
                continue
            _, _, price, scenario_name, qv = row
            try:
                series = await self.get_ohlcv(sym, Timeframe.H1, 25)
            except Exception:
                # Mirror the real connector: one bad symbol is skipped, the batch survives.
                continue
            closed = series.closed()
            change = 0.0
            if len(closed) >= 24:
                first = float(closed.close[-24])
                change = (closed.last_close - first) / first * 100.0 if first else 0.0
            out[sym] = Ticker24h(
                symbol=sym,
                last_price=closed.last_close if len(closed) else price,
                quote_volume_24h=qv,
                price_change_pct_24h=change,
                high_24h=float(np.max(closed.high)) if len(closed) else price,
                low_24h=float(np.min(closed.low)) if len(closed) else price,
                trades_24h=int(qv / 500),
                provenance=Provenance(
                    source=self.name,
                    as_of_ms=now,
                    fetched_at_ms=now,
                    quality=DataQuality.PARTIAL,
                    note=SYNTHETIC_NOTE,
                ),
            )
        if not out:
            raise DataUnavailable(f"fixture: no synthetic symbols matched {wanted[:5]}")
        return out

    async def get_ohlcv(self, symbol: str, timeframe: Timeframe, limit: int) -> OHLCVSeries:
        row = self._by_symbol.get(symbol.upper())
        if row is None:
            raise DataUnavailable(f"fixture: {symbol} is not in the synthetic universe")
        _, _, price, scenario_name, _ = row
        return generate_series(
            symbol.upper(),
            timeframe,
            limit,
            seed_price=price,
            scenario=SCENARIOS[scenario_name],
            end_time_ms=self.clock.now_ms(),
            seed=self.seed,
            include_open_candle=self.include_open_candle,
        )

    async def get_order_book(self, symbol: str, depth: int = 100) -> OrderBook:
        series = await self.get_ohlcv(symbol, Timeframe.M5, 60)
        mid = series.closed().last_close
        rng = np.random.default_rng(abs(hash((symbol, "book", self.seed))) % (2**32))
        row = self._by_symbol.get(symbol.upper())
        spread_frac = 0.004 if (row and row[3] == "illiquid") else 0.0004
        skew = float(rng.uniform(0.7, 1.45))

        bids, asks = [], []
        for i in range(depth):
            step = (i + 1) * spread_frac / 2
            size = float(rng.lognormal(3.0, 0.6))
            bids.append(OrderBookLevel(price=mid * (1 - step), qty=size * skew))
            asks.append(OrderBookLevel(price=mid * (1 + step), qty=size / skew))

        now = self.clock.now_ms()
        return OrderBook(
            symbol=symbol.upper(),
            bids=bids,
            asks=asks,
            provenance=Provenance(
                source=self.name, as_of_ms=now, fetched_at_ms=now, quality=DataQuality.PARTIAL, note=SYNTHETIC_NOTE
            ),
        )
