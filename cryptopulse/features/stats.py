"""Normalisation helpers.

A +150% volume day is unremarkable for a volatile micro-cap and extraordinary for
a large-cap. Every raw feature is therefore compared two ways: against the
asset's own history (self-relative) and against the rest of the universe at the
same instant (cross-sectional).
"""

from __future__ import annotations

import numpy as np

__all__ = ["zscore_last", "percentile_rank_last", "robust_zscore_last", "cross_sectional_percentile", "clamp01", "scale"]


def clamp01(x: float) -> float:
    if not np.isfinite(x):
        return 0.0
    return float(min(1.0, max(0.0, x)))


def scale(value: float, low: float, high: float) -> float:
    """Linear map from [low, high] to [0, 1], clamped. `low` may exceed `high`."""
    if not np.isfinite(value) or low == high:
        return 0.0
    return clamp01((value - low) / (high - low))


def zscore_last(values: np.ndarray, lookback: int = 100) -> float | None:
    """Z-score of the final observation against the preceding `lookback` bars.

    The baseline excludes the final bar so an extreme value does not inflate the
    standard deviation it is being measured against.
    """
    v = np.asarray(values, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size < 12:
        return None
    window = v[-(lookback + 1) :]
    current, base = float(window[-1]), window[:-1]
    if base.size < 10:
        return None
    sd = float(np.std(base))
    if sd <= 0:
        return None
    return (current - float(np.mean(base))) / sd


def robust_zscore_last(values: np.ndarray, lookback: int = 100) -> float | None:
    """Median/MAD z-score. Resistant to the outliers that dominate crypto volume."""
    v = np.asarray(values, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size < 12:
        return None
    window = v[-(lookback + 1) :]
    current, base = float(window[-1]), window[:-1]
    if base.size < 10:
        return None
    med = float(np.median(base))
    mad = float(np.median(np.abs(base - med)))
    if mad <= 0:
        return None
    return (current - med) / (1.4826 * mad)


def percentile_rank_last(values: np.ndarray, lookback: int = 200) -> float | None:
    """Fraction of the trailing window that the final observation exceeds, in [0, 1]."""
    v = np.asarray(values, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size < 10:
        return None
    window = v[-lookback:]
    current = float(window[-1])
    return float(np.mean(window[:-1] <= current))


def cross_sectional_percentile(value: float, population: list[float]) -> float | None:
    """Rank one asset's value against every other asset scanned in the same pass."""
    pop = np.array([p for p in population if p is not None and np.isfinite(p)], dtype=np.float64)
    if pop.size < 5 or not np.isfinite(value):
        return None
    return float(np.mean(pop <= value))
