"""Offline provider producing **synthetic** candles.

WHAT THIS IS: a deterministic generator used by the test suite and by
`--provider fixture` for developing without egress or API quota.

WHAT THIS IS NOT: market data. Every candle is a hash-derived value noise path.
Nothing here is a real price, a real volume, or evidence that anything works.

Two guards make it impossible to mistake for the real thing:

* `name = "SYNTHETIC-FIXTURE"` appears in every `Provenance.source`, so the
  string reaches every API response and the dashboard.
* `Provenance.quality` is always `PARTIAL` with the note
  `SYNTHETIC_DATA_NOT_REAL_MARKET`, which the confidence scorer penalises and
  the UI renders as a permanent banner.

--------------------------------------------------------------------------
TIME-ANCHORED HISTORY
--------------------------------------------------------------------------
Every candle is a pure function of its **absolute bar index** —
`open_time_ms // timeframe_ms` — and never of when you asked for it.

That property is what makes the fixture usable as a synthetic *history* rather
than a synthetic *snapshot*. The candle at a given timestamp is identical
whether you fetch it now or six hours from now, so:

* the outcome tracker can resolve a signal recorded earlier against the very
  bars that followed it, offline;
* backtests are reproducible run to run;
* `series.upto(t)` returns the same slice every time.

The previous implementation seeded an RNG per call and anchored the walk to
"now", so the price at a given timestamp drifted between calls. Anything that
compared two fetches was comparing two different universes.

Prices come from multi-octave value noise summed with **Brownian scaling**
(Hurst 0.5), so the path is a near-martingale like a real price series rather
than white noise around a level — see `_fbm` for why that distinction decides
whether label statistics measured here mean anything. Scenario behaviour —
compression, breakout, pump — is a periodic function of the absolute bar index,
so any window contains part of a cycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

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
from cryptopulse.providers.base import (
    MarketDataProvider,
    OrderBookProvider,
    ProviderHealth,
    TokenDiscoveryProvider,
)

__all__ = ["FixtureProvider", "Scenario", "SYNTHETIC_SOURCE", "SYNTHETIC_NOTE", "generate_window"]

SYNTHETIC_SOURCE = "SYNTHETIC-FIXTURE"
SYNTHETIC_NOTE = "SYNTHETIC_DATA_NOT_REAL_MARKET"

_U64 = np.uint64


# --------------------------------------------------------------------------- #
# Deterministic noise, keyed on absolute bar index
# --------------------------------------------------------------------------- #


def _hash01(idx: np.ndarray, salt: int) -> np.ndarray:
    """splitmix64 finalizer -> uniform [0, 1). Pure function of (idx, salt)."""
    z = (idx.astype(np.int64).astype(_U64) + _U64(salt & 0xFFFFFFFFFFFFFFFF)) * _U64(0x9E3779B97F4A7C15)
    z = (z ^ (z >> _U64(30))) * _U64(0xBF58476D1CE4E5B9)
    z = (z ^ (z >> _U64(27))) * _U64(0x94D049BB133111EB)
    z = z ^ (z >> _U64(31))
    return (z >> _U64(11)).astype(np.float64) / float(1 << 53)


def _value_noise(idx: np.ndarray, salt: int, spacing: float) -> np.ndarray:
    """Smooth noise in [-1, 1]: hash the lattice, smoothstep between points."""
    p = idx / max(spacing, 1e-9)
    i0 = np.floor(p)
    frac = p - i0
    a = _hash01(i0, salt) * 2.0 - 1.0
    b = _hash01(i0 + 1.0, salt) * 2.0 - 1.0
    t = frac * frac * (3.0 - 2.0 * frac)
    return a + (b - a) * t


def _fbm(idx: np.ndarray, salt: int, octaves: int = 9, base_spacing: float = 512.0, hurst: float = 0.5) -> np.ndarray:
    """Fractional Brownian motion by octave summation.

    `hurst` controls how amplitude scales as the lattice halves, and it is the
    parameter that decides whether the result behaves like a market:

    * H = 1.0 (amplitude halves with spacing) gives a smooth curve whose
      increments are effectively white noise around a moving level. Such a series
      is strongly mean-reverting bar to bar, so a stop 1 ATR away is hit far more
      often than a target 2 ATR away — a random entry wins about 10% of the time
      instead of the ~33% a 2:1 barrier ratio implies.
    * H = 0.5 (amplitude scales with sqrt of spacing) is Brownian: increments are
      independent and the path is a near-martingale, like a real price series.

    H = 0.5 is the default because label statistics measured on an H = 1 series
    say nothing transferable about a market. Getting this wrong made every
    synthetic outcome look catastrophic for reasons that had nothing to do with
    the strategy.
    """
    total = np.zeros_like(idx, dtype=np.float64)
    norm = 0.0
    amp = 1.0
    decay = 2.0**-hurst
    for k in range(octaves):
        spacing = base_spacing / (2**k)
        if spacing < 1.0:
            break
        total += amp * _value_noise(idx, salt + k * 7919, spacing)
        norm += amp
        amp *= decay
    return total / max(norm, 1e-9)


def _salt(symbol: str, timeframe: Timeframe, extra: int = 0) -> int:
    """Stable per-(symbol, timeframe) salt.

    Uses FNV-1a rather than Python's `hash()`, which is randomised per process
    and would break reproducibility across runs.
    """
    h = 1469598103934665603
    for ch in f"{symbol}|{timeframe.value}|{extra}":
        h = ((h ^ ord(ch)) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


# --------------------------------------------------------------------------- #
# Scenarios — periodic in absolute bar index
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Scenario:
    """A repeating behavioural shape laid over the noise path.

    `cycle` is in bars. Events sit at fixed phases of that cycle, so any window
    of a few hundred bars contains part of one.
    """

    name: str
    vol: float = 0.30
    cycle: int = 360
    compression_from: float | None = None
    compression_to: float | None = None
    breakout_at: float | None = None
    pump_at: float | None = None
    volume_ramp_from: float | None = None


SCENARIOS: dict[str, Scenario] = {
    "compression_breakout": Scenario(
        "compression_breakout", vol=0.26, cycle=340,
        compression_from=0.45, compression_to=0.80, breakout_at=0.80, volume_ramp_from=0.72,
    ),
    "early_accumulation": Scenario(
        "early_accumulation", vol=0.22, cycle=420,
        compression_from=0.40, compression_to=0.92, volume_ramp_from=0.78,
    ),
    "late_pump": Scenario("late_pump", vol=0.38, cycle=300, pump_at=0.55, volume_ramp_from=0.52),
    "downtrend": Scenario("downtrend", vol=0.34, cycle=500),
    "chop": Scenario("chop", vol=0.20, cycle=260),
    "illiquid": Scenario("illiquid", vol=0.75, cycle=180),
}

# symbol, base asset, reference price, scenario, 24h quote volume
_UNIVERSE: list[tuple[str, str, float, str, float]] = [
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


def _phase(idx: np.ndarray, cycle: int) -> np.ndarray:
    return (idx % cycle) / float(cycle)


def _ramp(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Smooth 0->1 ramp across [lo, hi], flat outside."""
    if hi <= lo:
        return np.zeros_like(x)
    t = np.clip((x - lo) / (hi - lo), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


# --------------------------------------------------------------------------- #
# Window generation
# --------------------------------------------------------------------------- #


def generate_window(
    symbol: str,
    timeframe: Timeframe,
    first_index: int,
    count: int,
    *,
    seed_price: float,
    scenario: Scenario,
) -> tuple[np.ndarray, ...]:
    """Candles for absolute bar indices [first_index, first_index + count).

    Pure function of the indices: asking for the same index twice, in any
    process, at any wall-clock time, returns the same candle.
    """
    # Generate one extra bar in front and drop it at the end. Without it the
    # window's first open would have to be invented, which would make that one
    # candle depend on where the window starts — breaking the purity guarantee
    # for exactly the bar an outcome resolution is most likely to land on.
    idx = np.arange(first_index - 1, first_index + count, dtype=np.float64)
    price_salt = _salt(symbol, timeframe, 1)
    vol_salt = _salt(symbol, timeframe, 2)
    wick_salt = _salt(symbol, timeframe, 3)

    ph = _phase(idx, scenario.cycle)

    # Brownian-scaled octaves down to single-bar detail. There is deliberately no
    # separate per-bar jitter term: an independent offset added to every bar makes
    # the series white noise around the path, which mean-reverts and destroys the
    # barrier statistics. All the bar-scale movement comes from the finest octaves.
    # Compression damps the *fine* octaves only, leaving the slow trend intact.
    squeeze = np.ones_like(idx)
    if scenario.compression_from is not None and scenario.compression_to is not None:
        inside = (ph >= scenario.compression_from) & (ph < scenario.compression_to)
        squeeze = np.where(inside, 0.35, 1.0)
    fine = _fbm(idx, price_salt + 555, octaves=4, base_spacing=16.0, hurst=0.5)
    coarse = _fbm(idx, price_salt, octaves=6, base_spacing=512.0, hurst=0.5)

    log_path = scenario.vol * (0.72 * coarse + 0.28 * fine * squeeze)

    # A breakout is a smooth upward step in log price; a pump rises then fades.
    if scenario.breakout_at is not None:
        b = scenario.breakout_at
        log_path = log_path + 0.16 * _ramp(ph, b, min(b + 0.08, 0.999))
    if scenario.pump_at is not None:
        p = scenario.pump_at
        rise = _ramp(ph, p, min(p + 0.10, 0.999))
        fade = _ramp(ph, min(p + 0.20, 0.999), 1.0)
        log_path = log_path + 0.42 * rise - 0.22 * fade
    if scenario.name == "downtrend":
        log_path = log_path - 0.30 * ph  # persistent decay across each cycle

    close = seed_price * np.exp(log_path)
    open_ = np.empty_like(close)
    open_[0] = close[0]  # discarded with the warm-up bar below
    open_[1:] = close[:-1]

    # Wicks scale with the bar's own body and the scenario volatility.
    body = np.abs(close - open_)
    span = body + close * scenario.vol * 0.012 * squeeze
    up = _hash01(idx, wick_salt) * span
    dn = _hash01(idx, wick_salt + 1013) * span
    high = np.maximum(np.maximum(open_, close) + up, np.maximum(open_, close))
    low = np.minimum(np.minimum(open_, close) - dn, np.minimum(open_, close))
    low = np.maximum(low, close * 1e-6)  # never non-positive

    # Volume: its own noise, plus ramps around the scripted events.
    base_units = 1_000_000.0 / max(seed_price, 1e-9)
    vol_noise = np.exp(1.05 * _fbm(idx, vol_salt, octaves=5, base_spacing=96.0, hurst=0.75))
    mult = np.ones_like(idx)
    if scenario.volume_ramp_from is not None:
        mult = mult + 2.1 * _ramp(ph, scenario.volume_ramp_from, 1.0)
    if scenario.breakout_at is not None:
        b = scenario.breakout_at
        mult = mult + 2.6 * _ramp(ph, b, min(b + 0.05, 0.999)) * (1.0 - _ramp(ph, min(b + 0.10, 0.999), 1.0))
    if scenario.pump_at is not None:
        p = scenario.pump_at
        mult = mult + 4.0 * _ramp(ph, p, min(p + 0.05, 0.999)) * (1.0 - _ramp(ph, min(p + 0.15, 0.999), 1.0))
    volume = np.maximum(base_units * vol_noise * mult, 1e-6)

    # Drop the warm-up bar; every remaining bar is a pure function of its index.
    return open_[1:], high[1:], low[1:], close[1:], volume[1:]


@lru_cache(maxsize=512)
def _scenario_for(symbol: str) -> tuple[float, Scenario, float]:
    for sym, _base, price, scen, qv in _UNIVERSE:
        if sym == symbol:
            return price, SCENARIOS[scen], qv
    raise KeyError(symbol)


class FixtureProvider(MarketDataProvider, OrderBookProvider, TokenDiscoveryProvider):
    """Offline stand-in. Emits synthetic data, loudly labelled."""

    name = SYNTHETIC_SOURCE

    def __init__(self, clock: Clock = SYSTEM_CLOCK, seed: int = 7, include_open_candle: bool = True) -> None:
        self.clock = clock
        self.seed = seed
        self.include_open_candle = include_open_candle
        self._symbols = {row[0] for row in _UNIVERSE}

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
        if quote_asset.upper() != "USDT":
            return []
        return [SymbolInfo(symbol=s, base=b, quote="USDT", status="TRADING") for s, b, _, _, _ in _UNIVERSE]

    async def get_tickers_24h(self, symbols: Sequence[str] | None = None) -> dict[str, Ticker24h]:
        wanted = list(symbols) if symbols else sorted(self._symbols)
        now = self.clock.now_ms()
        out: dict[str, Ticker24h] = {}
        for sym in wanted:
            if sym not in self._symbols:
                continue
            try:
                series = (await self.get_ohlcv(sym, Timeframe.H1, 26)).closed()
            except Exception:
                # Mirror the real connector: one bad symbol is skipped, the batch survives.
                continue
            if len(series) == 0:
                continue
            _, _, qv = _scenario_for(sym)
            first = float(series.close[0])
            change = (series.last_close - first) / first * 100.0 if first else 0.0
            out[sym] = Ticker24h(
                symbol=sym,
                last_price=series.last_close,
                quote_volume_24h=qv,
                price_change_pct_24h=change,
                high_24h=float(np.max(series.high)),
                low_24h=float(np.min(series.low)),
                trades_24h=int(qv / 500),
                provenance=Provenance(
                    source=self.name, as_of_ms=now, fetched_at_ms=now,
                    quality=DataQuality.PARTIAL, note=SYNTHETIC_NOTE,
                ),
            )
        if not out:
            raise DataUnavailable(f"fixture: no synthetic symbols matched {wanted[:5]}")
        return out

    async def get_ohlcv(self, symbol: str, timeframe: Timeframe, limit: int) -> OHLCVSeries:
        symbol = symbol.upper()
        if symbol not in self._symbols:
            raise DataUnavailable(f"fixture: {symbol} is not in the synthetic universe")
        seed_price, scenario, _ = _scenario_for(symbol)

        tf_ms = timeframe.ms
        now_ms = self.clock.now_ms()
        # Absolute index of the bar currently forming. Using absolute indices is
        # what keeps a given timestamp mapped to a given candle forever.
        current_index = now_ms // tf_ms
        last_index = current_index if self.include_open_candle else current_index - 1
        first_index = last_index - (limit - 1)

        open_, high, low, close, volume = generate_window(
            symbol, timeframe, first_index, limit, seed_price=seed_price, scenario=scenario
        )

        candles: list[Candle] = []
        for i in range(limit):
            open_time = (first_index + i) * tf_ms
            close_time = open_time + tf_ms - 1
            candles.append(
                Candle(
                    open_time_ms=open_time,
                    open=float(open_[i]),
                    high=float(high[i]),
                    low=float(low[i]),
                    close=float(close[i]),
                    volume=float(volume[i]),
                    close_time_ms=close_time,
                    quote_volume=float(volume[i] * close[i]),
                    trades=int(max(1, volume[i] / 40)),
                    is_closed=close_time <= now_ms,
                )
            )

        newest_closed = max((c.close_time_ms for c in candles if c.is_closed), default=candles[-1].close_time_ms)
        return OHLCVSeries.from_candles(
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
            provenance=Provenance(
                source=self.name, as_of_ms=newest_closed, fetched_at_ms=now_ms,
                quality=DataQuality.PARTIAL, note=SYNTHETIC_NOTE,
            ),
        )

    async def get_order_book(self, symbol: str, depth: int = 100) -> OrderBook:
        symbol = symbol.upper()
        series = (await self.get_ohlcv(symbol, Timeframe.M5, 3)).closed()
        if len(series) == 0:
            raise DataUnavailable(f"fixture: no closed candle for {symbol}")
        mid = series.last_close
        _, scenario, _ = _scenario_for(symbol)

        # Book shape is keyed on the current bar index, so it is stable within a
        # bar and changes between bars — like a real snapshot, minus the entropy.
        bar = np.array([series.last_open_time_ms // Timeframe.M5.ms], dtype=np.float64)
        book_salt = _salt(symbol, Timeframe.M5, 9)
        skew = 0.65 + 0.9 * float(_hash01(bar, book_salt)[0])
        spread_frac = 0.004 if scenario.name == "illiquid" else 0.0004

        sizes = _hash01(np.arange(depth, dtype=np.float64) + float(bar[0]) * 1000.0, book_salt + 31)
        bids, asks = [], []
        for i in range(depth):
            step = (i + 1) * spread_frac / 2
            size = 6.0 + 40.0 * float(sizes[i])
            bids.append(OrderBookLevel(price=mid * (1 - step), qty=size * skew))
            asks.append(OrderBookLevel(price=mid * (1 + step), qty=size / skew))

        now = self.clock.now_ms()
        return OrderBook(
            symbol=symbol,
            bids=bids,
            asks=asks,
            provenance=Provenance(
                source=self.name, as_of_ms=now, fetched_at_ms=now,
                quality=DataQuality.PARTIAL, note=SYNTHETIC_NOTE,
            ),
        )

    # -- token discovery ------------------------------------------------------ #

    async def discover_universe(self, quote_asset: str) -> dict[str, Ticker24h]:
        """The whole venue in one request — the call that makes a wide search viable."""
        return await self.get_tickers_24h(None)
