"""Safety Score (0–100) and hard veto.

On a centralised exchange the dangerous failure modes are mostly market-quality
ones: an asset that only trades a handful of times per bar, a chart with gaps, a
price series that behaves in ways that suggest thin or manipulated tape.

The DEX-specific checks (holder concentration, contract risk, liquidity that can
be pulled, pair age) are declared in `DexSafetyInputs` so the interface is fixed
now, but they are **not implemented in V1** — no DEX provider is wired up, and
inventing those numbers would be worse than not having them. `dex_safety` raises
NotImplementedError rather than returning a plausible-looking default.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.config.settings import RiskSettings
from cryptopulse.features.pipeline import AssetFeatures
from cryptopulse.risk.liquidity import LiquidityAssessment, LiquidityStatus

__all__ = ["SafetyAssessment", "assess_cex_safety", "DexSafetyInputs", "dex_safety"]


@dataclass(slots=True)
class SafetyAssessment:
    score: float  # 0..100, higher is safer
    hard_veto: bool
    reasons: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "hard_veto": self.hard_veto,
            "reasons": self.reasons,
            "detail": self.detail,
        }


def assess_cex_safety(af: AssetFeatures, liq: LiquidityAssessment, cfg: RiskSettings) -> SafetyAssessment:
    """Market-quality safety for a centralised-exchange pair.

    Starts at 100 and subtracts for each concrete defect found. Every deduction
    is named, so a 72 is always traceable to specific evidence.
    """
    score = 100.0
    reasons: list[str] = []
    detail: dict = {}
    hard_veto = False
    f = af.primary

    # Liquidity is the dominant safety term on a CEX.
    if liq.status is LiquidityStatus.DANGEROUS:
        score -= 55.0
        hard_veto = True
        reasons.append("liquidity DANGEROUS — hard veto")
    elif liq.status is LiquidityStatus.POOR:
        score -= 25.0
        reasons.append("poor liquidity")
    elif liq.status is LiquidityStatus.UNKNOWN:
        score -= 15.0
        reasons.append("liquidity could not be assessed")

    detail["bars"] = f.bars

    # Gaps in the candle stream mean the pair stops trading — a real hazard for
    # anything you intend to exit.
    if f.provenance and f.provenance.note == "GAPS":
        score -= 20.0
        reasons.append("gaps in the candle stream (pair does not trade continuously)")

    # Insufficient history: we cannot judge what we cannot see.
    if f.bars < 60:
        score -= 20.0
        reasons.append(f"only {f.bars} bars of history")
    elif f.bars < 120:
        score -= 8.0

    # Extreme volatility relative to the asset's own norm.
    if f.atr_pct is not None:
        detail["atr_pct"] = round(f.atr_pct, 3)
        if f.atr_pct > 8.0:
            score -= 20.0
            reasons.append(f"extreme volatility (ATR {f.atr_pct:.1f}% of price per bar)")
        elif f.atr_pct > 4.0:
            score -= 8.0
            reasons.append(f"elevated volatility (ATR {f.atr_pct:.1f}% of price)")

    # Wide spread is both a liquidity and a safety issue.
    if af.spread_bps is not None and af.spread_bps >= cfg.spread_dangerous_bps:
        score -= 20.0
        reasons.append(f"spread {af.spread_bps:.0f} bps")

    score = float(max(0.0, min(100.0, score)))
    if score <= cfg.safety_hard_veto_floor:
        hard_veto = True
        reasons.append(f"safety score {score:.0f} at or below the {cfg.safety_hard_veto_floor:.0f} floor — hard veto")

    if not reasons:
        reasons.append("no market-quality defects detected")

    return SafetyAssessment(score=score, hard_veto=hard_veto, reasons=reasons, detail=detail)


# --------------------------------------------------------------------------- #
# DEX surface — interface fixed, implementation deferred to Phase 2
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DexSafetyInputs:
    """Inputs a DEX safety check will need once a DEX provider is connected."""

    liquidity_usd: float | None = None
    liquidity_change_1h_pct: float | None = None
    pair_age_hours: float | None = None
    holder_count: int | None = None
    top10_holder_pct: float | None = None
    buys_24h: int | None = None
    sells_24h: int | None = None
    contract_verified: bool | None = None
    mint_authority_active: bool | None = None
    liquidity_locked: bool | None = None
    fdv_usd: float | None = None


def dex_safety(inputs: DexSafetyInputs, cfg: RiskSettings) -> SafetyAssessment:
    raise NotImplementedError(
        "DEX safety scoring is NOT IMPLEMENTED in V1. No DEX provider is connected, and the "
        "holder-concentration and contract-risk fields it depends on have no source yet. "
        "Implementing it against invented inputs would produce a safety number that looks "
        "authoritative and means nothing. Scheduled for Phase 2 alongside the DEX provider."
    )
