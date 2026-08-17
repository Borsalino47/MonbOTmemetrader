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
from cryptopulse.i18n import money, num
from cryptopulse.i18n import reasons as R
from cryptopulse.i18n.labels import LIQUIDITY_FR

__all__ = ["LiquidityStatus", "LIQUIDITY_LABEL_FR", "LiquidityAssessment", "assess_liquidity"]


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


# The enum values stay English: they are stored in SQLite, filtered on by the
# API and compared in code. Only the *label* is translated — changing the value
# would be a database migration wearing a translation's clothes.
LIQUIDITY_LABEL_FR: dict[LiquidityStatus, str] = {
    LiquidityStatus.EXCELLENT: "excellente",
    LiquidityStatus.GOOD: "bonne",
    LiquidityStatus.ACCEPTABLE: "acceptable",
    LiquidityStatus.POOR: "faible",
    LiquidityStatus.DANGEROUS: "dangereuse",
    LiquidityStatus.UNKNOWN: "inconnue",
}


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
            # The identifier stays for filtering and storage; the label is what
            # the screen renders (invariant: never print the enum value).
            "status_label_fr": LIQUIDITY_FR[self.status.value],
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
            reasons=[R.LIQ_NO_VOLUME_DATA()],
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
        reasons.append(R.LIQ_BELOW_FLOOR(volume=money(qv), floor=money(cfg.liq_poor_volume)))

    # Spread tier, when a book was fetched. The final status is the worse of the two.
    status = vol_status
    if af.spread_bps is not None:
        detail["spread_bps"] = round(af.spread_bps, 2)
        if af.spread_bps >= cfg.spread_dangerous_bps:
            spread_status = LiquidityStatus.DANGEROUS
            reasons.append(R.LIQ_SPREAD_FATAL(spread=num(af.spread_bps, 0)))
        elif af.spread_bps >= cfg.spread_acceptable_bps:
            spread_status = LiquidityStatus.POOR
            reasons.append(R.LIQ_SPREAD_WIDE(spread=num(af.spread_bps, 0)))
        elif af.spread_bps >= cfg.spread_good_bps:
            spread_status = LiquidityStatus.ACCEPTABLE
        elif af.spread_bps >= cfg.spread_excellent_bps:
            spread_status = LiquidityStatus.GOOD
        else:
            spread_status = LiquidityStatus.EXCELLENT
        if spread_status.rank < status.rank:
            status = spread_status
    else:
        reasons.append(R.LIQ_SPREAD_UNKNOWN())

    # Continuous fraction for the 5-point scoring component.
    frac = clamp01(0.7 * scale(qv, cfg.liq_poor_volume, cfg.liq_excellent_volume) + 0.3 * (status.rank / 5.0))

    if status is not LiquidityStatus.DANGEROUS and not reasons:
        reasons.append(R.LIQ_GRADED(status=LIQUIDITY_LABEL_FR[status], volume=money(qv)))

    return LiquidityAssessment(
        status=status,
        fraction=frac,
        veto=status is LiquidityStatus.DANGEROUS,
        reasons=reasons,
        detail=detail,
    )
