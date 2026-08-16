"""TOKEN_DISCOVERY_SCORE — does this token deserve priority monitoring *now*?

A DIFFERENT QUESTION FROM THE OPPORTUNITY SCORE

The Opportunity Score asks "is this a good setup?". This asks "has this token's
behaviour just changed enough that I should start watching it?". They are not
the same question and must not share an engine:

  * A large-cap in a textbook retest scores well on opportunity and near zero
    here — nothing about it has *changed*, and it is already being watched.
  * A quiet token whose volume has tripled while its price has barely moved
    scores well here and poorly on opportunity — there is no setup yet, which is
    exactly the point of finding it early.

Stacking both into one weighted engine would force one set of weights to serve
two horizons and two validation samples, and would make it impossible to say
later which of the two was carrying any signal. So this is a separate engine
with its own version and its own weights fingerprint, journalled beside the
opportunity score rather than replacing it.

WHAT IT WEIGHS, AND WHY THOSE THINGS

  anomaly (25)       How unusual current activity is against this token's own
                     recent history. Self-relative on purpose: a $2M token
                     doing 4x its normal volume is a bigger event than a $2B one
                     doing 1.1x, and any absolute threshold would say otherwise.
  acceleration (20)  Whether that unusualness is still building or already
                     fading. A finished surge is not a discovery.
  precedence (15)    Volume rising while price is still flat — the shape this
                     whole product exists to find. Weighted deliberately: it is
                     rare, and it is the only component that describes a
                     *sequence* rather than a level.
  compression (15)   Volatility coiled and ready to expand. A token that has
                     already expanded has told you its news.
  earliness (15)     Low pump maturity and room left in the day's range. This is
                     the veto-shaped one: a token that has already run cannot be
                     an early discovery however loud it is.
  tradeability (10)  Enough liquidity and a tight enough spread that a move
                     would be worth anything. Small, because it is a filter, not
                     a reason to be interested.

NOT A PROBABILITY, AND NOT VALIDATED

These weights are a hypothesis, exactly like the opportunity weights, and they
have never been fitted against outcomes. `DISCOVERY_ENGINE_V1` and the weights
fingerprint go into every journalled row so that when there is enough history to
measure them, old rows are never reinterpreted under new rules. Until then the
number is a ranking and is displayed as `84/100`, never `84%`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.logging import get_logger
from cryptopulse.features.pipeline import AssetFeatures
from cryptopulse.risk.liquidity import LiquidityStatus
from cryptopulse.scoring.components import ComponentScore

log = get_logger("scoring.discovery")

__all__ = ["DISCOVERY_ENGINE_VERSION", "DiscoveryResult", "DiscoveryEngine"]

DISCOVERY_ENGINE_VERSION = "DISCOVERY_ENGINE_V1"

# Must sum to 100. The engine refuses to construct otherwise, on purpose.
WEIGHTS: dict[str, float] = {
    "anomaly": 25.0,
    "acceleration": 20.0,
    "precedence": 15.0,
    "compression": 15.0,
    "earliness": 15.0,
    "tradeability": 10.0,
}


@dataclass(slots=True)
class DiscoveryResult:
    symbol: str
    score: float
    components: list[ComponentScore]
    engine_version: str
    weights_fingerprint: str
    timestamp_ms: int

    def why(self) -> list[str]:
        out: list[str] = []
        for c in sorted(self.components, key=lambda c: c.points, reverse=True):
            if c.points > 0:
                out.extend(c.reasons)
        return out

    def risks(self) -> list[str]:
        out: list[str] = []
        for c in self.components:
            out.extend(c.caveats)
        return out

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            # A ranking out of 100. Never rendered with a percent sign.
            "discovery_score": round(self.score, 2),
            "discovery_label": f"{self.score:.0f}/100",
            "engine_version": self.engine_version,
            "weights_fingerprint": self.weights_fingerprint,
            "timestamp_ms": self.timestamp_ms,
            "components": [c.to_dict() for c in self.components],
            "why": self.why(),
            "risks": self.risks(),
            "disclaimer": (
                "A ranking of how much this token's behaviour has changed, not a probability "
                "and not advice. These weights have never been validated against outcomes."
            ),
        }


class DiscoveryEngine:
    """Stateless. One instance is safe to share across a deep scan."""

    def __init__(self, settings: CryptoPulseSettings) -> None:
        self.settings = settings
        total = sum(WEIGHTS.values())
        if abs(total - 100.0) > 1e-6:
            raise ValueError(
                f"discovery weights must sum to 100, got {total}. "
                "The score is presented on a 0-100 scale and must stay on it."
            )
        self.weights_fingerprint = self._fingerprint()

    def _fingerprint(self) -> str:
        payload = json.dumps({"weights": WEIGHTS, "version": DISCOVERY_ENGINE_VERSION}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def score(
        self,
        af: AssetFeatures,
        *,
        timestamp_ms: int,
        volume_excess_vs_yesterday: float | None = None,
        maturity_score: float | None = None,
        liquidity_status: LiquidityStatus | None = None,
        range_position: float | None = None,
    ) -> DiscoveryResult:
        """Score one asset. Inputs the caller could not supply are stated, not guessed."""
        components = [
            _anomaly(af, volume_excess_vs_yesterday),
            _acceleration(af),
            _precedence(af, volume_excess_vs_yesterday),
            _compression(af),
            _earliness(maturity_score, range_position),
            _tradeability(af, liquidity_status),
        ]
        total = sum(c.points for c in components)
        return DiscoveryResult(
            symbol=af.symbol,
            score=max(0.0, min(100.0, total)),
            components=components,
            engine_version=DISCOVERY_ENGINE_VERSION,
            weights_fingerprint=self.weights_fingerprint,
            timestamp_ms=timestamp_ms,
        )


# --------------------------------------------------------------------------- #
# Components. Each explains itself; an unavailable input scores zero AND says so.
# --------------------------------------------------------------------------- #


def _mk(name: str) -> ComponentScore:
    return ComponentScore(name=name, points=0.0, max_points=WEIGHTS[name])


def _anomaly(af: AssetFeatures, excess: float | None) -> ComponentScore:
    """How unusual is current activity against this token's *own* history."""
    c = _mk("anomaly")
    f = af.primary
    max_pts = c.max_points

    if f.rvol is None:
        c.available = False
        c.caveats.append("DATA_UNAVAILABLE: relative volume could not be computed")
    else:
        # RVOL is already self-relative: 1.0 is this token's own normal.
        if f.rvol >= 1.3:
            gain = min(max_pts * 0.7, (f.rvol - 1.0) * max_pts * 0.35)
            c.points += gain
            c.reasons.append(f"volume {f.rvol:.1f}x this token's own recent average")
        elif f.rvol < 0.8:
            c.caveats.append(f"volume below its own average ({f.rvol:.1f}x) — nothing is happening")

    if excess is None:
        c.caveats.append("no venue-wide comparison yet (needs two ticker readings)")
    elif excess > 0:
        c.points += min(max_pts * 0.3, excess * max_pts * 0.06)
        c.reasons.append(f"activity {excess:+.2f}% above the same moment yesterday")

    c.points = min(c.points, max_pts)
    if c.points > 0 and not c.reasons:
        c.reasons.append("activity above this token's normal level")
    return c


