"""Market regime classification.

Two uses: the backtester reports performance per regime (a scanner that only
works in a bull trend is a scanner you must not run in a bear trend), and the
scanner records the regime alongside every signal so that later analysis can
condition on it.

Regime is derived from a reference asset (BTC by default) on a higher timeframe,
never from the asset being scored — otherwise the label leaks the answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from cryptopulse.features.indicators import atr, ema, roc

__all__ = ["Regime", "VolatilityRegime", "RegimeReport", "classify_regime"]


class Regime(str, Enum):
    BULL_TREND = "BULL_TREND"
    BEAR_TREND = "BEAR_TREND"
    RANGE = "RANGE"
    UNKNOWN = "UNKNOWN"


class VolatilityRegime(str, Enum):
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    NORMAL_VOLATILITY = "NORMAL_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class RegimeReport:
    trend: Regime
    volatility: VolatilityRegime
    reference_symbol: str
    atr_pct: float | None
    ema_spread_pct: float | None
    roc_pct: float | None

    def to_dict(self) -> dict:
        return {
            "trend": self.trend.value,
            "volatility": self.volatility.value,
            "reference": self.reference_symbol,
            "atr_pct": None if self.atr_pct is None else round(self.atr_pct, 4),
            "ema_spread_pct": None if self.ema_spread_pct is None else round(self.ema_spread_pct, 4),
            "roc_pct": None if self.roc_pct is None else round(self.roc_pct, 4),
        }

    @classmethod
    def unknown(cls, symbol: str = "n/a") -> RegimeReport:
        return cls(Regime.UNKNOWN, VolatilityRegime.UNKNOWN, symbol, None, None, None)


def classify_regime(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    *,
    reference_symbol: str = "BTCUSDT",
    fast: int = 20,
    slow: int = 50,
    trend_threshold_pct: float = 0.75,
) -> RegimeReport:
    c = np.asarray(close, dtype=np.float64)
    if c.size < slow + 5:
        return RegimeReport.unknown(reference_symbol)

    ema_fast = ema(c, fast)
    ema_slow = ema(c, slow)
    if not (np.isfinite(ema_fast[-1]) and np.isfinite(ema_slow[-1]) and ema_slow[-1] > 0):
        return RegimeReport.unknown(reference_symbol)

    spread_pct = float((ema_fast[-1] - ema_slow[-1]) / ema_slow[-1] * 100.0)
    roc_series = roc(c, min(24, c.size - 1))
    roc_now = float(roc_series[-1]) if np.isfinite(roc_series[-1]) else None

    if spread_pct > trend_threshold_pct:
        trend = Regime.BULL_TREND
    elif spread_pct < -trend_threshold_pct:
        trend = Regime.BEAR_TREND
    else:
        trend = Regime.RANGE

    atr_series = atr(high, low, c, 14)
    atr_pct = None
    vol = VolatilityRegime.UNKNOWN
    if np.isfinite(atr_series[-1]) and c[-1] > 0:
        atr_pct = float(atr_series[-1] / c[-1] * 100.0)
        history = atr_series[np.isfinite(atr_series)] / c[-atr_series[np.isfinite(atr_series)].size :] * 100.0
        if history.size >= 30:
            hi = float(np.percentile(history, 75))
            lo = float(np.percentile(history, 25))
            if atr_pct >= hi:
                vol = VolatilityRegime.HIGH_VOLATILITY
            elif atr_pct <= lo:
                vol = VolatilityRegime.LOW_VOLATILITY
            else:
                vol = VolatilityRegime.NORMAL_VOLATILITY

    return RegimeReport(
        trend=trend,
        volatility=vol,
        reference_symbol=reference_symbol,
        atr_pct=atr_pct,
        ema_spread_pct=spread_pct,
        roc_pct=roc_now,
    )
