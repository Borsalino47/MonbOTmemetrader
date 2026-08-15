"""Volume features.

The distinction this module exists to make: *high* volume and *rising* volume are
different signals. A coin that has been at RVOL 2.5 for six hours is already
discovered. A coin whose RVOL just went 1.0 → 1.2 → 1.5 → 1.9 → 2.6 is the thing
we are actually hunting.
"""

from __future__ import annotations

import numpy as np

from cryptopulse.features.indicators import linreg_slope, sma

__all__ = [
    "relative_volume",
    "volume_acceleration",
    "volume_trend_slope",
    "volume_climax",
    "quote_volume_series",
]


def relative_volume(volume: np.ndarray, period: int = 50) -> np.ndarray:
    """RVOL = current volume / trailing average volume.

    The trailing average deliberately excludes the current bar, otherwise a huge
    bar inflates its own baseline and mutes the very spike we want to see.
    """
    v = np.asarray(volume, dtype=np.float64)
    n = v.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period + 1:
        return out
    baseline = sma(v, period)
    # baseline[i] covers bars [i-period+1 .. i]; shift by one so bar i is excluded.
    shifted = np.full(n, np.nan, dtype=np.float64)
    shifted[1:] = baseline[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(shifted > 0, v / shifted, np.nan)
    return out


def volume_acceleration(volume: np.ndarray, fast: int = 3, slow: int = 12) -> np.ndarray:
    """Percent by which recent volume exceeds the medium-term average.

    `(mean(last `fast`) / mean(last `slow`) - 1) * 100`. Positive means volume is
    building right now relative to the recent norm, independent of absolute size.
    """
    v = np.asarray(volume, dtype=np.float64)
    f = sma(v, fast)
    s = sma(v, slow)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where((s > 0) & ~np.isnan(f), (f / s - 1.0) * 100.0, np.nan)


def volume_trend_slope(volume: np.ndarray, period: int = 8) -> np.ndarray:
    """Normalised least-squares slope of volume: units of "average bars per bar".

    Dividing the raw slope by the window mean makes the number comparable between
    a large-cap and a micro-cap.
    """
    v = np.asarray(volume, dtype=np.float64)
    slope = linreg_slope(v, period)
    base = sma(v, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(base > 0, slope / base, np.nan)


def volume_climax(volume: np.ndarray, period: int = 50, threshold: float = 4.0) -> np.ndarray:
    """Boolean flag: this bar's volume is an extreme outlier versus its own history.

    Climax volume is a *risk* signal, not an opportunity signal — it frequently
    marks exhaustion at the end of a move rather than the start of one.
    """
    rv = relative_volume(volume, period)
    return np.where(np.isnan(rv), False, rv >= threshold)


def quote_volume_series(close: np.ndarray, volume: np.ndarray, quote_volume: np.ndarray | None) -> np.ndarray:
    """Prefer the exchange-reported quote volume; fall back to close * base volume.

    The fallback is an identity, not an estimate: for a single candle
    close*volume is a legitimate (if slightly coarse) notional. We still tag the
    fallback so data confidence can account for it.
    """
    if quote_volume is not None and np.asarray(quote_volume).size and np.nansum(quote_volume) > 0:
        return np.asarray(quote_volume, dtype=np.float64)
    return np.asarray(close, dtype=np.float64) * np.asarray(volume, dtype=np.float64)
