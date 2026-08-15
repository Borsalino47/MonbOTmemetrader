"""Pump Maturity Score — 0 = move has not started, 100 = move is probably late.

This is the counterweight to the opportunity score. Without it, any momentum
scanner degenerates into a list of things that already went up, which is exactly
the list that gets you filled at the top.

Maturity is a *separate axis*, never blended into opportunity. A setup can be
strong and late at the same time, and the user needs to see both numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.core.types import Timeframe
from cryptopulse.features.pipeline import AssetFeatures
from cryptopulse.features.stats import clamp01, scale

__all__ = ["PumpMaturity", "compute_pump_maturity"]


@dataclass(slots=True)
class PumpMaturity:
    score: float  # 0..100
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    coverage: float = 1.0  # fraction of sub-signals that had data

    @property
    def is_late(self) -> bool:
        return self.score >= 70.0

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "is_late": self.is_late,
            "coverage": round(self.coverage, 3),
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "reasons": self.reasons,
        }


# Each sub-signal contributes a weighted 0..1 "lateness" reading.
_WEIGHTS = {
    "short_change": 1.0,
    "medium_change": 1.2,
    "long_change": 1.0,
    "ema20_extension": 1.3,
    "vwap_extension": 0.8,
    "atr_extension": 1.5,
    "consecutive_green": 0.8,
    "rsi": 1.2,
    "volume_climax": 1.2,
    "range_position": 0.8,
}


def compute_pump_maturity(af: AssetFeatures) -> PumpMaturity:
    f = af.primary
    parts: dict[str, float] = {}
    reasons: list[str] = []

    # -- price change over several horizons -------------------------------- #
    # Thresholds are per-timeframe-agnostic percentages of price. A 6% move on a
    # 5m chart in three bars is a very different thing from 6% over a day, so each
    # horizon gets its own scale.
    if f.roc3 is not None:
        parts["short_change"] = scale(f.roc3, 1.5, 9.0)
        if f.roc3 > 5:
            reasons.append(f"+{f.roc3:.1f}% in the last 3 bars")
    if f.roc12 is not None:
        parts["medium_change"] = scale(f.roc12, 3.0, 20.0)
        if f.roc12 > 12:
            reasons.append(f"+{f.roc12:.1f}% over the last 12 bars")

    h1 = af.get(Timeframe.H1)
    h4 = af.get(Timeframe.H4)
    long_change = None
    if h4 is not None and h4.roc3 is not None:
        long_change = h4.roc3
    elif h1 is not None and h1.roc6 is not None:
        long_change = h1.roc6
    elif af.price_change_pct_24h is not None:
        long_change = af.price_change_pct_24h
    if long_change is not None:
        parts["long_change"] = scale(long_change, 8.0, 55.0)
        if long_change > 30:
            reasons.append(f"already +{long_change:.0f}% on the higher timeframe")

    # -- extension from fair-value anchors ---------------------------------- #
    if f.ema20_distance_pct is not None:
        parts["ema20_extension"] = scale(f.ema20_distance_pct, 1.5, 9.0)
        if f.ema20_distance_pct > 6:
            reasons.append(f"{f.ema20_distance_pct:.1f}% above EMA20")
    if f.vwap_distance_pct is not None:
        parts["vwap_extension"] = scale(f.vwap_distance_pct, 1.0, 7.0)

    # -- extension measured in ATR (the scale-free version of the above) ---- #
    if f.atr14 and f.ema20:
        atr_ext = (f.close - f.ema20) / f.atr14
        parts["atr_extension"] = scale(atr_ext, 1.0, 5.0)
        if atr_ext > 3:
            reasons.append(f"price {atr_ext:.1f} ATR above EMA20 — stretched")

    # -- exhaustion tells ---------------------------------------------------- #
    parts["consecutive_green"] = scale(float(f.consecutive_green), 3.0, 9.0)
    if f.consecutive_green >= 6:
        reasons.append(f"{f.consecutive_green} consecutive green candles")

    if f.rsi14 is not None:
        parts["rsi"] = scale(f.rsi14, 62.0, 85.0)
        if f.rsi14 > 78:
            reasons.append(f"RSI {f.rsi14:.0f}")

    if f.rvol is not None:
        # Climax volume is the classic late tell: enormous participation *after* a run.
        climax = 1.0 if f.volume_climax else scale(f.rvol, 3.0, 7.0)
        # Only counts as late if price has actually moved; huge volume at the base
        # of a range is accumulation, not exhaustion.
        moved = parts.get("medium_change", 0.0)
        parts["volume_climax"] = clamp01(climax * (0.35 + 0.65 * moved))
        if f.volume_climax and moved > 0.3:
            reasons.append("volume climax after an extended move")

    if f.structure and f.structure.range_position is not None:
        parts["range_position"] = scale(f.structure.range_position, 0.75, 1.0)

    if not parts:
        return PumpMaturity(score=0.0, coverage=0.0, reasons=["insufficient data to assess maturity"])

    total_w = sum(_WEIGHTS[k] for k in parts)
    weighted = sum(_WEIGHTS[k] * v for k, v in parts.items())
    score = 100.0 * clamp01(weighted / total_w) if total_w else 0.0
    coverage = total_w / sum(_WEIGHTS.values())

    if score < 25 and not reasons:
        reasons.append("no signs the move is extended")

    return PumpMaturity(score=score, components=parts, reasons=reasons, coverage=coverage)
