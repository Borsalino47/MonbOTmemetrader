"""POSITION_HEALTH_SCORE — are the reasons for buying still true?

THE QUESTION THIS ANSWERS, AND THE ONE IT DOES NOT

It does *not* ask "is this a good asset?". That is the opportunity score, and it
would answer the same way whether or not anyone owned the thing. This asks a
question that only exists once a position is open: **the case that justified
entering — is it still standing?**

The distinction matters because the two diverge constantly. A token can be a
perfectly reasonable asset while the specific setup you bought has quietly
stopped existing: the level you entered on gave way, the volume that carried the
move left, the higher timeframe turned. Opportunity would still read 60. Health
reads what changed *since entry*, which is the only thing that can tell you the
thesis died.

MEASURED AGAINST ENTRY, NOT AGAINST ZERO

Every component compares the present to the state recorded when the position was
opened. That is why `Position` copies its entry context in rather than
recomputing it: a health score computed against today's idea of the setup would
always look fine, because it would be marking its own homework.

NOT A PnL

Health is deliberately independent of whether the position is up or down. A
position that is +12% on a setup that has completely broken is unhealthy, and
saying so is the entire point — that is the moment the gain is about to be given
back. Conversely a position that is -2% on an intact setup is healthy and has
told you nothing except that entries are rarely perfect. PnL is displayed
separately, right next to it, so the two can disagree visibly.

WHAT AN UNAVAILABLE INPUT DOES

It removes its component from the total *and* from the denominator, then says
so. Scoring a missing reading as zero would make a temporary data gap look like
a collapsing position, which would be the worst possible false alarm: it would
tell someone to sell because a request failed. `coverage` reports how much of
the score could actually be computed, and below half the score is reported as
unknown rather than as a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import Timeframe
from cryptopulse.features.pipeline import Bias
from cryptopulse.risk.liquidity import LiquidityStatus
from cryptopulse.scoring.states import SetupState

log = get_logger("trading.health")

__all__ = ["HEALTH_VERSION", "HealthComponent", "PositionHealth", "compute_health"]

HEALTH_VERSION = "POSITION_HEALTH_V1"

# Below this fraction of computable components the score is not reported. Half
# a health score is not a small health score; it is an unknown one.
MIN_COVERAGE = 0.5

# Weights sum to 100 when every input is available. Missing ones are removed
# from both sides, so the scale never silently shifts.
WEIGHTS: dict[str, float] = {
    "structure": 25.0,      # is the shape you bought still there
    "momentum": 20.0,       # is price still going the way it was
    "volume": 15.0,         # is anyone still participating
    "invalidation": 15.0,   # how close is the line that ends the thesis
    "score_drift": 15.0,    # what the engines think now vs at entry
    "tradeability": 10.0,   # could you still get out at the price on screen
}


@dataclass(slots=True)
class HealthComponent:
    name: str
    points: float
    max_points: float
    available: bool = True
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "points": round(self.points, 1),
            "max_points": self.max_points,
            "available": self.available,
            "reasons": self.reasons,
            "caveats": self.caveats,
        }


@dataclass(slots=True)
class PositionHealth:
    score: float | None                  # None when too little could be computed
    coverage: float                      # 0..1, share of the weight that was measurable
    components: list[HealthComponent] = field(default_factory=list)
    version: str = HEALTH_VERSION

    @property
    def label_fr(self) -> str:
        if self.score is None:
            return "INCONNUE"
        if self.score >= 70.0:
            return "SAINE"
        if self.score >= 50.0:
            return "QUI SE DÉGRADE"
        if self.score >= 30.0:
            return "FRAGILE"
        return "ROMPUE"

    def reasons(self) -> list[str]:
        out: list[str] = []
        for c in sorted(self.components, key=lambda c: c.points, reverse=True):
            out.extend(c.reasons)
        return out

    def caveats(self) -> list[str]:
        out: list[str] = []
        for c in self.components:
            out.extend(c.caveats)
        return out

    def to_dict(self) -> dict:
        return {
            "score": None if self.score is None else round(self.score, 1),
            "label_fr": self.label_fr,
            "coverage": round(self.coverage, 3),
            "version": self.version,
            "components": [c.to_dict() for c in self.components],
            "reasons": self.reasons(),
            "caveats": self.caveats(),
            "note": (
                "Santé de la thèse d'achat, pas du gain. Une position en hausse dont le "
                "setup est cassé est en mauvaise santé — c'est précisément le moment où "
                "le gain est sur le point d'être rendu."
            ),
        }


def compute_health(position, result, *, current_price: float | None = None) -> PositionHealth:
    """Score the surviving thesis. `position` supplies the entry state.

    `result` is the current `ScoreResult` for the symbol. Both are needed: the
    comparison is the measurement, and neither side alone means anything.
    """
    price = current_price if current_price is not None else result.price
    components = [
        _structure(result),
        _momentum(result),
        _volume(result),
        _invalidation(position, price),
        _score_drift(position, result),
        _tradeability(result),
    ]

    available = [c for c in components if c.available]
    total_weight = sum(c.max_points for c in available)
    coverage = total_weight / sum(WEIGHTS.values()) if WEIGHTS else 0.0

    if coverage < MIN_COVERAGE:
        # A data gap must never look like a collapsing position. Telling someone
        # to sell because a request failed would be the worst false alarm here.
        return PositionHealth(score=None, coverage=coverage, components=components)

    earned = sum(c.points for c in available)
    # Rescaled onto 0-100 over the weight that was actually measurable, so a
    # missing component does not quietly lower every score.
    score = (earned / total_weight) * 100.0 if total_weight else 0.0
    return PositionHealth(score=max(0.0, min(100.0, score)), coverage=coverage, components=components)


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #


def _mk(name: str) -> HealthComponent:
    return HealthComponent(name=name, points=0.0, max_points=WEIGHTS[name])


def _structure(result) -> HealthComponent:
    """Is the shape that was bought still on the chart?"""
    c = _mk("structure")
    st = result.features.primary.structure if result.features else None
    if st is None:
        c.available = False
        c.caveats.append("structure indisponible")
        return c

    state = result.state.state
    if state is SetupState.INVALIDATED:
        c.caveats.append("setup invalidé — la raison de l'achat n'existe plus")
        return c

    from cryptopulse.features.structure import StructureTrend

    if st.trend is StructureTrend.UPTREND:
        c.points += c.max_points * 0.6
        c.reasons.append("structure toujours haussière (plus hauts et plus bas croissants)")
    elif st.trend is StructureTrend.DOWNTREND:
        c.caveats.append("structure retournée : plus hauts et plus bas décroissants")
    else:
        c.points += c.max_points * 0.25
        c.reasons.append("structure en range, ni cassée ni confirmée")

    if st.fake_breakout:
        c.caveats.append("cassure échouée — le niveau a rejeté le prix")
    elif state in (SetupState.BREAKOUT, SetupState.RETEST, SetupState.CONTINUATION):
        c.points += c.max_points * 0.4
        c.reasons.append(f"setup toujours en {state.value}")

    c.points = min(c.points, c.max_points)
    return c


def _momentum(result) -> HealthComponent:
    """Is price still going the way it was bought to go?"""
    c = _mk("momentum")
    f = result.features.primary if result.features else None
    if f is None or (f.roc3 is None and f.macd_hist is None):
        c.available = False
        c.caveats.append("momentum indisponible")
        return c

    if f.roc3 is not None:
        if f.roc3 > 0:
            c.points += c.max_points * 0.45
            c.reasons.append(f"prix encore en hausse sur les dernières bougies ({f.roc3:+.2f}%)")
        elif f.roc3 < -0.5:
            c.caveats.append(f"prix en repli ({f.roc3:+.2f}% sur les dernières bougies)")

    if f.macd_hist is not None and f.macd_hist_prev is not None:
        if f.macd_hist > f.macd_hist_prev:
            c.points += c.max_points * 0.3
            c.reasons.append("MACD encore en expansion")
        else:
            c.caveats.append("histogramme MACD en contraction — le momentum ralentit")

    if f.bias is Bias.BULLISH:
        c.points += c.max_points * 0.25
        c.reasons.append("biais toujours haussier sur l'unité de temps principale")
    elif f.bias is Bias.BEARISH:
        c.caveats.append("biais retourné baissier")

    c.points = min(c.points, c.max_points)
    return c


def _volume(result) -> HealthComponent:
    """Is anyone still participating, or did the crowd leave?"""
    c = _mk("volume")
    f = result.features.primary if result.features else None
    if f is None or f.rvol is None:
        c.available = False
        c.caveats.append("volume relatif indisponible")
        return c

    if f.rvol >= 1.2:
        c.points += c.max_points * 0.6
        c.reasons.append(f"participation toujours élevée (RVOL {f.rvol:.1f}x)")
    elif f.rvol >= 0.8:
        c.points += c.max_points * 0.3
        c.reasons.append(f"participation revenue à la normale (RVOL {f.rvol:.1f}x)")
    else:
        c.caveats.append(f"volume retombé sous la normale (RVOL {f.rvol:.1f}x) — plus personne ne suit")

    if f.volume_climax:
        c.caveats.append("pic de volume d'épuisement détecté")
    elif f.volume_acceleration_pct is not None and f.volume_acceleration_pct > 0:
        c.points += c.max_points * 0.4
        c.reasons.append("volume encore en accélération")

    c.points = min(c.points, c.max_points)
    return c


def _invalidation(position, price: float) -> HealthComponent:
    """How much room is left before the thesis is formally dead?

    Measured against the level recorded at entry, not against a level recomputed
    now. A moving invalidation is not an invalidation.
    """
    c = _mk("invalidation")
    level = getattr(position, "invalidation_price", None)
    entry = getattr(position, "entry_price", None)
    if level is None or entry is None or level <= 0 or entry <= 0:
        c.available = False
        c.caveats.append("aucun niveau d'invalidation enregistré à l'entrée")
        return c

    if price <= level:
        c.caveats.append(f"invalidation franchie : {price:.6g} sous {level:.6g}")
        return c

    # Distance expressed as a share of the original risk, so it means the same
    # thing on a memecoin and on BTC.
    risk = entry - level
    if risk <= 0:
        c.available = False
        c.caveats.append("niveau d'invalidation incohérent avec le prix d'entrée")
        return c

    room = (price - level) / risk
    if room >= 1.5:
        c.points = c.max_points
        c.reasons.append("prix largement au-dessus de l'invalidation")
    elif room >= 1.0:
        c.points = c.max_points * 0.75
        c.reasons.append("prix au-dessus de l'invalidation, marge confortable")
    elif room >= 0.5:
        c.points = c.max_points * 0.4
        c.reasons.append("prix au-dessus de l'invalidation, marge réduite")
    else:
        c.points = c.max_points * 0.15
        c.caveats.append("invalidation proche — la thèse tient à peu de chose")
    return c


def _score_drift(position, result) -> HealthComponent:
    """What the engines thought at entry against what they think now.

    The most direct reading of "has the case changed", and the one that needs
    the entry snapshot: without it there is nothing to compare against.
    """
    c = _mk("score_drift")
    entry_opportunity = getattr(position, "entry_opportunity", None)
    if entry_opportunity is None:
        c.available = False
        c.caveats.append("score d'opportunité à l'entrée non enregistré")
        return c

    drop = entry_opportunity - result.final_score
    if drop <= 0:
        c.points += c.max_points * 0.6
        c.reasons.append(f"opportunité stable ou en hausse depuis l'entrée ({result.final_score:.0f})")
    elif drop <= 10:
        c.points += c.max_points * 0.4
        c.reasons.append(f"opportunité légèrement en retrait ({entry_opportunity:.0f} → {result.final_score:.0f})")
    elif drop <= 25:
        c.points += c.max_points * 0.15
        c.caveats.append(f"opportunité en baisse nette ({entry_opportunity:.0f} → {result.final_score:.0f})")
    else:
        c.caveats.append(f"opportunité effondrée ({entry_opportunity:.0f} → {result.final_score:.0f})")

    entry_explosion = getattr(position, "entry_explosion", None)
    now_explosion = result.explosion.score if result.explosion else None
    if entry_explosion is None or now_explosion is None:
        c.caveats.append("score d'explosion non comparable")
    elif now_explosion >= entry_explosion * 0.6:
        c.points += c.max_points * 0.4
        c.reasons.append("urgence encore présente")
    else:
        c.caveats.append(f"explosion retombée ({entry_explosion:.0f} → {now_explosion:.0f})")

    c.points = min(c.points, c.max_points)
    return c


def _tradeability(result) -> HealthComponent:
    """Could the position still be closed at something like the screen price?"""
    c = _mk("tradeability")
    status = result.liquidity.status
    if status is LiquidityStatus.UNKNOWN:
        c.available = False
        c.caveats.append("liquidité inconnue")
        return c

    if result.liquidity.veto or status is LiquidityStatus.DANGEROUS:
        c.caveats.append("liquidité devenue dangereuse — une sortie ne serait pas exécutable au prix affiché")
        return c
    if status is LiquidityStatus.POOR:
        c.points = c.max_points * 0.3
        c.caveats.append("liquidité dégradée depuis l'entrée")
        return c

    c.points = c.max_points
    c.reasons.append(f"liquidité {status.value} — la sortie reste exécutable")
    return c


def primary_timeframe_of(result) -> Timeframe | None:
    """Small helper so callers do not reach into `features` themselves."""
    return result.features.primary_timeframe if result.features else None
