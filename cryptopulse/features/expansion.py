"""Expansion features — the readings that precede a *large multiple*, not a 3% move.

WHY THIS MODULE IS SEPARATE FROM `structure.py`

`structure.py` answers "is this a good setup right now", on the primary
timeframe, over tens of bars. Nothing in it can see a base that has been forming
for four months, a token that trades 92% below a price it actually printed last
year, or volume arriving while price refuses to move. Those are the readings that
distinguish a candidate for a *ten-fold* move from a candidate for a good day,
and they live on the daily chart over hundreds of bars.

WHAT IS HONEST ABOUT THESE NUMBERS

Every value here is measured from the candles supplied and nothing else:

* `multiple_to_window_high` is not a forecast. It is arithmetic: the highest high
  in the supplied history divided by the current close. When it reads 11.4x, the
  only claim being made is that this asset traded 11.4x higher inside this
  window. Whether it ever does so again is not knowable from a chart.
* `base_length_bars` counts bars, it does not predict a breakout.
* `quiet_accumulation` is a description of a tape (volume building, price flat,
  closes in the upper half of their ranges). It is a pattern, not an intention —
  no candle tells you who is buying or why.

Anything that cannot be computed from the bars given is `None`, never a default.

NO LOOK-AHEAD

Every function reads a trailing window ending at the last supplied bar, so a
report computed over `series[:k]` is identical whether or not bars after `k`
exist. `tests/test_no_lookahead.py` asserts exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from cryptopulse.features.indicators import (
    accumulation_distribution,
    chaikin_money_flow,
    linreg_slope,
    obv,
)
from cryptopulse.features.structure import SwingKind, find_swings

__all__ = ["ExpansionReport", "analyse_expansion", "base_run_length", "contraction_sequence"]

# A base is "one range" while its full height stays inside this many ATR. Wider
# than a breakout box on purpose: a multi-month accumulation range breathes.
DEFAULT_BASE_MAX_WIDTH_ATR = 6.0
DEFAULT_BASE_MAX_BARS = 260

# Long-term level the price has to clear for `broke_prior_high`. 60 daily bars is
# roughly a quarter — long enough that clearing it is news, short enough that it
# can be measured on the history most venues return.
DEFAULT_PRIOR_HIGH_LOOKBACK = 60


@dataclass(slots=True)
class ExpansionReport:
    """One timeframe's long-horizon reading. Every field is optional by design."""

    bars: int
    window_high: float | None = None
    window_low: float | None = None
    bars_since_window_high: int | None = None
    drawdown_from_high_pct: float | None = None  # negative: -87.5 means 87.5% below
    multiple_to_window_high: float | None = None  # window_high / close

    base_length_bars: int | None = None
    base_high: float | None = None
    base_low: float | None = None
    base_width_pct: float | None = None
    broke_base_high: bool = False

    prior_high_lookback: int = DEFAULT_PRIOR_HIGH_LOOKBACK
    prior_high: float | None = None
    broke_prior_high: bool = False

    volume_regime_ratio: float | None = None  # recent volume vs its older median
    volume_slope_norm: float | None = None
    price_drift_pct: float | None = None
    quiet_accumulation: bool = False

    cmf: float | None = None
    ad_slope_norm: float | None = None
    obv_slope_norm: float | None = None

    contractions: list[float] = field(default_factory=list)
    vcp: bool = False
    spring: bool = False

    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "bars": self.bars,
            "window_high": self.window_high,
            "window_low": self.window_low,
            "bars_since_window_high": self.bars_since_window_high,
            "drawdown_from_high_pct": _r(self.drawdown_from_high_pct, 2),
            "multiple_to_window_high": _r(self.multiple_to_window_high, 3),
            "base_length_bars": self.base_length_bars,
            "base_high": self.base_high,
            "base_low": self.base_low,
            "base_width_pct": _r(self.base_width_pct, 2),
            "broke_base_high": self.broke_base_high,
            "prior_high": self.prior_high,
            "prior_high_lookback": self.prior_high_lookback,
            "broke_prior_high": self.broke_prior_high,
            "volume_regime_ratio": _r(self.volume_regime_ratio, 3),
            "volume_slope_norm": _r(self.volume_slope_norm, 4),
            "price_drift_pct": _r(self.price_drift_pct, 3),
            "quiet_accumulation": self.quiet_accumulation,
            "cmf": _r(self.cmf, 4),
            "ad_slope_norm": _r(self.ad_slope_norm, 5),
            "obv_slope_norm": _r(self.obv_slope_norm, 5),
            "contractions": [round(c, 2) for c in self.contractions],
            "vcp": self.vcp,
            "spring": self.spring,
            "notes": self.notes,
        }


