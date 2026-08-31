"""Feature assembly.

Turns raw OHLCV into the single `AssetFeatures` object that the scoring engine
consumes. Nothing downstream touches raw candles, so there is exactly one place
where the closed-candle rule has to be enforced — `TimeframeFeatures.build`,
which calls `series.closed()` before it does anything else.

Every optional field is `None` when it cannot be computed honestly. There is no
zero-filling: a `None` RVOL means "unknown", and the scorer awards no points for
unknown, rather than awarding the points a zero would imply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from cryptopulse.core.types import AssetValuation, OHLCVSeries, Provenance, Timeframe
from cryptopulse.features.expansion import ExpansionReport, analyse_expansion
from cryptopulse.features.indicators import (
    atr,
    bollinger_width,
    consecutive_up,
    ema,
    macd,
    roc,
    rolling_vwap,
    rsi,
)
from cryptopulse.features.stats import percentile_rank_last, robust_zscore_last
from cryptopulse.features.structure import StructureReport, analyse_structure, compression_ratio
from cryptopulse.features.volume import (
    quote_volume_series,
    relative_volume,
    volume_acceleration,
    volume_climax,
    volume_trend_slope,
)

__all__ = ["Bias", "TimeframeFeatures", "AssetFeatures"]


class Bias(str, Enum):
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"
    UNKNOWN = "UNKNOWN"


def _f(x) -> float | None:
    """Coerce to a plain float, mapping nan/inf to None."""
    try:
        val = float(x)
    except (TypeError, ValueError):
        return None
    return val if np.isfinite(val) else None


@dataclass(slots=True)
class TimeframeFeatures:
    """Indicator snapshot for one symbol on one timeframe, at the last closed bar."""

    timeframe: Timeframe
    bars: int
    close: float
    open_time_ms: int
    close_time_ms: int

    ema9: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    rsi14: float | None = None
    macd_hist: float | None = None
    macd_hist_prev: float | None = None
    atr14: float | None = None
    atr_pct: float | None = None
    bb_width: float | None = None
    compression: float | None = None  # 0 = tightest in lookback, 1 = widest
    vwap20: float | None = None
    vwap_distance_pct: float | None = None
    ema20_distance_pct: float | None = None

    rvol: float | None = None
    rvol_prev: float | None = None
    rvol_slope: float | None = None  # least-squares slope of the last 5 RVOL readings
    volume_acceleration_pct: float | None = None
    volume_trend: float | None = None
    volume_climax: bool = False
    volume_zscore: float | None = None
    quote_volume_last: float | None = None

    roc1: float | None = None
    roc3: float | None = None
    roc6: float | None = None
    roc12: float | None = None
    price_zscore: float | None = None
    range_percentile: float | None = None

    consecutive_green: int = 0
    structure: StructureReport | None = None
    # Long-horizon reading (base age, drawdown from the window high, accumulation
    # tape). Only meaningful on a high timeframe with real history behind it; it
    # is computed for every timeframe because the cost is negligible and the
    # moonshot layer picks whichever timeframe it was given.
    expansion: ExpansionReport | None = None
    bias: Bias = Bias.UNKNOWN
    provenance: Provenance | None = None
    warnings: list[str] = field(default_factory=list)

    # -- construction ------------------------------------------------------- #

    @classmethod
    def build(cls, series: OHLCVSeries, *, min_bars: int = 60, structure_kwargs: dict | None = None) -> TimeframeFeatures:
        s = series.closed()  # <- the no-look-ahead boundary
        n = len(s)
        if n == 0:
            raise ValueError(f"{series.symbol} {series.timeframe.value}: no closed candles")

        feats = cls(
            timeframe=s.timeframe,
            bars=n,
            close=float(s.close[-1]),
            open_time_ms=s.last_open_time_ms,
            close_time_ms=s.last_close_time_ms,
            provenance=s.provenance,
        )
        if n < min_bars:
            feats.warnings.append(f"INSUFFICIENT_HISTORY:{n}<{min_bars}")

        c, h, l, v = s.close, s.high, s.low, s.volume
        qv = quote_volume_series(c, v, s.quote_volume)

        feats.ema9 = _f(ema(c, 9)[-1]) if n >= 9 else None
        feats.ema20 = _f(ema(c, 20)[-1]) if n >= 20 else None
        feats.ema50 = _f(ema(c, 50)[-1]) if n >= 50 else None
        feats.ema200 = _f(ema(c, 200)[-1]) if n >= 200 else None
        feats.rsi14 = _f(rsi(c, 14)[-1]) if n >= 15 else None

        if n >= 35:
            _, _, hist = macd(c)
            feats.macd_hist = _f(hist[-1])
            feats.macd_hist_prev = _f(hist[-2]) if hist.size >= 2 else None

        if n >= 15:
            a = atr(h, l, c, 14)
            feats.atr14 = _f(a[-1])
            if feats.atr14 and feats.close > 0:
                feats.atr_pct = feats.atr14 / feats.close * 100.0

        if n >= 20:
            feats.bb_width = _f(bollinger_width(c, 20)[-1])
            feats.compression = compression_ratio(c, 20, lookback=min(120, n))
            feats.vwap20 = _f(rolling_vwap(h, l, c, v, 20)[-1])
            if feats.vwap20:
                feats.vwap_distance_pct = (feats.close - feats.vwap20) / feats.vwap20 * 100.0
        if feats.ema20:
            feats.ema20_distance_pct = (feats.close - feats.ema20) / feats.ema20 * 100.0

        # Volume block.
        if n >= 51:
            rv = relative_volume(v, 50)
            feats.rvol = _f(rv[-1])
            feats.rvol_prev = _f(rv[-2]) if rv.size >= 2 else None
            tail = rv[-5:][np.isfinite(rv[-5:])]
            if tail.size >= 3:
                x = np.arange(tail.size, dtype=np.float64)
                feats.rvol_slope = _f(np.polyfit(x, tail, 1)[0])
            feats.volume_climax = bool(volume_climax(v, 50)[-1])
        if n >= 13:
            feats.volume_acceleration_pct = _f(volume_acceleration(v, 3, 12)[-1])
        if n >= 9:
            feats.volume_trend = _f(volume_trend_slope(v, 8)[-1])
        feats.volume_zscore = robust_zscore_last(v, lookback=min(120, n))
        feats.quote_volume_last = _f(qv[-1])

        # Price momentum block.
        for attr, period in (("roc1", 1), ("roc3", 3), ("roc6", 6), ("roc12", 12)):
            if n > period:
                setattr(feats, attr, _f(roc(c, period)[-1]))
        feats.price_zscore = robust_zscore_last(c, lookback=min(120, n))
        feats.range_percentile = percentile_rank_last(h - l, lookback=min(120, n))
        feats.consecutive_green = consecutive_up(c, s.open)

        feats.structure = analyse_structure(h, l, c, s.open_time_ms, **(structure_kwargs or {}))
        feats.expansion = analyse_expansion(h, l, c, v, s.open_time_ms, atr_value=feats.atr14)
        feats.bias = feats._compute_bias()
        return feats

    # -- derived ------------------------------------------------------------ #

    def _compute_bias(self) -> Bias:
        """Directional read of this timeframe from EMA stack, MACD and structure.

        Deliberately coarse — three buckets. A finer scale would imply a precision
        the inputs do not support.
        """
        votes = 0
        seen = 0

        if self.ema20 is not None:
            seen += 1
            votes += 1 if self.close > self.ema20 else -1
        if self.ema20 is not None and self.ema50 is not None:
            seen += 1
            votes += 1 if self.ema20 > self.ema50 else -1
        if self.macd_hist is not None:
            seen += 1
            votes += 1 if self.macd_hist > 0 else -1
        if self.structure is not None:
            from cryptopulse.features.structure import StructureTrend

            if self.structure.trend is StructureTrend.UPTREND:
                seen += 1
                votes += 1
            elif self.structure.trend is StructureTrend.DOWNTREND:
                seen += 1
                votes -= 1

        if seen == 0:
            return Bias.UNKNOWN
        ratio = votes / seen
        if ratio >= 0.5:
            return Bias.BULLISH
        if ratio <= -0.5:
            return Bias.BEARISH
        return Bias.NEUTRAL

    def to_dict(self) -> dict:
        return {
            "timeframe": self.timeframe.value,
            "bars": self.bars,
            "close": self.close,
            "bias": self.bias.value,
            "ema9": self.ema9,
            "ema20": self.ema20,
            "ema50": self.ema50,
            "ema200": self.ema200,
            "rsi14": self.rsi14,
            "macd_hist": self.macd_hist,
            "atr14": self.atr14,
            "atr_pct": self.atr_pct,
            "bb_width": self.bb_width,
            "compression": self.compression,
            "vwap20": self.vwap20,
            "vwap_distance_pct": self.vwap_distance_pct,
            "ema20_distance_pct": self.ema20_distance_pct,
            "rvol": self.rvol,
            "rvol_slope": self.rvol_slope,
            "volume_acceleration_pct": self.volume_acceleration_pct,
            "volume_trend": self.volume_trend,
            "volume_climax": self.volume_climax,
            "volume_zscore": self.volume_zscore,
            "roc1": self.roc1,
            "roc3": self.roc3,
            "roc6": self.roc6,
            "roc12": self.roc12,
            "consecutive_green": self.consecutive_green,
            "structure": self.structure.to_dict() if self.structure else None,
            "expansion": self.expansion.to_dict() if self.expansion else None,
            "warnings": self.warnings,
        }


@dataclass(slots=True)
class AssetFeatures:
    """All timeframes for one symbol, plus the cross-timeframe derived values."""

    symbol: str
    primary_timeframe: Timeframe
    timeframes: dict[Timeframe, TimeframeFeatures]
    quote_volume_24h: float | None = None
    price_change_pct_24h: float | None = None
    order_book_imbalance: float | None = None
    spread_bps: float | None = None
    order_book_provenance: Provenance | None = None
    ticker_provenance: Provenance | None = None
    warnings: list[str] = field(default_factory=list)

    # -- cross-asset context, filled in by the scanner ---------------------- #
    # None everywhere means "the scanner did not compute it", which scores
    # nothing rather than scoring neutral.
    valuation: AssetValuation | None = None
    benchmark_symbol: str | None = None
    # Percentage points by which this asset out- or under-performed the benchmark
    # over the same window. Positive = stronger than the market.
    rs_vs_benchmark_pct: float | None = None
    # This asset's RVOL as a percentile of every asset in the same scan. A 2.5x
    # RVOL means something different on a quiet day than on a day the whole
    # market is running.
    rvol_percentile_universe: float | None = None

    @property
    def primary(self) -> TimeframeFeatures:
        return self.timeframes[self.primary_timeframe]

    @property
    def price(self) -> float:
        return self.primary.close

    def get(self, tf: Timeframe) -> TimeframeFeatures | None:
        return self.timeframes.get(tf)

    def highest_timeframe(self) -> TimeframeFeatures | None:
        """The slowest timeframe available, which is where a multi-week base shows.

        The moonshot layer prefers the daily but must not silently score a 5m
        chart as if it were one, so the caller checks which timeframe came back.
        """
        if not self.timeframes:
            return None
        return max(self.timeframes.values(), key=lambda f: f.timeframe.seconds)

    def change_pct(self, tf: Timeframe, bars: int = 1) -> float | None:
        """Percent change over `bars` closed candles of `tf`."""
        f = self.timeframes.get(tf)
        if f is None:
            return None
        return {1: f.roc1, 3: f.roc3, 6: f.roc6, 12: f.roc12}.get(bars)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "primary_timeframe": self.primary_timeframe.value,
            "quote_volume_24h": self.quote_volume_24h,
            "price_change_pct_24h": self.price_change_pct_24h,
            "order_book_imbalance": self.order_book_imbalance,
            "spread_bps": self.spread_bps,
            "valuation": self.valuation.to_dict() if self.valuation else None,
            "benchmark_symbol": self.benchmark_symbol,
            "rs_vs_benchmark_pct": self.rs_vs_benchmark_pct,
            "rvol_percentile_universe": self.rvol_percentile_universe,
            "timeframes": {tf.value: f.to_dict() for tf, f in self.timeframes.items()},
            "warnings": self.warnings,
        }