def _acceleration(af: AssetFeatures) -> ComponentScore:
    """Is the unusualness still building, or already fading?"""
    c = _mk("acceleration")
    f = af.primary
    max_pts = c.max_points

    if f.rvol_slope is None and f.volume_acceleration_pct is None:
        c.available = False
        c.caveats.append("DATA_UNAVAILABLE: not enough bars to measure acceleration")
        return c

    if f.rvol_slope is not None:
        if f.rvol_slope > 0:
            c.points += min(max_pts * 0.6, f.rvol_slope * max_pts * 2.0)
            c.reasons.append("relative volume still rising")
        else:
            c.caveats.append("relative volume already falling — the surge may be over")

    if f.volume_acceleration_pct is not None:
        if f.volume_acceleration_pct > 15.0:
            c.points += min(max_pts * 0.4, f.volume_acceleration_pct / 100.0 * max_pts * 0.5)
            c.reasons.append(f"volume accelerating ({f.volume_acceleration_pct:+.0f}%)")
        elif f.volume_acceleration_pct < -15.0:
            c.caveats.append(f"volume decelerating ({f.volume_acceleration_pct:+.0f}%)")

    c.points = min(c.points, max_pts)
    if c.points > 0 and not c.reasons:
        c.reasons.append("activity building rather than fading")
    return c


