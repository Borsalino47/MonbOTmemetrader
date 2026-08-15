"""Technical indicators, numpy only.

Contract shared by every function in this module:

1. Input is a 1-D float array of length n; output is a float array of length n.
2. Output index `i` is computed **only** from input indices `<= i`. No function
   centres a window, back-fills, or otherwise reaches forward. `tests/test_no_lookahead.py`
   enforces this by truncating the input and asserting the prefix is unchanged.
3. Warm-up positions are `np.nan`, never zero and never back-filled. A caller
   that needs a value must check for nan; it must not receive a fabricated one.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "sma",
    "ema",
    "rsi",
    "macd",
    "true_range",
    "atr",
    "bollinger",
    "bollinger_width",
    "rolling_vwap",
    "session_vwap",
    "roc",
    "momentum",
    "rolling_max",
    "rolling_min",
    "rolling_std",
    "linreg_slope",
    "consecutive_up",
]


def _as_f64(x: np.ndarray) -> np.ndarray:
    a = np.asarray(x, dtype=np.float64)
    if a.ndim != 1:
        raise ValueError("expected a 1-D array")
    return a


def _nan_prefix(n: int, warmup: int) -> np.ndarray:
    out = np.full(n, np.nan, dtype=np.float64)
    return out if warmup < n else out


# --------------------------------------------------------------------------- #
# Moving averages
# --------------------------------------------------------------------------- #


def sma(values: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average. out[i] = mean(values[i-period+1 : i+1])."""
    v = _as_f64(values)
    n = v.size
    out = _nan_prefix(n, period - 1)
    if period <= 0:
        raise ValueError("period must be positive")
    if n < period:
        return out
    csum = np.cumsum(np.insert(v, 0, 0.0))
    out[period - 1 :] = (csum[period:] - csum[:-period]) / period
    return out


def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average, seeded with the SMA of the first `period` bars.

    Seeding with an SMA (rather than the first value) is the convention used by
    most charting platforms, so levels here line up with what the user sees on
    TradingView.
    """
    v = _as_f64(values)
    n = v.size
    out = _nan_prefix(n, period - 1)
    if period <= 0:
        raise ValueError("period must be positive")
    if n < period:
        return out
    alpha = 2.0 / (period + 1.0)
    acc = float(np.mean(v[:period]))
    out[period - 1] = acc
    for i in range(period, n):
        acc = alpha * v[i] + (1 - alpha) * acc
        out[i] = acc
    return out


def _rma(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing (used by RSI and ATR). alpha = 1/period."""
    v = _as_f64(values)
    n = v.size
    out = _nan_prefix(n, period - 1)
    if n < period:
        return out
    acc = float(np.mean(v[:period]))
    out[period - 1] = acc
    for i in range(period, n):
        acc = (acc * (period - 1) + v[i]) / period
        out[i] = acc
    return out


# --------------------------------------------------------------------------- #
# Oscillators
# --------------------------------------------------------------------------- #


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder RSI. Returns nan until `period` deltas exist."""
    c = _as_f64(close)
    n = c.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period + 1:
        return out
    delta = np.diff(c)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = _rma(gains, period)
    avg_loss = _rma(losses, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss > 0, avg_gain / avg_loss, np.inf)
        rsi_vals = np.where(np.isfinite(rs), 100.0 - 100.0 / (1.0 + rs), 100.0)
    rsi_vals = np.where(np.isnan(avg_gain) | np.isnan(avg_loss), np.nan, rsi_vals)
    # delta[j] corresponds to close[j+1]
    out[1:] = rsi_vals
    return out


def macd(
    close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (macd_line, signal_line, histogram)."""
    c = _as_f64(close)
    fast_e = ema(c, fast)
    slow_e = ema(c, slow)
    line = fast_e - slow_e
    valid = ~np.isnan(line)
    sig = np.full(c.size, np.nan, dtype=np.float64)
    if valid.any():
        first = int(np.argmax(valid))
        sub = ema(line[first:], signal)
        sig[first:] = sub
    return line, sig, line - sig


# --------------------------------------------------------------------------- #
# Volatility
# --------------------------------------------------------------------------- #


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    h, l, c = _as_f64(high), _as_f64(low), _as_f64(close)
    n = h.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n == 0:
        return out
    out[0] = h[0] - l[0]
    if n > 1:
        prev_close = c[:-1]
        out[1:] = np.maximum.reduce(
            [h[1:] - l[1:], np.abs(h[1:] - prev_close), np.abs(l[1:] - prev_close)]
        )
    return out


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder ATR in price units."""
    return _rma(true_range(high, low, close), period)


def bollinger(
    close: np.ndarray, period: int = 20, num_std: float = 2.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (upper, middle, lower). Population std, matching charting defaults."""
    c = _as_f64(close)
    mid = sma(c, period)
    sd = rolling_std(c, period)
    return mid + num_std * sd, mid, mid - num_std * sd


