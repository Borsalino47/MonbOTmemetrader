"""Opportunity score components.

Every component returns a `ComponentScore` carrying its points, its maximum, and
the human-readable reasons that produced those points. The dashboard renders
those reasons verbatim — there is no path by which a score reaches the user
without its explanation.

Two rules hold throughout:

* Unknown is not zero-credit-with-a-guess, it is zero-credit-and-say-so. A
  component whose inputs are missing appends an `unavailable` reason and returns
  0 points, and data confidence drops separately.
* Nothing here uses an absolute threshold that assumes a price scale. Distances
  are ATR multiples, volumes are ratios, changes are percentages.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.config.settings import ScoringSettings
from cryptopulse.core.types import Timeframe
from cryptopulse.features.pipeline import AssetFeatures, Bias
from cryptopulse.features.stats import clamp01, scale
from cryptopulse.features.structure import StructureTrend
from cryptopulse.i18n import money, mult, num, pct
from cryptopulse.i18n import reasons as R

__all__ = ["ComponentScore", "score_volume", "score_momentum", "score_structure", "score_breakout",
           "score_volatility", "score_orderflow", "score_mtf", "score_liquidity"]


@dataclass(slots=True)
class ComponentScore:
    name: str
    points: float
    max_points: float
    reasons: list[str] = field(default_factory=list)
    # Statements that explain the score but argue *against* the setup. Kept apart
    # from `reasons` so "Why this asset?" cannot fill up with warnings.
    caveats: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)
    available: bool = True

    @property
    def fraction(self) -> float:
        return self.points / self.max_points if self.max_points else 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "points": round(self.points, 2),
            "max_points": self.max_points,
            "fraction": round(self.fraction, 4),
            "available": self.available,
            "reasons": self.reasons,
            "caveats": self.caveats,
            "detail": self.detail,
        }


def _mk(name: str, max_points: float) -> ComponentScore:
    return ComponentScore(name=name, points=0.0, max_points=max_points)


# --------------------------------------------------------------------------- #
# 1. Volume & volume acceleration (20 pts)
# --------------------------------------------------------------------------- #


def score_volume(af: AssetFeatures, cfg: ScoringSettings) -> ComponentScore:
    """Rewards volume that is *both* elevated and still building.

    Split 40/40/20 between level (RVOL), acceleration (short vs medium average),
    and trend (is RVOL itself rising bar over bar). The last term is what
    separates 1.0→2.6 from a flat 2.5.
    """
    cs = _mk("volume", cfg.w_volume)
    f = af.primary

    if f.rvol is None and f.volume_acceleration_pct is None:
        cs.available = False
        cs.reasons.append(R.VOLUME_UNAVAILABLE())
        return cs

    level = 0.0
    if f.rvol is not None:
        # 1.0x earns nothing, 3.5x saturates.
        level = scale(f.rvol, 1.0, 3.5)
        cs.detail["rvol"] = round(f.rvol, 3)
        if f.rvol >= 2.0:
            cs.reasons.append(R.RVOL_HIGH(rvol=mult(f.rvol), bars=50))
        elif f.rvol >= 1.3:
            cs.reasons.append(R.RVOL_MILD(rvol=mult(f.rvol)))
        else:
            cs.caveats.append(R.RVOL_LOW(rvol=mult(f.rvol)))

    accel = 0.0
    if f.volume_acceleration_pct is not None:
        accel = scale(f.volume_acceleration_pct, 0.0, 90.0)
        cs.detail["volume_acceleration_pct"] = round(f.volume_acceleration_pct, 2)
        if f.volume_acceleration_pct >= 25:
            cs.reasons.append(R.VOLUME_ACCELERATING(change=pct(f.volume_acceleration_pct), bars=12))

    trend = 0.0
    if f.rvol_slope is not None:
        trend = scale(f.rvol_slope, 0.0, 0.45)
        cs.detail["rvol_slope"] = round(f.rvol_slope, 4)
        if f.rvol_slope > 0.1:
            cs.reasons.append(R.RVOL_RISING())
    if f.volume_zscore is not None:
        cs.detail["volume_zscore"] = round(f.volume_zscore, 2)

    cs.points = cs.max_points * clamp01(0.40 * level + 0.40 * accel + 0.20 * trend)
    return cs


# --------------------------------------------------------------------------- #
# 2. Momentum (15 pts)
# --------------------------------------------------------------------------- #


def score_momentum(af: AssetFeatures, cfg: ScoringSettings) -> ComponentScore:
    """Directional push on the primary timeframe.

    Uses rate of change, MACD histogram direction and position versus EMA20/VWAP.
    Note what is *not* here: raw 24h change. A coin already up 80% on the day has
    momentum, but that belongs to pump maturity, not opportunity.
    """
    cs = _mk("momentum", cfg.w_momentum)
    f = af.primary
    parts: list[float] = []

    if f.roc3 is not None:
        # Modest, sustained push scores best; the extreme end is maturity's problem.
        parts.append(scale(f.roc3, 0.0, 3.0))
        cs.detail["roc3_pct"] = round(f.roc3, 3)
        if f.roc3 > 0.5:
            cs.reasons.append(R.PRICE_ROC_UP(change=pct(f.roc3, 2), bars=3))
        elif f.roc3 < 0:
            cs.caveats.append(R.PRICE_ROC_DOWN(change=pct(f.roc3, 2), bars=3))

    if f.macd_hist is not None:
        rising = f.macd_hist_prev is not None and f.macd_hist > f.macd_hist_prev
        v = 0.0
        if f.macd_hist > 0:
            v += 0.6
        if rising:
            v += 0.4
            cs.reasons.append(R.MACD_EXPANDING())
        parts.append(clamp01(v))
        cs.detail["macd_hist"] = round(f.macd_hist, 8)

    if f.ema20_distance_pct is not None:
        # Above EMA20 is good; far above is extension, not momentum.
        d = f.ema20_distance_pct
        parts.append(scale(d, -0.5, 2.0) if d <= 2.0 else max(0.3, 1.0 - scale(d, 2.0, 8.0)))
        cs.detail["ema20_distance_pct"] = round(d, 3)

    if f.rsi14 is not None:
        cs.detail["rsi14"] = round(f.rsi14, 2)
        # 50-68 is the constructive band. Below 45 lacks push, above 78 is stretched.
        if 50 <= f.rsi14 <= 68:
            parts.append(1.0)
            cs.reasons.append(R.RSI_CONSTRUCTIVE(rsi=num(f.rsi14, 0)))
        elif f.rsi14 > 78:
            parts.append(0.2)
            cs.caveats.append(R.RSI_STRETCHED(rsi=num(f.rsi14, 0)))
        else:
            parts.append(scale(f.rsi14, 35.0, 50.0) * 0.6)

    if not parts:
        cs.available = False
        cs.reasons.append(R.MOMENTUM_UNAVAILABLE())
        return cs

    cs.points = cs.max_points * clamp01(sum(parts) / len(parts))
    if not cs.reasons:
        cs.caveats.append(R.MOMENTUM_FLAT())
    return cs


# --------------------------------------------------------------------------- #
# 3. Structure (15 pts)
# --------------------------------------------------------------------------- #


def score_structure(af: AssetFeatures, cfg: ScoringSettings) -> ComponentScore:
    """Is the price map underneath this move constructive?"""
    cs = _mk("structure", cfg.w_structure)
    f = af.primary
    st = f.structure
    if st is None or st.notes == ["INSUFFICIENT_HISTORY"]:
        cs.available = False
        cs.reasons.append(R.STRUCTURE_UNAVAILABLE())
        return cs

    parts: list[float] = []

    trend_value = {
        StructureTrend.UPTREND: 1.0,
        StructureTrend.RANGE: 0.55,
        StructureTrend.UNCLEAR: 0.4,
        StructureTrend.DOWNTREND: 0.0,
    }[st.trend]
    parts.append(trend_value)
    cs.detail["trend"] = st.trend.value
    if st.trend is StructureTrend.UPTREND:
        cs.reasons.append(R.STRUCTURE_UP())
    elif st.trend is StructureTrend.DOWNTREND:
        cs.caveats.append(R.STRUCTURE_DOWN())

    if st.range_position is not None:
        cs.detail["range_position"] = round(st.range_position, 3)
        # Upper-middle of the range is where breakout attempts originate. The very
        # top means the move already happened inside this window.
        rp = st.range_position
        parts.append(1.0 if 0.55 <= rp <= 0.90 else (0.5 if rp > 0.90 else scale(rp, 0.15, 0.55)))
        if rp > 0.90:
            cs.caveats.append(R.RANGE_TOP())

    if st.nearest_support is not None and st.distance_to_support_atr is not None:
        cs.detail["distance_to_support_atr"] = round(st.distance_to_support_atr, 3)
        # A support close underneath gives the setup a definable invalidation.
        parts.append(1.0 - scale(st.distance_to_support_atr, 0.5, 5.0))
        d_sup = st.distance_to_support_atr
        if d_sup <= 3.0:
            cs.reasons.append(R.SUPPORT_CLOSE(distance=num(d_sup)))
        else:
            cs.caveats.append(R.SUPPORT_FAR(distance=num(d_sup, 1)))

    if st.fake_breakout:
        parts.append(0.0)
        cs.caveats.append(R.FAILED_BREAKOUT_RECENT())
    if st.in_retest:
        parts.append(1.0)
        cs.reasons.append(R.RETEST_HOLDING())

    cs.points = cs.max_points * clamp01(sum(parts) / len(parts))
    return cs


# --------------------------------------------------------------------------- #
# 4. Breakout proximity (15 pts)
# --------------------------------------------------------------------------- #


def score_breakout(af: AssetFeatures, cfg: ScoringSettings) -> ComponentScore:
    """Distance to the level that matters, in ATR.

    This is the component that lets the scanner fire *before* the candle everyone
    else is looking at. Peak credit sits just under the level; credit falls away
    both as price runs past it and as it drifts too far below to be relevant.
    """
    cs = _mk("breakout", cfg.w_breakout)
    f = af.primary
    st = f.structure
    if st is None or st.atr_value is None:
        cs.available = False
        cs.reasons.append(R.BREAKOUT_UNAVAILABLE())
        return cs

    if st.broke_out:
        cs.points = cs.max_points * 0.80
        cs.detail["state"] = "broken"
        cs.reasons.append(R.LEVEL_BROKEN())
        return cs

    d = st.distance_to_resistance_atr
    if d is None:
        # No resistance overhead is genuinely bullish, but it also means there is
        # no measurable trigger, so this scores mid rather than max.
        cs.points = cs.max_points * 0.55
        cs.detail["state"] = "no_overhead_resistance"
        cs.reasons.append(R.NO_RESISTANCE())
        return cs

    cs.detail["distance_to_resistance_atr"] = round(d, 3)
    cs.detail["resistance"] = st.nearest_resistance.price if st.nearest_resistance else None

    # 0 ATR away -> 1.0, 2.0 ATR away -> 0.
    proximity = 1.0 - scale(d, 0.0, 2.0)
    strength = st.nearest_resistance.strength if st.nearest_resistance else 0.5
    # A level nobody has tested is a weak trigger even if it is close.
    cs.points = cs.max_points * clamp01(0.75 * proximity + 0.25 * strength)

    if d <= cfg.armed_max_distance_atr:
        cs.reasons.append(R.BELOW_RESISTANCE(distance=num(d), touches=st.nearest_resistance.touches))
    else:
        cs.caveats.append(R.RESISTANCE_FAR(distance=num(d)))
    return cs


# --------------------------------------------------------------------------- #
# 5. Volatility expansion (10 pts)
# --------------------------------------------------------------------------- #


def score_volatility(af: AssetFeatures, cfg: ScoringSettings) -> ComponentScore:
    """Compression that is starting to release.

    Maximum credit is *not* for the tightest band — it is for a band that was
    tight and is now widening on rising volume. Pure compression with no volume
    is a coiled spring nobody is touching yet.
    """
    cs = _mk("volatility", cfg.w_volatility)
    f = af.primary
    if f.compression is None:
        cs.available = False
        cs.reasons.append(R.VOLATILITY_UNAVAILABLE())
        return cs

    cs.detail["compression_percentile"] = round(f.compression, 3)
    # compression is a percentile: low = tight.
    was_tight = 1.0 - f.compression
    parts = [was_tight]
    if f.compression <= 0.25:
        cs.reasons.append(R.BB_COMPRESSED(percent=pct(f.compression * 100, 0).lstrip("+")))

    if f.volume_acceleration_pct is not None:
        release = scale(f.volume_acceleration_pct, 0.0, 60.0)
        parts.append(release)
        if f.compression <= 0.35 and f.volume_acceleration_pct > 20:
            cs.reasons.append(R.COMPRESSION_RELEASING())

    if f.range_percentile is not None:
        cs.detail["range_percentile"] = round(f.range_percentile, 3)

    cs.points = cs.max_points * clamp01(sum(parts) / len(parts))
    if not cs.reasons:
        pctile = num(f.compression * 100, 0)
        text = R.BB_NONE(percentile=pctile) if f.compression > 0.6 else R.BB_PARTIAL(percentile=pctile)
        (cs.caveats if f.compression > 0.6 else cs.reasons).append(text)
    return cs


# --------------------------------------------------------------------------- #
# 6. Order flow (10 pts)
# --------------------------------------------------------------------------- #


def score_orderflow(af: AssetFeatures, cfg: ScoringSettings) -> ComponentScore:
    """Order book imbalance, when a book was actually fetched.

    If no book is available this component is marked unavailable and contributes
    nothing — it does not silently fall back to a candle-based proxy, because a
    proxy would be indistinguishable from real order flow in the UI.
    """
    cs = _mk("orderflow", cfg.w_orderflow)
    if af.order_book_imbalance is None:
        cs.available = False
        cs.reasons.append(R.BOOK_UNAVAILABLE())
        return cs

    imb = af.order_book_imbalance
    cs.detail["imbalance"] = round(imb, 4)
    cs.points = cs.max_points * scale(imb, -0.15, 0.45)
    if imb > 0.15:
        cs.reasons.append(R.BID_DOMINANT(imbalance=pct(imb * 100, 0)))
    elif imb < -0.15:
        cs.caveats.append(R.ASK_DOMINANT(imbalance=pct(imb * 100, 0)))
    else:
        cs.reasons.append(R.BOOK_BALANCED())

    if af.spread_bps is not None:
        cs.detail["spread_bps"] = round(af.spread_bps, 2)
    return cs


# --------------------------------------------------------------------------- #
# 7. Multi-timeframe alignment (10 pts)
# --------------------------------------------------------------------------- #

# Higher timeframes carry more weight: a 5m signal against a bearish 4h is a
# fundamentally different trade from the same 5m signal with the 4h agreeing.
_TF_WEIGHTS: dict[Timeframe, float] = {
    Timeframe.M1: 0.5,
    Timeframe.M5: 1.0,
    Timeframe.M15: 1.5,
    Timeframe.H1: 2.0,
    Timeframe.H4: 2.5,
    Timeframe.D1: 2.0,
}

_BIAS_VALUE: dict[Bias, float] = {
    Bias.BULLISH: 1.0,
    Bias.NEUTRAL: 0.5,
    Bias.BEARISH: 0.0,
    Bias.UNKNOWN: 0.5,
}


def score_mtf(af: AssetFeatures, cfg: ScoringSettings) -> ComponentScore:
    cs = _mk("mtf", cfg.w_mtf)
    known = {tf: f for tf, f in af.timeframes.items() if f.bias is not Bias.UNKNOWN}
    if len(known) < 2:
        cs.available = False
        cs.reasons.append(R.MTF_TOO_FEW())
        return cs

    total_w = 0.0
    acc = 0.0
    per_tf: dict[str, str] = {}
    for tf, f in known.items():
        w = _TF_WEIGHTS.get(tf, 1.0)
        total_w += w
        acc += w * _BIAS_VALUE[f.bias]
        per_tf[tf.value] = f.bias.value

    alignment = acc / total_w if total_w else 0.5
    cs.detail["per_timeframe"] = per_tf
    cs.detail["alignment"] = round(alignment, 4)
    cs.points = cs.max_points * clamp01(alignment)

    bulls = [tf.value for tf, f in known.items() if f.bias is Bias.BULLISH]
    bears = [tf.value for tf, f in known.items() if f.bias is Bias.BEARISH]
    if bulls:
        cs.reasons.append(R.MTF_BULLISH(timeframes=", ".join(sorted(bulls))))
    if bears:
        cs.caveats.append(R.MTF_BEARISH(timeframes=", ".join(sorted(bears))))
    if not bulls and not bears:
        cs.caveats.append(R.MTF_NEUTRAL(count=len(known)))
    return cs


# --------------------------------------------------------------------------- #
# 8. Market quality / liquidity (5 pts)
# --------------------------------------------------------------------------- #


def score_liquidity(af: AssetFeatures, cfg: ScoringSettings, liquidity_fraction: float | None) -> ComponentScore:
    """Small contribution by design — liquidity's real job is the gate, not the score."""
    cs = _mk("liquidity", cfg.w_liquidity)
    if liquidity_fraction is None:
        cs.available = False
        cs.reasons.append(R.LIQUIDITY_UNAVAILABLE())
        return cs
    cs.points = cs.max_points * clamp01(liquidity_fraction)
    cs.detail["liquidity_fraction"] = round(liquidity_fraction, 3)
    if af.quote_volume_24h:
        cs.reasons.append(R.LIQUIDITY_VOLUME_24H(volume=money(af.quote_volume_24h)))
    else:
        cs.reasons.append(R.LIQUIDITY_NO_VOLUME())
    return cs
