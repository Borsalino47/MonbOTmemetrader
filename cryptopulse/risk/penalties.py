"""Risk penalties, computed separately from the raw score.

Kept separate on purpose. Folding risk into the components would hide it: the
user would see 68 and not know whether that is a clean 68 or an 85 with 17 points
of problems. `FINAL = max(0, RAW - PENALTY)` and all three numbers are displayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.config.settings import RiskSettings, ScoringSettings
from cryptopulse.core.types import Timeframe
from cryptopulse.features.pipeline import AssetFeatures, Bias
from cryptopulse.features.stats import scale
from cryptopulse.risk.liquidity import LiquidityAssessment, LiquidityStatus
from cryptopulse.scoring.pump_maturity import PumpMaturity

__all__ = ["Penalty", "PenaltyReport", "compute_penalties"]


@dataclass(slots=True)
class Penalty:
    name: str
    points: float
    reason: str

    def to_dict(self) -> dict:
        return {"name": self.name, "points": round(self.points, 2), "reason": self.reason}


@dataclass(slots=True)
class PenaltyReport:
    total: float
    items: list[Penalty] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"total": round(self.total, 2), "items": [p.to_dict() for p in self.items]}


def compute_penalties(
    af: AssetFeatures,
    maturity: PumpMaturity,
    liq: LiquidityAssessment,
    scoring: ScoringSettings,
    risk: RiskSettings,
) -> PenaltyReport:
    items: list[Penalty] = []
    f = af.primary

    # -- 1. Pump already mature --------------------------------------------- #
    if maturity.score > scoring.pump_maturity_late:
        pts = 18.0 * scale(maturity.score, scoring.pump_maturity_late, 100.0)
        items.append(Penalty("pump_maturity", pts, f"pump maturity {maturity.score:.0f}/100 — move likely late"))
    elif maturity.score > 55:
        items.append(Penalty("pump_maturity", 5.0, f"pump maturity {maturity.score:.0f}/100 — move partly played out"))

    # -- 2. Liquidity -------------------------------------------------------- #
    if liq.status is LiquidityStatus.DANGEROUS:
        items.append(Penalty("liquidity", 20.0, "liquidity DANGEROUS"))
    elif liq.status is LiquidityStatus.POOR:
        items.append(Penalty("liquidity", 10.0, "liquidity POOR"))
    elif liq.status is LiquidityStatus.UNKNOWN:
        items.append(Penalty("liquidity", 4.0, "liquidity could not be assessed"))

    # -- 3. Spread ----------------------------------------------------------- #
    if af.spread_bps is not None and af.spread_bps > risk.spread_acceptable_bps:
        pts = 8.0 * scale(af.spread_bps, risk.spread_acceptable_bps, risk.spread_dangerous_bps)
        items.append(Penalty("spread", pts, f"spread {af.spread_bps:.0f} bps erodes the edge"))

    # -- 4. Volume climax ---------------------------------------------------- #
    if f.volume_climax and maturity.score > 40:
        items.append(Penalty("volume_climax", 7.0, "climax volume after an extended move — exhaustion risk"))

    # -- 5. Higher timeframes against the trade ------------------------------ #
    h1 = af.get(Timeframe.H1)
    h4 = af.get(Timeframe.H4)
    bearish_htf = [
        tf.value for tf, feats in ((Timeframe.H1, h1), (Timeframe.H4, h4)) if feats and feats.bias is Bias.BEARISH
    ]
    if len(bearish_htf) == 2:
        items.append(Penalty("htf_conflict", 12.0, "both 1h and 4h bearish — trading against the higher timeframe"))
    elif len(bearish_htf) == 1:
        items.append(Penalty("htf_conflict", 5.0, f"{bearish_htf[0]} bearish"))

    # -- 6. Major resistance immediately overhead ---------------------------- #
    st = f.structure
    if st and st.distance_to_resistance_atr is not None and st.nearest_resistance is not None:
        d = st.distance_to_resistance_atr
        if d < 0.35 and st.nearest_resistance.touches >= 3:
            items.append(
                Penalty(
                    "overhead_resistance",
                    6.0,
                    f"heavily tested resistance only {d:.2f} ATR overhead ({st.nearest_resistance.touches} touches)",
                )
            )

    # -- 7. Failed breakout -------------------------------------------------- #
    if st and st.fake_breakout:
        items.append(Penalty("fake_breakout", 8.0, "recent breakout attempt failed to hold on close"))

    # -- 8. Irrational volatility -------------------------------------------- #
    if f.atr_pct is not None and f.atr_pct > 6.0:
        pts = 10.0 * scale(f.atr_pct, 6.0, 15.0)
        items.append(Penalty("volatility", pts, f"ATR {f.atr_pct:.1f}% of price per bar — position sizing is punishing"))

    # -- 9. Unstable structure ----------------------------------------------- #
    from cryptopulse.features.structure import StructureTrend

    if st and st.trend is StructureTrend.DOWNTREND:
        items.append(Penalty("structure", 8.0, "structural downtrend on the primary timeframe"))

    # -- 10. RSI divergence proxy -------------------------------------------- #
    # Price making a new high in the window while RSI is well off its own extreme
    # is the classic weakening tell. Reported as a proxy, not a full divergence
    # scan, and named as such.
    if st and st.range_position is not None and f.rsi14 is not None:
        if st.range_position > 0.92 and f.rsi14 < 60:
            items.append(Penalty("momentum_divergence", 5.0, "price at range highs but RSI below 60 (proxy divergence)"))

    total = min(sum(p.points for p in items), risk.max_risk_penalty)
    return PenaltyReport(total=total, items=items)