def bollinger_width(close: np.ndarray, period: int = 20, num_std: float = 2.0) -> np.ndarray:
    """(upper - lower) / middle. Unitless, so it is comparable across assets."""
    upper, mid, lower = bollinger(close, period, num_std)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(np.abs(mid) > 0, (upper - lower) / mid, np.nan)


def rolling_std(values: np.ndarray, period: int) -> np.ndarray:
    v = _as_f64(values)
    n = v.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return out
    c1 = np.cumsum(np.insert(v, 0, 0.0))
    c2 = np.cumsum(np.insert(v * v, 0, 0.0))
    s = c1[period:] - c1[:-period]
    s2 = c2[period:] - c2[:-period]
    var = np.maximum(s2 / period - (s / period) ** 2, 0.0)
    out[period - 1 :] = np.sqrt(var)
    return out


# --------------------------------------------------------------------------- #
# VWAP
# --------------------------------------------------------------------------- #


def rolling_vwap(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray, period: int = 20
) -> np.ndarray:
    """Volume-weighted average typical price over a trailing window.

    A rolling window (rather than a session anchor) keeps the measure meaningful
    for a 24/7 market where "the session" is arbitrary.
    """
    h, l, c, v = _as_f64(high), _as_f64(low), _as_f64(close), _as_f64(volume)
    tp = (h + l + c) / 3.0
    pv = tp * v
    n = c.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return out
    cpv = np.cumsum(np.insert(pv, 0, 0.0))
    cv = np.cumsum(np.insert(v, 0, 0.0))
    num = cpv[period:] - cpv[:-period]
    den = cv[period:] - cv[:-period]
    with np.errstate(divide="ignore", invalid="ignore"):
        out[period - 1 :] = np.where(den > 0, num / den, np.nan)
    return out


def session_vwap(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray, open_time_ms: np.ndarray
) -> np.ndarray:
    """VWAP anchored at each UTC day boundary."""
    h, l, c, v = _as_f64(high), _as_f64(low), _as_f64(close), _as_f64(volume)
    t = np.asarray(open_time_ms, dtype=np.int64)
    tp = (h + l + c) / 3.0
    day = t // 86_400_000
    out = np.full(c.size, np.nan, dtype=np.float64)
    cum_pv = 0.0
    cum_v = 0.0
    current = None
    for i in range(c.size):
        if current is None or day[i] != current:
            current = day[i]
            cum_pv = 0.0
            cum_v = 0.0
        cum_pv += tp[i] * v[i]
        cum_v += v[i]
        out[i] = cum_pv / cum_v if cum_v > 0 else np.nan
    return out


# --------------------------------------------------------------------------- #
# Rate of change / momentum
# --------------------------------------------------------------------------- #


def roc(values: np.ndarray, period: int) -> np.ndarray:
    """Percent change over `period` bars, expressed in percent."""
    v = _as_f64(values)
    n = v.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n <= period:
        return out
    prev = v[:-period]
    with np.errstate(divide="ignore", invalid="ignore"):
        out[period:] = np.where(prev != 0, (v[period:] - prev) / np.abs(prev) * 100.0, np.nan)
    return out


def momentum(values: np.ndarray, period: int) -> np.ndarray:
    """Absolute difference over `period` bars."""
    v = _as_f64(values)
    out = np.full(v.size, np.nan, dtype=np.float64)
    if v.size > period:
        out[period:] = v[period:] - v[:-period]
    return out


# --------------------------------------------------------------------------- #
# Rolling helpers
# --------------------------------------------------------------------------- #


def rolling_max(values: np.ndarray, period: int) -> np.ndarray:
    v = _as_f64(values)
    n = v.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(v, period)
    out[period - 1 :] = windows.max(axis=1)
    return out


def rolling_min(values: np.ndarray, period: int) -> np.ndarray:
    v = _as_f64(values)
    n = v.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(v, period)
    out[period - 1 :] = windows.min(axis=1)
    return out


def linreg_slope(values: np.ndarray, period: int) -> np.ndarray:
    """Least-squares slope over a trailing window, in units per bar."""
    v = _as_f64(values)
    n = v.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period or period < 2:
        return out
    x = np.arange(period, dtype=np.float64)
    x_mean = x.mean()
    x_dev = x - x_mean
    denom = float(np.sum(x_dev**2))
    windows = np.lib.stride_tricks.sliding_window_view(v, period)
    y_mean = windows.mean(axis=1, keepdims=True)
    out[period - 1 :] = (windows - y_mean) @ x_dev / denom
    return out


def consecutive_up(close: np.ndarray, open_: np.ndarray | None = None) -> int:
    """Number of consecutive green candles ending at the last bar.

    Green means close > open when opens are supplied, otherwise close > prior close.
    """
    c = _as_f64(close)
    if c.size == 0:
        return 0
    if open_ is not None:
        o = _as_f64(open_)
        green = c > o
    else:
        green = np.empty(c.size, dtype=bool)
        green[0] = False
        green[1:] = c[1:] > c[:-1]
    count = 0
    for i in range(c.size - 1, -1, -1):
        if green[i]:
            count += 1
        else:
            break
    return count
