"""Liquidity gate — runs before scoring, not after.

An asset you cannot get out of is not an opportunity regardless of how pretty its
chart is. `DANGEROUS` is a hard veto: the asset may still appear in the UI for
information, but it can never be labelled a premium setup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from cryptopulse.config.settings import RiskSettings
from cryptopulse.features.pipeline import AssetFeatures
from cryptopulse.features.stats import clamp01, scale

__all__ = ["LiquidityStatus", "LiquidityAssessment", "assess_liquidity"]


class LiquidityStatus(str, Enum):
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    ACCEPTABLE = "ACCEPTABLE"
    POOR = "POOR"
    DANGEROUS = "DANGEROUS"
    UNKNOWN = "UNKNOWN"

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK = {
    LiquidityStatus.DANGEROUS: 0,
    LiquidityStatus.UNKNOWN: 1,
    LiquidityStatus.POOR: 2,
    LiquidityStatus.ACCEPTABLE: 3,
    LiquidityStatus.GOOD: 4,
    LiquidityStatus.EXCELLENT: 5,
}


@dataclass(slots=True)
class LiquidityAssessment:
    status: LiquidityStatus
    fraction: float | None  # 0..1 for the scoring component
    veto: bool
    reasons: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "fraction": None if self.fraction is None else round(self.fraction, 3),
            "veto": self.veto,
            "reasons": self.reasons,
            "detail": self.detail,
        }


def assess_liquidity(af: AssetFeatures, cfg: RiskSettings) -> LiquidityAssessment:
    reasons: list[str] = []
    detail: dict = {}

    qv = af.quote_volume_24h
    if qv is None:
        return LiquidityAssessment(
            status=LiquidityStatus.UNKNOWN,
            fraction=None,
            veto=False,
            reasons=["24h volume DATA_UNAVAILABLE — cannot assess liquidity"],
        )

    detail["quote_volume_24h"] = qv

    # Volume tier.
    if qv >= cfg.liq_excellent_volume:
        vol_status = LiquidityStatus.EXCELLENT
    elif qv >= cfg.liq_good_volume:
        vol_status = LiquidityStatus.GOOD
    elif qv >= cfg.liq_acceptable_volume:
        vol_status = LiquidityStatus.ACCEPTABLE
    elif qv >= cfg.liq_poor_volume:
        vol_status = LiquidityStatus.POOR
    else:
        vol_status = LiquidityStatus.DANGEROUS
        reasons.append(f"24h volume {qv:,.0f} below the {cfg.liq_poor_volume:,.0f} floor")

    # Spread tier, when a book was fetched. The final status is the worse of the two.
    status = vol_status
    if af.spread_bps is not None:
        detail["spread_bps"] = round(af.spread_bps, 2)
        if af.spread_bps >= cfg.spread_dangerous_bps:
            spread_status = LiquidityStatus.DANGEROUS
            reasons.append(f"spread {af.spread_bps:.0f} bps — execution cost alone invalidates the setup")
        elif af.spread_bps >= cfg.spread_acceptable_bps:
            spread_status = LiquidityStatus.POOR
            reasons.append(f"wide spread ({af.spread_bps:.0f} bps)")
        elif af.spread_bps >= cfg.spread_good_bps:
            spread_status = LiquidityStatus.ACCEPTABLE
        elif af.spread_bps >= cfg.spread_excellent_bps:
            spread_status = LiquidityStatus.GOOD
        else:
            spread_status = LiquidityStatus.EXCELLENT
        if spread_status.rank < status.rank:
            status = spread_status
    else:
        reasons.append("spread unknown (no order book) — liquidity judged on volume alone")

    # Continuous fraction for the 5-point scoring component.
    frac = clamp01(0.7 * scale(qv, cfg.liq_poor_volume, cfg.liq_excellent_volume) + 0.3 * (status.rank / 5.0))

    if status is not LiquidityStatus.DANGEROUS and not reasons:
        reasons.append(f"liquidity {status.value.lower()} on 24h volume {qv:,.0f}")

    return LiquidityAssessment(
        status=status,
        fraction=frac,
        veto=status is LiquidityStatus.DANGEROUS,
        reasons=reasons,
        detail=detail,
    )