def _precedence(af: AssetFeatures, excess: float | None) -> ComponentScore:
    """Volume rising while price is still flat — activity before the move.

    The only component describing a *sequence* rather than a level, and the
    reason this engine exists separately from the opportunity engine.
    """
    c = _mk("precedence")
    f = af.primary
    max_pts = c.max_points

    volume_up = (f.rvol is not None and f.rvol >= 1.3) or (excess is not None and excess > 1.0)
    if not volume_up:
        c.caveats.append("volume is not elevated, so nothing is leading anything")
        return c

    if f.roc1 is None:
        c.available = False
        c.caveats.append("DATA_UNAVAILABLE: recent price change unknown")
        return c

    move = abs(f.roc1)
    if move < 0.25:
        c.points = max_pts
        c.reasons.append("volume elevated while price is still flat — activity is leading price")
    elif move < 0.75:
        c.points = max_pts * 0.55
        c.reasons.append("volume elevated with price only just beginning to move")
    else:
        c.caveats.append(f"price already moving {f.roc1:+.2f}% — the volume is not ahead of it")
    return c


def _compression(af: AssetFeatures) -> ComponentScore:
    """Coiled volatility. A token that has already expanded has told its news."""
    c = _mk("compression")
    f = af.primary
    max_pts = c.max_points

    if f.compression is None:
        c.available = False
        c.caveats.append("DATA_UNAVAILABLE: volatility compression could not be measured")
        return c

    # 0 = tightest in the lookback, 1 = widest.
    if f.compression <= 0.35:
        c.points = max_pts * (1.0 - f.compression / 0.35) * 0.8 + max_pts * 0.2
        c.reasons.append(f"volatility compressed ({f.compression:.2f}) — coiled rather than spent")
    elif f.compression >= 0.8:
        c.caveats.append(f"volatility already wide ({f.compression:.2f}) — the expansion has happened")
    else:
        c.points = max_pts * 0.25
        c.reasons.append("volatility neither compressed nor stretched")
    return c


def _earliness(maturity: float | None, range_position: float | None) -> ComponentScore:
    """A token that has already run cannot be an early discovery."""
    c = _mk("earliness")
    max_pts = c.max_points

    if maturity is None:
        c.available = False
        c.caveats.append("DATA_UNAVAILABLE: pump maturity unknown")
    else:
        if maturity <= 30.0:
            c.points += max_pts * 0.7
            c.reasons.append(f"move barely started (maturity {maturity:.0f}/100)")
        elif maturity <= 55.0:
            c.points += max_pts * 0.35
            c.reasons.append(f"move under way but not extended (maturity {maturity:.0f}/100)")
        else:
            c.caveats.append(f"move already advanced (maturity {maturity:.0f}/100) — not an early find")

    if range_position is None:
        c.caveats.append("24h range position unknown")
    elif range_position <= 0.7:
        c.points += max_pts * 0.3 * (1.0 - range_position / 0.7)
        c.reasons.append(f"at {range_position:.0%} of the 24h range, with room above")
    else:
        c.caveats.append(f"already at {range_position:.0%} of the 24h range")

    c.points = min(c.points, max_pts)
    return c


def _tradeability(af: AssetFeatures, status: LiquidityStatus | None) -> ComponentScore:
    """A filter, not a reason to be interested — hence the small weight."""
    c = _mk("tradeability")
    max_pts = c.max_points

    if status is None:
        c.available = False
        c.caveats.append("DATA_UNAVAILABLE: liquidity not assessed")
    else:
        share = {
            LiquidityStatus.EXCELLENT: 1.0,
            LiquidityStatus.GOOD: 0.85,
            LiquidityStatus.ACCEPTABLE: 0.55,
            LiquidityStatus.POOR: 0.15,
            LiquidityStatus.DANGEROUS: 0.0,
            LiquidityStatus.UNKNOWN: 0.0,
        }.get(status, 0.0)
        c.points += max_pts * 0.7 * share
        if share >= 0.55:
            c.reasons.append(f"liquidity {status.value.lower()}")
        else:
            c.caveats.append(f"liquidity {status.value.lower()} — a move here may not be tradeable")

    spread = af.spread_bps
    if spread is None:
        c.caveats.append("spread unknown")
    elif spread <= 20.0:
        c.points += max_pts * 0.3
        c.reasons.append(f"spread {spread:.0f} bps")
    else:
        c.caveats.append(f"spread {spread:.0f} bps will eat a small move")

    c.points = min(c.points, max_pts)
    return c
