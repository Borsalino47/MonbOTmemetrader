"""ROBINHOOD_EXPLOSION_15M_V1 — is this token about to accelerate, within ~15 min?

THE SAME QUESTION AS THE CEX ENGINE, ASKED OF DIFFERENT DATA

`scoring/explosion.py` answers this from candles: RVOL on 5m and 15m, thrust,
distance to resistance in ATR, range compression, order-book pressure. On a
token twenty minutes old none of that exists — there is no ATR, no resistance
level worth the name, no resting book. So this is a separate engine with its
own version and fingerprint, reading the only thing that does exist at this
timescale: the five-minute flow against the hour behind it.

WHY IT IS NOT THE EARLY SCORE WITH A SHORTER HORIZON

`ROBINHOOD_EARLY_SCORE_V1` asks "is a big move starting, and is there room
left" — it rewards youth, small FDV and low maturity. This asks only "is
something about to happen in the next quarter of an hour", and a token that has
been climbing for six hours can score high here while scoring poorly there.
They disagree by construction, and that disagreement is the point (invariant
37). Averaging them would produce a number describing neither.

THE HORIZON IS PART OF THE IDENTITY, NOT A PARAMETER

Same rule as the CEX engine (invariant 38): `HORIZON_MINUTES` goes into the
fingerprint, because the same weights over a different window are a different
claim. Fifteen minutes is chosen to match `outcomes/horizons.py`, so that when
a Robinhood outcome history exists this is the first Robinhood score that can
be shown to be wrong. A claim without a window cannot be wrong, and a claim
that cannot be wrong is worthless.

WHAT IT READS, AND WHY EACH ONE

  volume_burst (30)   The five-minute volume rate against the hourly average.
                      The single hardest thing to fake for more than a few
                      minutes, and on a fresh pool it is most of the signal.
  trade_burst (25)    The same ratio on trade *count*. One whale can quadruple
                      volume alone; quadrupling the number of distinct trades
                      takes a crowd. Weighted heavily because it is the part
                      that separates a real crowd from one wallet.
  buy_pressure (20)   Five-minute buys against sells. Direction, not size:
                      a burst of selling is also a burst.
  thrust (15)         Five-minute price change, and whether the hour agrees.
                      A price already falling is not about to explode upward.
  depth (10)          Enough liquidity that a move can happen at all without
                      being one person's market order.

A HARD GATE ZEROES IT

Same as the CEX engine (invariant 40) and the safety engine (invariant 72): a
safety veto or liquidity below the floor produces zero, not a reduction. On a
fifteen-minute horizon an illiquid token can "explode" ten percent on nine
hundred dollars of volume, and that is a trap rather than an opportunity. The
zero always carries its reason — a silent zero is indistinguishable from a
calm market.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.core.logging import get_logger

log = get_logger("scoring.robinhood_explosion")

__all__ = [
    "RH_EXPLOSION_VERSION",
    "RH_EXPLOSION_HORIZON_MINUTES",
    "RH_EXPLOSION_WEIGHTS",
    "RobinhoodExplosion",
    "score_explosion",
    "explosion_fingerprint",
]

RH_EXPLOSION_VERSION = "ROBINHOOD_EXPLOSION_15M_V1"

# The window this score makes a claim about. Matches outcomes/horizons.py so
# the claim is checkable once a Robinhood history exists. Part of the
# fingerprint: the same weights over a different window are a different claim.
RH_EXPLOSION_HORIZON_MINUTES = 15

RH_EXPLOSION_WEIGHTS: dict[str, float] = {
    "volume_burst": 30.0,
    "trade_burst": 25.0,
    "buy_pressure": 20.0,
    "thrust": 15.0,
    "depth": 10.0,
}

if abs(sum(RH_EXPLOSION_WEIGHTS.values()) - 100.0) > 1e-9:  # pragma: no cover - import guard
    raise ValueError("ROBINHOOD_EXPLOSION weights must sum to 100")


@dataclass(slots=True)
class RobinhoodExplosion:
    score: float
    horizon_minutes: int = RH_EXPLOSION_HORIZON_MINUTES
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    vetoed: bool = False
    veto_reason: str | None = None
    version: str = RH_EXPLOSION_VERSION
    weights_fingerprint: str = ""

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            # Never "89 % de chance" (spec §16). A rank under fixed weights.
            "label": f"{self.score:.0f}/100",
            "horizon_minutes": self.horizon_minutes,
            "components": {k: round(v, 2) for k, v in self.components.items()},
            "reasons": self.reasons,
            "caveats": self.caveats,
            "unavailable": self.unavailable,
            "vetoed": self.vetoed,
            "veto_reason": self.veto_reason,
            "version": self.version,
            "weights_fingerprint": self.weights_fingerprint,
            "note": (
                f"Configuration compatible avec une accélération dans les "
                f"~{self.horizon_minutes} minutes. Ce n'est pas une probabilité."
            ),
        }


def explosion_fingerprint(settings: RobinhoodSettings) -> str:
    payload = json.dumps(
        {
            "weights": RH_EXPLOSION_WEIGHTS,
            "version": RH_EXPLOSION_VERSION,
            # Part of the identity, not a parameter (invariant 38).
            "horizon_minutes": RH_EXPLOSION_HORIZON_MINUTES,
            "min_liquidity": settings.discovery_min_liquidity_usd,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def score_explosion(candidate, settings: RobinhoodSettings) -> RobinhoodExplosion:
    """Score one candidate over the next ~15 minutes. Never raises."""
    result = RobinhoodExplosion(score=0.0, weights_fingerprint=explosion_fingerprint(settings))

    # -- gates first ---------------------------------------------------------- #
    # A veto zeroes rather than reduces, so a trap cannot outrank a tradeable
    # token in a sorted list.
    if candidate.safety is not None and candidate.safety.hard_veto:
        blocking = candidate.safety.blocking_findings
        reason = blocking[0].label_fr if blocking else "sécurité bloquante"
        result.vetoed = True
        result.veto_reason = f"veto sécurité : {reason}"
        result.caveats.append(result.veto_reason)
        return result

    liq = candidate.liquidity_usd
    if liq is not None and liq < settings.discovery_min_liquidity_usd:
        result.vetoed = True
        result.veto_reason = (
            f"liquidité de {liq:,.0f} $ — un mouvement de 10 % ici n'est pas une opportunité"
        )
        result.caveats.append(result.veto_reason)
        return result

    points = 0.0

    # -- 1. volume burst ------------------------------------------------------ #
    cap = RH_EXPLOSION_WEIGHTS["volume_burst"]
    accel = candidate.volume_acceleration
    if accel is None:
        result.unavailable.append("volume_burst")
        result.caveats.append("Rythme du volume indisponible")
        result.components["volume_burst"] = 0.0
    else:
        if accel >= 6:
            earned = cap
            result.reasons.append(f"volume {accel:.1f}× le rythme horaire — rupture nette")
        elif accel >= 3:
            earned = cap * 0.75
            result.reasons.append(f"volume {accel:.1f}× le rythme horaire")
        elif accel >= 1.5:
            earned = cap * 0.4
            result.reasons.append(f"volume {accel:.1f}× le rythme horaire")
        else:
            earned = 0.0
            result.caveats.append(f"volume au rythme habituel ({accel:.1f}×)")
        points += earned
        result.components["volume_burst"] = earned

    # -- 2. trade burst -------------------------------------------------------- #
    cap = RH_EXPLOSION_WEIGHTS["trade_burst"]
    tx_m5 = _tx_total(candidate, "m5")
    tx_h1 = _tx_total(candidate, "h1")
    if tx_m5 is None or tx_h1 is None or tx_h1 <= 0:
        result.unavailable.append("trade_burst")
        result.caveats.append("Rythme des transactions indisponible")
        result.components["trade_burst"] = 0.0
    else:
        ratio = (tx_m5 * 12.0) / tx_h1
        if ratio >= 5:
            earned = cap
            result.reasons.append(f"{ratio:.1f}× plus de transactions — afflux d'acheteurs")
        elif ratio >= 2.5:
            earned = cap * 0.75
            result.reasons.append(f"transactions {ratio:.1f}× le rythme horaire")
        elif ratio >= 1.3:
            earned = cap * 0.4
            result.reasons.append(f"transactions {ratio:.1f}× le rythme horaire")
        else:
            earned = 0.0
            result.caveats.append(f"transactions au rythme habituel ({ratio:.1f}×)")
        points += earned
        result.components["trade_burst"] = earned

    # -- 3. buy pressure -------------------------------------------------------- #
    # Direction, not size: a burst of selling is also a burst, and scoring it
    # as imminent upside would be the most expensive confusion in this module.
    cap = RH_EXPLOSION_WEIGHTS["buy_pressure"]
    buys = candidate._sum("buys", "m5")
    sells = candidate._sum("sells", "m5")
    if buys is None or sells is None or (buys + sells) == 0:
        result.unavailable.append("buy_pressure")
        result.caveats.append("Répartition achats/ventes 5 min indisponible")
        result.components["buy_pressure"] = 0.0
    else:
        share = buys / (buys + sells)
        if share >= 0.75:
            earned = cap
            result.reasons.append(f"{share * 100:.0f} % d'achats sur 5 min")
        elif share >= 0.6:
            earned = cap * 0.65
            result.reasons.append(f"{share * 100:.0f} % d'achats sur 5 min")
        elif share >= 0.5:
            earned = cap * 0.3
            result.reasons.append(f"achats légèrement majoritaires ({share * 100:.0f} %)")
        else:
            earned = 0.0
            result.caveats.append(f"vendeurs majoritaires sur 5 min ({(1 - share) * 100:.0f} %)")
        points += earned
        result.components["buy_pressure"] = earned

    # -- 4. thrust ---------------------------------------------------------------- #
    cap = RH_EXPLOSION_WEIGHTS["thrust"]
    m5_change = _change(candidate, "m5")
    h1_change = _change(candidate, "h1")
    if m5_change is None:
        result.unavailable.append("thrust")
        result.caveats.append("Variation 5 min indisponible")
        result.components["thrust"] = 0.0
    else:
        if m5_change >= 5:
            earned = cap
            result.reasons.append(f"+{m5_change:.1f} % sur 5 min")
        elif m5_change >= 1:
            earned = cap * 0.6
            result.reasons.append(f"+{m5_change:.1f} % sur 5 min")
        elif m5_change > -1:
            earned = cap * 0.2
        else:
            earned = 0.0
            result.caveats.append(f"{m5_change:.1f} % sur 5 min — prix en repli")
        # An hour that disagrees with the last five minutes is a bounce inside
        # a decline rather than the start of a rise.
        if h1_change is not None and h1_change < -10 and earned > 0:
            earned *= 0.5
            result.caveats.append(f"rebond dans une baisse ({h1_change:.0f} % sur 1 h)")
        points += earned
        result.components["thrust"] = earned

    # -- 5. depth ------------------------------------------------------------------ #
    cap = RH_EXPLOSION_WEIGHTS["depth"]
    if liq is None:
        result.unavailable.append("depth")
        result.caveats.append("Liquidité inconnue")
        result.components["depth"] = 0.0
    elif liq >= 50_000:
        points += cap
        result.components["depth"] = cap
        result.reasons.append(f"{liq:,.0f} $ de liquidité")
    elif liq >= 10_000:
        points += cap * 0.6
        result.components["depth"] = cap * 0.6
        result.reasons.append(f"{liq:,.0f} $ de liquidité")
    else:
        points += cap * 0.25
        result.components["depth"] = cap * 0.25
        result.caveats.append(f"seulement {liq:,.0f} $ de liquidité — mouvement facile à provoquer")

    result.score = max(0.0, min(100.0, points))
    log.debug("robinhood_explosion", token=candidate.address[:10], score=round(result.score, 1))
    return result


def _tx_total(candidate, interval: str) -> float | None:
    buys = candidate._sum("buys", interval)
    sells = candidate._sum("sells", interval)
    if buys is None and sells is None:
        return None
    return (buys or 0.0) + (sells or 0.0)


def _change(candidate, interval: str) -> float | None:
    """Price change over one interval, from the deepest pool that reports it."""
    ordered = sorted(
        candidate.pools,
        key=lambda p: (p.liquidity_usd if p.liquidity_usd is not None else -1.0),
        reverse=True,
    )
    for pool in ordered:
        value = pool.price_change_pct.get(interval)
        if value is not None:
            return value
    return None
