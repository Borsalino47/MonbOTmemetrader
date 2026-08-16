"""SCORE_ENGINE_V1 — the orchestrator.

Versioning rule: the weights and thresholds that produced a score are recorded
*with* that score, in `ScoreResult.engine_version` and `weights_fingerprint`.
When the weighting changes, the version string changes with it, so historical
signals in the journal are never silently re-interpreted under new rules.

The current weighting is a starting hypothesis, not a validated model. It has NOT
been statistically fitted — see `backtest/` for the machinery that is meant to
validate or replace it, and README "Known limits".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.features.pipeline import AssetFeatures
from cryptopulse.risk.liquidity import LiquidityAssessment, assess_liquidity
from cryptopulse.risk.penalties import PenaltyReport, compute_penalties
from cryptopulse.risk.safety import SafetyAssessment, assess_cex_safety
from cryptopulse.scoring.acceleration import AccelerationScores, compute_acceleration
from cryptopulse.scoring.components import (
    ComponentScore,
    score_breakout,
    score_liquidity,
    score_momentum,
    score_mtf,
    score_orderflow,
    score_structure,
    score_volatility,
    score_volume,
)
from cryptopulse.scoring.confidence import DataConfidence, compute_confidence
from cryptopulse.scoring.pump_maturity import PumpMaturity, compute_pump_maturity
from cryptopulse.scoring.states import SetupState, StateDecision, determine_state

if TYPE_CHECKING:  # `scoring/explosion.py` reads AssetFeatures, not this module,
    # so there is no runtime cycle — but the import stays behind the guard to keep
    # the dependency one-directional and obvious.
    from cryptopulse.scoring.explosion import ExplosionResult

__all__ = ["ScoreResult", "ScoreEngine"]

ENGINE_VERSION = "SCORE_ENGINE_V1"


@dataclass(slots=True)
class ScoreResult:
    """Everything the UI, the alert engine and the journal need about one asset."""

    symbol: str
    price: float
    timestamp_ms: int

    raw_score: float
    risk_penalty: float
    final_score: float

    components: list[ComponentScore]
    penalties: PenaltyReport
    maturity: PumpMaturity
    acceleration: AccelerationScores
    confidence: DataConfidence
    liquidity: LiquidityAssessment
    safety: SafetyAssessment
    state: StateDecision

    engine_version: str = ENGINE_VERSION
    weights_fingerprint: str = ""
    features: AssetFeatures | None = None
    score_acceleration: float | None = None  # filled in by ScoreMemory
    previous_score: float | None = None

    # A separate engine's answer to a separate question — "is this about to move
    # in the next fifteen minutes?" — carried alongside rather than folded in.
    # Filled in by the scanner after the order-book pass, so the leaders' books
    # reach it. `None` means the explosion engine did not run, which is different
    # from a score of zero and must stay distinguishable.
    explosion: ExplosionResult | None = None

    # Never a probability. See README §"Score is not a probability".
    @property
    def is_premium(self) -> bool:
        """Premium requires a good score AND clean gates AND a young move."""
        return (
            self.final_score >= 72.0
            and not self.safety.hard_veto
            and not self.liquidity.veto
            and self.confidence.score >= 60.0
            and self.state.state in (SetupState.ARMED, SetupState.BREAKOUT, SetupState.RETEST)
        )

    def why(self) -> list[str]:
        out: list[str] = []
        for c in sorted(self.components, key=lambda c: c.points, reverse=True):
            if c.points > 0 and c.reasons:
                out.extend(c.reasons)
        out.extend(self.acceleration.reasons)
        return out

    def risks(self) -> list[str]:
        out = [p.reason for p in self.penalties.items]
        for c in self.components:
            out.extend(c.caveats)
        out.extend(r for r in self.maturity.reasons if self.maturity.score >= 45)
        if self.safety.hard_veto:
            out.extend(self.safety.reasons)
        out.extend(self.confidence.issues)
        return out

    def table_metrics(self) -> dict:
        """The handful of numbers the scanner table shows, without shipping the
        whole feature blob for every row."""
        if self.features is None:
            return {}
        f = self.features.primary
        st = f.structure
        by_tf = self.features.timeframes

        def roc_of(tf_value: str) -> float | None:
            for tf, feats in by_tf.items():
                if tf.value == tf_value:
                    return feats.roc1
            return None

        return {
            "change_primary_pct": f.roc1,
            "change_5m_pct": roc_of("5m"),
            "change_15m_pct": roc_of("15m"),
            "change_1h_pct": roc_of("1h"),
            "change_4h_pct": roc_of("4h"),
            "change_24h_pct": self.features.price_change_pct_24h,
            "rvol": f.rvol,
            "volume_acceleration_pct": f.volume_acceleration_pct,
            "atr_pct": f.atr_pct,
            "rsi14": f.rsi14,
            "quote_volume_24h": self.features.quote_volume_24h,
            "spread_bps": self.features.spread_bps,
            "order_book_imbalance": self.features.order_book_imbalance,
            "distance_to_breakout_atr": st.distance_to_resistance_atr if st else None,
            "resistance": st.nearest_resistance.price if (st and st.nearest_resistance) else None,
            "support": st.nearest_support.price if (st and st.nearest_support) else None,
        }

    def verdict(self) -> dict:
        """Four-level plain-language summary. Imported lazily: `scoring/verdict.py`
        reads a `ScoreResult`, so importing it at module scope would be circular."""
        from cryptopulse.scoring.verdict import build_verdict

        return build_verdict(self).to_dict()

    def to_dict(self, include_features: bool = False) -> dict:
        d = {
            "symbol": self.symbol,
            "price": self.price,
            "timestamp_ms": self.timestamp_ms,
            "engine_version": self.engine_version,
            "weights_fingerprint": self.weights_fingerprint,
            "raw_score": round(self.raw_score, 2),
            "risk_penalty": round(self.risk_penalty, 2),
            "final_score": round(self.final_score, 2),
            "opportunity_label": f"{self.final_score:.0f}/100",
            "score_acceleration": None if self.score_acceleration is None else round(self.score_acceleration, 2),
            "previous_score": None if self.previous_score is None else round(self.previous_score, 2),
            "components": [c.to_dict() for c in self.components],
            "penalties": self.penalties.to_dict(),
            "pump_maturity": self.maturity.to_dict(),
            "acceleration": self.acceleration.to_dict(),
            "data_confidence": self.confidence.to_dict(),
            "liquidity": self.liquidity.to_dict(),
            "safety": self.safety.to_dict(),
            "setup": self.state.to_dict(),
            "is_premium": self.is_premium,
            "verdict": self.verdict(),
            # Absent rather than zero when the engine did not run: a token that
            # scored 0 for explosion said something, one that was never scored
            # did not.
            "explosion": self.explosion.to_dict() if self.explosion else None,
            "metrics": self.table_metrics(),
            "why": self.why(),
            "risks": self.risks(),
        }
        if include_features and self.features is not None:
            d["features"] = self.features.to_dict()
        return d


class ScoreEngine:
    """Stateless scorer. One instance is safe to share across the scan."""

    def __init__(self, settings: CryptoPulseSettings) -> None:
        self.settings = settings
        self.scoring = settings.scoring
        self.risk = settings.risk
        self._validate_weights()
        self.weights_fingerprint = self._fingerprint()

    def _weights(self) -> dict[str, float]:
        s = self.scoring
        return {
            "volume": s.w_volume,
            "momentum": s.w_momentum,
            "structure": s.w_structure,
            "breakout": s.w_breakout,
            "volatility": s.w_volatility,
            "orderflow": s.w_orderflow,
            "mtf": s.w_mtf,
            "liquidity": s.w_liquidity,
        }

    def _validate_weights(self) -> None:
        total = sum(self._weights().values())
        if abs(total - 100.0) > 1e-6:
            raise ValueError(
                f"scoring weights must sum to 100, got {total}. "
                "Adjust CP_SCORE_W_* so the opportunity score stays on a 0-100 scale."
            )

    def _fingerprint(self) -> str:
        """Short hash of the weights + thresholds, so a config change is visible."""
        payload = json.dumps(
            {
                "weights": self._weights(),
                "thresholds": {
                    "observe": self.scoring.threshold_observe,
                    "watch": self.scoring.threshold_watch,
                    "armed": self.scoring.threshold_armed,
                    "breakout_confirm_atr": self.scoring.breakout_confirm_atr,
                    "pump_late": self.scoring.pump_maturity_late,
                },
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    # -- main entry point ---------------------------------------------------- #

    def score(self, af: AssetFeatures, now_ms: int) -> ScoreResult:
        cfg = self.scoring

        # Gates run first — liquidity informs both a component and the penalties.
        liq = assess_liquidity(af, self.risk)

        components = [
            score_volume(af, cfg),
            score_momentum(af, cfg),
            score_structure(af, cfg),
            score_breakout(af, cfg),
            score_volatility(af, cfg),
            score_orderflow(af, cfg),
            score_mtf(af, cfg),
            score_liquidity(af, cfg, liq.fraction),
        ]
        raw = sum(c.points for c in components)

        maturity = compute_pump_maturity(af)
        acceleration = compute_acceleration(af, maturity)
        confidence = compute_confidence(af, self.settings.scanner, now_ms)
        safety = assess_cex_safety(af, liq, self.risk)
        penalties = compute_penalties(af, maturity, liq, cfg, self.risk)

        final = max(0.0, raw - penalties.total)

        state = determine_state(
            af,
            final,
            cfg,
            hard_veto=safety.hard_veto or liq.veto,
            maturity_score=maturity.score,
        )

        return ScoreResult(
            symbol=af.symbol,
            price=af.price,
            timestamp_ms=af.primary.close_time_ms,
            raw_score=raw,
            risk_penalty=penalties.total,
            final_score=final,
            components=components,
            penalties=penalties,
            maturity=maturity,
            acceleration=acceleration,
            confidence=confidence,
            liquidity=liq,
            safety=safety,
            state=state,
            engine_version=cfg.engine_version,
            weights_fingerprint=self.weights_fingerprint,
            features=af,
        )