def _r(x: float | None, nd: int) -> float | None:
    return None if x is None else round(x, nd)


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #


def base_run_length(
    high: np.ndarray,
    low: np.ndarray,
    atr_value: float,
    *,
    max_width_atr: float = DEFAULT_BASE_MAX_WIDTH_ATR,
    max_bars: int = DEFAULT_BASE_MAX_BARS,
) -> tuple[int, float, float]:
    """How many trailing bars fit inside one range, and that range.

    Walks backwards accumulating the running high and low, and stops at the bar
    that would make the range taller than `max_width_atr` ATR. The answer is the
    length of the *current* base — the thing that has been building — rather than
    the best base anywhere in history, because only the current one can break.

    Returns (bars, base_high, base_low). Zero bars means no measurable base.
    """
    h = np.asarray(high, dtype=np.float64)
    l = np.asarray(low, dtype=np.float64)
    n = h.size
    if n == 0 or not np.isfinite(atr_value) or atr_value <= 0:
        return 0, float("nan"), float("nan")

    ceiling = max_width_atr * atr_value
    hi = -np.inf
    lo = np.inf
    count = 0
    best_hi = best_lo = float("nan")
    for i in range(n - 1, max(-1, n - 1 - max_bars), -1):
        new_hi = max(hi, float(h[i]))
        new_lo = min(lo, float(l[i]))
        if (new_hi - new_lo) > ceiling and count > 0:
            break
        hi, lo = new_hi, new_lo
        count += 1
        best_hi, best_lo = hi, lo
    return count, best_hi, best_lo


def contraction_sequence(
    high: np.ndarray, low: np.ndarray, open_time_ms: np.ndarray, *, max_pullbacks: int = 4
) -> list[float]:
    """Depth of each recent pullback, oldest first, as a positive percentage.

    A pullback is measured from a confirmed swing high to the confirmed swing low
    that follows it. Successively shallower pullbacks are the volatility
    contraction pattern: each wave of selling is absorbed higher than the last,
    which is what a supply shortage looks like on a chart.

    Uses confirmed swings only, so nothing here repaints.
    """
    swings = find_swings(high, low, open_time_ms)
    depths: list[float] = []
    pending_high: float | None = None
    for s in swings:
        if s.kind is SwingKind.HIGH:
            pending_high = s.price
        elif pending_high is not None and s.price > 0 and pending_high > 0:
            depth = (pending_high - s.price) / pending_high * 100.0
            if depth > 0:
                depths.append(depth)
            pending_high = None
    return depths[-max_pullbacks:]


