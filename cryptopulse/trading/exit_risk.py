"""EXIT RISK ENGINE — what is going wrong, named one signal at a time.

WHY THIS IS SEPARATE FROM THE HEALTH SCORE

Health answers "is the thesis still standing?" as one number. That number is
what you glance at. But a number cannot be argued with, and the moment that
actually matters — the one where someone decides to sell — deserves a list of
named, individually checkable facts rather than "48/100".

So this produces *signals*, each with a name, a severity and a sentence. The
health score and the exit risk are computed from overlapping inputs on purpose:
one is for the glance, the other is for the decision.

SEVERITY IS NOT A SUM

Signals carry a severity and the report counts them by severity. It does not add
them into a single "exit score", because that would let five cosmetic warnings
outweigh one broken support — and a broken support is not five times anything,
it is a different kind of event. The decision rules read the counts and the
critical flags, never a total.

INVALIDATION OUTRANKS EVERYTHING

The level recorded when the position was opened is the one fact that was agreed
in advance, before any of this was emotional. If price closes through it, the
reason for holding is gone regardless of what momentum, volume or the scores
say, and the report says CRITICAL. Every other signal is evidence; this one is
the terms of the trade.

NOTHING HERE SELLS ANYTHING

The report is read by `trading/decision.py`, which produces a recommendation
that a human then acts on manually. There is no order-placing code in this
repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from cryptopulse.core.logging import get_logger
from cryptopulse.features.pipeline import Bias
from cryptopulse.features.structure import StructureTrend
from cryptopulse.risk.liquidity import LiquidityStatus
from cryptopulse.scoring.states import SetupState

log = get_logger("trading.exit_risk")

__all__ = ["Severity", "ExitSignal", "ExitRiskReport", "assess_exit_risk"]


class Severity(str, Enum):
    """Three levels, because the decision rules need three, not a spectrum."""

    # The agreed terms broke, or the asset became untradeable. Acts immediately.
    CRITICAL = "CRITICAL"
    # A real deterioration in the case. Several of these together mean act.
    MAJOR = "MAJOR"
    # Worth noticing. Never enough on its own.
    MINOR = "MINOR"


_RANK = {Severity.MINOR: 0, Severity.MAJOR: 1, Severity.CRITICAL: 2}


@dataclass(slots=True)
class ExitSignal:
    name: str
    severity: Severity
    message: str

    def to_dict(self) -> dict:
        return {"name": self.name, "severity": self.severity.value, "message": self.message}


@dataclass(slots=True)
class ExitRiskReport:
    signals: list[ExitSignal] = field(default_factory=list)
    # Recorded separately from the signals because the decision rules treat it
    # as a different kind of fact: it is the term agreed in advance.
    invalidation_broken: bool = False
    unavailable: list[str] = field(default_factory=list)

    @property
    def critical(self) -> list[ExitSignal]:
        return [s for s in self.signals if s.severity is Severity.CRITICAL]

    @property
    def major(self) -> list[ExitSignal]:
        return [s for s in self.signals if s.severity is Severity.MAJOR]

    @property
    def minor(self) -> list[ExitSignal]:
        return [s for s in self.signals if s.severity is Severity.MINOR]

    @property
    def worst(self) -> Severity | None:
        return max((s.severity for s in self.signals), key=lambda s: _RANK[s], default=None)

    def messages(self, limit: int = 6) -> list[str]:
        """Most serious first — the order someone reading in a hurry needs."""
        ordered = sorted(self.signals, key=lambda s: _RANK[s.severity], reverse=True)
        return [s.message for s in ordered[:limit]]

    def to_dict(self) -> dict:
        return {
            "signals": [s.to_dict() for s in self.signals],
            "counts": {
                "critical": len(self.critical),
                "major": len(self.major),
                "minor": len(self.minor),
            },
            "invalidation_broken": self.invalidation_broken,
            "worst": self.worst.value if self.worst else None,
            "unavailable": self.unavailable,
            "messages": self.messages(),
        }


def assess_exit_risk(
    position,
    result,
    *,
    current_price: float | None = None,
    drawdown_from_peak_pct: float | None = None,
    btc_regime=None,
) -> ExitRiskReport:
    """Enumerate what is going wrong. Never decides; the decision engine does."""
    report = ExitRiskReport()
    price = current_price if current_price is not None else result.price
    add = report.signals.append

    # -- the terms agreed in advance ---------------------------------------- #
    level = getattr(position, "invalidation_price", None)
    if level is None:
        report.unavailable.append("aucun niveau d'invalidation enregistré à l'entrée")
    elif price <= level:
        report.invalidation_broken = True
        add(ExitSignal(
            "invalidation_broken", Severity.CRITICAL,
            f"invalidation franchie : {price:.6g} est sous {level:.6g} — "
            "la raison de l'achat n'existe plus",
        ))

    if result.state.state is SetupState.INVALIDATED:
        add(ExitSignal(
            "setup_invalidated", Severity.CRITICAL,
            "le setup est passé en INVALIDATED",
        ))

    # -- the asset became untradeable --------------------------------------- #
    status = result.liquidity.status
    if result.liquidity.veto or status is LiquidityStatus.DANGEROUS:
        add(ExitSignal(
            "liquidity_collapsed", Severity.CRITICAL,
            "liquidité devenue dangereuse — sortir au prix affiché n'est plus réaliste",
        ))
    elif status is LiquidityStatus.POOR:
        add(ExitSignal("liquidity_degraded", Severity.MAJOR, "liquidité dégradée"))

    if result.safety.hard_veto:
        add(ExitSignal(
            "safety_veto", Severity.CRITICAL,
            "veto de sécurité déclenché sur cet actif depuis l'entrée",
        ))

    # -- structure ----------------------------------------------------------- #
    st = result.features.primary.structure if result.features else None
    if st is None:
        report.unavailable.append("structure indisponible")
    else:
        if st.trend is StructureTrend.DOWNTREND:
            add(ExitSignal(
                "lower_highs", Severity.MAJOR,
                "structure retournée : plus hauts et plus bas décroissants",
            ))
        if st.fake_breakout:
            add(ExitSignal(
                "failed_breakout", Severity.MAJOR,
                "cassure échouée — le niveau a rejeté le prix",
            ))
        if st.nearest_support is not None and price < st.nearest_support.price:
            add(ExitSignal(
                "support_broken", Severity.MAJOR,
                f"support {st.nearest_support.price:.6g} cassé",
            ))
        if st.range_position is not None and st.range_position < 0.25:
            add(ExitSignal(
                "bottom_of_range", Severity.MINOR,
                "prix retombé dans le bas de son range récent",
            ))

    # -- momentum ------------------------------------------------------------ #
    f = result.features.primary if result.features else None
    if f is None:
        report.unavailable.append("indicateurs indisponibles")
    else:
        if f.roc3 is not None and f.roc3 < -1.0:
            add(ExitSignal(
                "momentum_reversed", Severity.MAJOR,
                f"momentum retourné ({f.roc3:+.2f}% sur les dernières bougies)",
            ))
        elif f.roc3 is not None and f.roc3 < 0:
            add(ExitSignal("momentum_slowing", Severity.MINOR, "momentum qui ralentit"))

        if f.macd_hist is not None and f.macd_hist_prev is not None and f.macd_hist < f.macd_hist_prev:
            add(ExitSignal("macd_contracting", Severity.MINOR, "histogramme MACD en contraction"))

        if f.rsi14 is not None and f.rsi14 < 40.0:
            add(ExitSignal("rsi_weak", Severity.MINOR, f"RSI retombé à {f.rsi14:.0f}"))

        if f.bias is Bias.BEARISH:
            add(ExitSignal("bias_bearish", Severity.MAJOR, "biais retourné baissier"))

        # -- volume ----------------------------------------------------------- #
        if f.volume_climax:
            add(ExitSignal(
                "volume_climax", Severity.MAJOR,
                "pic de volume d'épuisement — souvent la fin d'un mouvement",
            ))
        if f.rvol is not None and f.rvol < 0.7:
            add(ExitSignal(
                "participation_gone", Severity.MINOR,
                f"volume retombé sous la normale (RVOL {f.rvol:.1f}x)",
            ))
        if f.volume_acceleration_pct is not None and f.volume_acceleration_pct < -30.0:
            add(ExitSignal(
                "volume_collapsing", Severity.MINOR,
                f"volume en forte décélération ({f.volume_acceleration_pct:+.0f}%)",
            ))

    # -- order flow ----------------------------------------------------------- #
    af = result.features
    if af is not None:
        if af.order_book_imbalance is not None and af.order_book_imbalance < -0.25:
            add(ExitSignal(
                "sell_pressure", Severity.MAJOR,
                f"pression vendeuse au carnet ({af.order_book_imbalance:+.2f})",
            ))
        if af.spread_bps is not None and af.spread_bps > 30.0:
            add(ExitSignal(
                "spread_widening", Severity.MINOR,
                f"spread élargi à {af.spread_bps:.0f} bps",
            ))

    # -- the engines' own opinion --------------------------------------------- #
    entry_opportunity = getattr(position, "entry_opportunity", None)
    if entry_opportunity is None:
        report.unavailable.append("scores d'entrée non enregistrés")
    elif entry_opportunity - result.final_score > 25.0:
        add(ExitSignal(
            "opportunity_collapsed", Severity.MAJOR,
            f"opportunité effondrée ({entry_opportunity:.0f} → {result.final_score:.0f})",
        ))

    entry_explosion = getattr(position, "entry_explosion", None)
    now_explosion = result.explosion.score if result.explosion else None
    if entry_explosion is not None and now_explosion is not None and now_explosion < entry_explosion * 0.4:
        add(ExitSignal(
            "explosion_collapsed", Severity.MINOR,
            f"urgence retombée ({entry_explosion:.0f} → {now_explosion:.0f})",
        ))

    if result.maturity.score >= 80.0:
        add(ExitSignal(
            "move_exhausted", Severity.MAJOR,
            f"mouvement très avancé (maturité {result.maturity.score:.0f}/100)",
        ))

    # -- giving back the gain --------------------------------------------------- #
    if drawdown_from_peak_pct is None:
        report.unavailable.append("sommet depuis l'entrée non suivi")
    elif drawdown_from_peak_pct <= -10.0:
        add(ExitSignal(
            "deep_retrace", Severity.MAJOR,
            f"repli de {abs(drawdown_from_peak_pct):.1f}% depuis le sommet atteint",
        ))
    elif drawdown_from_peak_pct <= -5.0:
        add(ExitSignal(
            "retrace_from_peak", Severity.MINOR,
            f"repli de {abs(drawdown_from_peak_pct):.1f}% depuis le sommet atteint",
        ))

    # -- the market around it ---------------------------------------------------- #
    if btc_regime is not None:
        trend = getattr(btc_regime, "value", btc_regime)
        if trend == "BEAR_TREND":
            add(ExitSignal(
                "market_against", Severity.MINOR,
                "régime de marché baissier — le contexte joue contre la position",
            ))

    return report
