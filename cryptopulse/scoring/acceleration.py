"""Early-move detection: derivatives, not levels.

The premise of the whole product. A coin sitting at RVOL 2.5 for six hours has
been found by everybody. A coin whose RVOL went 1.0 → 1.2 → 1.5 → 1.9 → 2.6 in
the last five bars is changing behaviour right now.

Two scores come out of here:

* `MOMENTUM_ACCELERATION` — how fast the inputs are changing.
* `EARLY_MOVE` — acceleration that has *not yet* been paid out in price, i.e.
  the intersection of "something is changing" and "price has not moved far".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.features.pipeline import AssetFeatures
from cryptopulse.features.stats import clamp01, scale
from cryptopulse.scoring.pump_maturity import PumpMaturity

__all__ = ["AccelerationScores", "compute_acceleration"]


@dataclass(slots=True)
class AccelerationScores:
    momentum_acceleration: float  # 0..100
    early_move: float  # 0..100
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "momentum_acceleration": round(self.momentum_acceleration, 1),
            "early_move": round(self.early_move, 1),
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "reasons": self.reasons,
        }


def compute_acceleration(af: AssetFeatures, maturity: PumpMaturity) -> AccelerationScores:
    f = af.primary
    parts: dict[str, float] = {}
    reasons: list[str] = []

    # Volume derivative: is the volume average itself rising?
    if f.volume_acceleration_pct is not None:
        parts["volume_derivative"] = scale(f.volume_acceleration_pct, 5.0, 80.0)
    # Second derivative: is RVOL climbing bar over bar?
    if f.rvol_slope is not None:
        parts["rvol_slope"] = scale(f.rvol_slope, 0.02, 0.5)
        if f.rvol_slope > 0.15:
            reasons.append("RVOL climbing bar over bar")
    if f.rvol is not None and f.rvol_prev is not None and f.rvol_prev > 0:
        step = f.rvol / f.rvol_prev - 1.0
        parts["rvol_step"] = scale(step, 0.05, 0.75)

    # Price acceleration: latest bar outpacing the recent average bar.
    if f.roc1 is not None and f.roc3 is not None:
        avg_bar = f.roc3 / 3.0
        if abs(avg_bar) > 1e-9:
            parts["price_acceleration"] = scale(f.roc1 - avg_bar, 0.0, 0.8)
        else:
            parts["price_acceleration"] = scale(f.roc1, 0.0, 0.8)

    # MACD histogram expanding = momentum of momentum.
    if f.macd_hist is not None and f.macd_hist_prev is not None:
        delta = f.macd_hist - f.macd_hist_prev
        ref = abs(f.macd_hist_prev) if abs(f.macd_hist_prev) > 1e-12 else abs(f.macd_hist) + 1e-12
        parts["macd_expansion"] = scale(delta / ref, 0.0, 1.0)

    # Volatility waking up out of compression.
    if f.compression is not None:
        parts["volatility_release"] = clamp01(1.0 - f.compression) * 0.7
        if f.compression < 0.2:
            reasons.append("volatility compressed and beginning to release")

    if not parts:
        return AccelerationScores(0.0, 0.0, reasons=["insufficient history for acceleration"])

    momentum_accel = 100.0 * clamp01(sum(parts.values()) / len(parts))

    # Early move = acceleration that price has not yet fully expressed.
    # A high maturity means the market already noticed, so it discounts hard.
    earliness = clamp01(1.0 - maturity.score / 100.0)
    proximity_bonus = 0.0
    st = f.structure
    if st and st.distance_to_resistance_atr is not None:
        proximity_bonus = 0.15 * (1.0 - scale(st.distance_to_resistance_atr, 0.0, 2.0))

    early = momentum_accel * (0.55 + 0.45 * earliness) + 100.0 * proximity_bonus
    early = min(100.0, early)

    if momentum_accel > 60 and maturity.score < 45:
        reasons.append("behaviour changing while the move is still young")

    return AccelerationScores(
        momentum_acceleration=momentum_accel,
        early_move=early,
        components=parts,
        reasons=reasons,
    )