def _normalised_slope(values: np.ndarray, period: int) -> float | None:
    """Least-squares slope over the last `period` bars, divided by the window mean.

    Normalising makes the number comparable between a coin that trades 12 units a
    day and one that trades 12 million. For a cumulative series (A/D, OBV) the
    mean can sit near zero or go negative, so the scale is the mean *absolute*
    level; a slope divided by a near-zero level is not reported at all.
    """
    v = np.asarray(values, dtype=np.float64)
    if v.size < period or period < 2:
        return None
    window = v[-period:]
    if not np.all(np.isfinite(window)):
        return None
    slope = _f(linreg_slope(v, period)[-1])
    if slope is None:
        return None
    scale = float(np.mean(np.abs(window)))
    if scale <= 0 or not np.isfinite(scale):
        return None
    return slope / scale


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def analyse_expansion(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    open_time_ms: np.ndarray,
    *,
    atr_value: float | None = None,
    prior_high_lookback: int = DEFAULT_PRIOR_HIGH_LOOKBACK,
    accumulation_period: int = 20,
    volume_regime_fast: int = 5,
    volume_regime_baseline: int = 30,
) -> ExpansionReport:
    """Long-horizon reading for one timeframe. Intended for D1, works on any."""
    h = np.asarray(high, dtype=np.float64)
    l = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    v = np.asarray(volume, dtype=np.float64)
    n = c.size
    rep = ExpansionReport(bars=n, prior_high_lookback=prior_high_lookback)
    if n == 0:
        rep.notes.append("NO_DATA")
        return rep

    last = float(c[-1])

    # -- where the price sits inside everything we can see ------------------- #
    if n >= 2 and last > 0:
        idx_high = int(np.argmax(h))
        rep.window_high = float(h[idx_high])
        rep.window_low = float(np.min(l))
        rep.bars_since_window_high = n - 1 - idx_high
        if rep.window_high > 0:
            rep.drawdown_from_high_pct = (last - rep.window_high) / rep.window_high * 100.0
            # Arithmetic, not a forecast: the multiple this asset would trade at
            # if it merely returned to a price it already printed in this window.
            rep.multiple_to_window_high = rep.window_high / last

    # -- the base that is actually forming right now ------------------------- #
    if atr_value is not None and np.isfinite(atr_value) and atr_value > 0:
        bars, base_hi, base_lo = base_run_length(h, l, atr_value)
        if bars > 0 and np.isfinite(base_hi) and np.isfinite(base_lo):
            rep.base_length_bars = bars
            rep.base_high = base_hi
            rep.base_low = base_lo
            if base_lo > 0:
                rep.base_width_pct = (base_hi - base_lo) / base_lo * 100.0
            # Only the *closed* bar can break the base, and `base_high` includes
            # the current bar's own high, so compare against the base excluding it.
            if bars >= 2:
                prior_base_high = float(np.max(h[-bars:-1]))
                rep.broke_base_high = last > prior_base_high
    else:
        rep.notes.append("NO_ATR: base geometry unavailable")

    # -- multi-month level ---------------------------------------------------- #
    if n >= prior_high_lookback + 1:
        prior = float(np.max(h[-prior_high_lookback - 1 : -1]))
        rep.prior_high = prior
        rep.broke_prior_high = last > prior
    else:
        rep.notes.append(f"SHORT_HISTORY: fewer than {prior_high_lookback + 1} bars for the long-term level")

    # -- is money arriving? ---------------------------------------------------- #
    need = volume_regime_fast + volume_regime_baseline
    if n >= need:
        recent = v[-volume_regime_fast:]
        older = v[-need:-volume_regime_fast]
        baseline = float(np.median(older))
        if baseline > 0:
            rep.volume_regime_ratio = float(np.mean(recent)) / baseline
    rep.volume_slope_norm = _normalised_slope(v, min(accumulation_period, n))

    if n > accumulation_period:
        ref = float(c[-accumulation_period - 1])
        if ref > 0:
            rep.price_drift_pct = (last - ref) / ref * 100.0

    if n >= accumulation_period:
        rep.cmf = _f(chaikin_money_flow(h, l, c, v, accumulation_period)[-1])
        rep.ad_slope_norm = _normalised_slope(accumulation_distribution(h, l, c, v), accumulation_period)
        rep.obv_slope_norm = _normalised_slope(obv(c, v), accumulation_period)

    # Quiet accumulation: volume building, price going nowhere, and bars closing
    # in the upper half of their ranges. Any two of the three is ordinary; all
    # three together is the tape that precedes a markup often enough to watch for.
    if (
        rep.volume_slope_norm is not None
        and rep.price_drift_pct is not None
        and rep.cmf is not None
        and rep.volume_slope_norm > 0.01
        and abs(rep.price_drift_pct) < 12.0
        and rep.cmf > 0.05
    ):
        rep.quiet_accumulation = True

    # -- contraction pattern and spring --------------------------------------- #
    if n >= 20:
        rep.contractions = contraction_sequence(h, l, np.asarray(open_time_ms))
        if len(rep.contractions) >= 2:
            tightening = all(
                rep.contractions[i] < rep.contractions[i - 1] for i in range(1, len(rep.contractions))
            )
            rep.vcp = tightening and rep.contractions[-1] < 0.7 * rep.contractions[0]

    # Spring / failed breakdown: price dipped under the base floor and closed back
    # above it. Sellers got the break they wanted and it did not hold.
    if rep.base_low is not None and rep.base_length_bars and rep.base_length_bars >= 5:
        window = min(10, rep.base_length_bars)
        floor = float(np.min(l[-rep.base_length_bars : -window])) if rep.base_length_bars > window else rep.base_low
        recent_low = float(np.min(l[-window:]))
        rep.spring = recent_low < floor and last > floor

    return rep
