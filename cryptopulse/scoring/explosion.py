"""EXPLOSION_15M_SCORE — is this about to move within the next few 15m bars?

A THIRD QUESTION, AND THE ONLY ONE THAT IS ALREADY MEASURED

The project now carries three engines, deliberately never blended:

  Opportunity (SCORE_ENGINE_V1)     "is this a good setup?"          — quality
  Discovery (DISCOVERY_ENGINE_V1)   "has this behaviour changed?"    — novelty
  Explosion (this)                  "is this about to move, soon?"   — urgency

They disagree constantly, and that disagreement is the point. A textbook daily
retest is a fine setup that will resolve over days: high opportunity, near-zero
explosion. A token whose 5m volume has quadrupled in the last three bars while
sitting a third of an ATR under resistance is the reverse — there is barely a
setup, and something is very likely to happen in the next hour. Averaging the
two would produce a number describing neither, and would make it impossible to
learn later which of the three carried any signal.

WHAT MAKES THIS ONE DIFFERENT: IT CAN BE CHECKED

Every score in this project is unvalidated, but this one is the only one whose
claim is *already being measured*. `outcomes/horizons.py` records what the price
actually did 15 minutes after every emitted signal — the exact window this score
makes a claim about. The 15m horizon table is therefore this engine's scorecard,
and once there are enough rows it will be the first component of the system that
can be shown to work or not work. That is the reason the horizon is 15 minutes
and not "soon": a claim without a window cannot be wrong, and a claim that
cannot be wrong is worthless.

WHAT IT WEIGHS, AND WHY

Everything here reads short timeframes, because a 4h indicator cannot say
anything about the next fifteen minutes.

  volume_burst (25)  RVOL on 5m and 15m. The single most reliable precursor of
                     a fast move, and the only one that is genuinely hard to
                     fake for more than a few bars.
  thrust (20)        Price rate-of-change on 5m and 15m, and whether it is
                     still increasing. A move already decelerating is not about
                     to accelerate.
  proximity (20)     Distance to the nearest resistance in ATR. This is the
                     component that makes the horizon meaningful: a level a
                     third of an ATR away can be reached in fifteen minutes; one
                     five ATR away cannot, however loud the volume is.
  compression (15)   A coiled 15m range releases fast. A range already wide has
                     spent its energy.
  book_pressure (10) Resting bid/ask imbalance and spread. Small weight on
                     purpose: a book is a snapshot of intent that can be
                     withdrawn in a second, and it is the input most easily
                     manipulated. It is also frequently absent — only the top
                     symbols get a book fetched — and absence scores zero and
                     says so.
  earliness (10)     Inverse pump maturity. A move that is 85% done does not
                     explode again in the next fifteen minutes; it retraces.

GATES ARE NOT NEGOTIABLE HERE EITHER

A hard veto from liquidity or safety zeroes this score outright rather than
reducing it. On a fifteen-minute horizon an illiquid token can "explode" ten
percent on a thousand dollars of volume, and that is a trap rather than an
opportunity. The zero is reported with its reason, never silently.

NOT A PROBABILITY

`72/100` means "this ranks above a token at 55 under the current weights". It
does not mean 72%. These weights are a hypothesis and have never been fitted;
`EXPLOSION_ENGINE_V1` and the weights fingerprint go into every journalled row
so that when the 15m horizon table is large enough to fit them, old rows are
never reinterpreted under new rules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import Timeframe
from cryptopulse.features.pipeline import AssetFeatures, TimeframeFeatures
from cryptopulse.i18n import mult, num, pct, signed
from cryptopulse.i18n import reasons as R
from cryptopulse.scoring.components import ComponentScore

log = get_logger("scoring.explosion")

__all__ = [
    "EXPLOSION_ENGINE_VERSION",
    "EXPLOSION_WEIGHTS",
    "EXPLOSION_HORIZON_MINUTES",
    "ExplosionResult",
    "ExplosionEngine",
]

EXPLOSION_ENGINE_VERSION = "EXPLOSION_ENGINE_V1"

# The window this score makes a claim about, and the window `outcomes/horizons.py`
# already measures. Changing one without the other would break the only
# validation loop in the system that closes.
EXPLOSION_HORIZON_MINUTES = 15

# Must sum to 100. The engine refuses to construct otherwise.
EXPLOSION_WEIGHTS: dict[str, float] = {
    "volume_burst": 25.0,
    "thrust": 20.0,
    "proximity": 20.0,
    "compression": 15.0,
    "book_pressure": 10.0,
    "earliness": 10.0,
}

# Above this the score is loud enough to be worth interrupting someone for.
# A stated cut, not a measured one — see the module docstring.
IMMINENT_THRESHOLD = 70.0
BUILDING_THRESHOLD = 45.0


@dataclass(slots=True)
class ExplosionResult:
    symbol: str
    score: float
    components: list[ComponentScore]
    engine_version: str
    weights_fingerprint: str
    timestamp_ms: int
    horizon_minutes: int = EXPLOSION_HORIZON_MINUTES
    vetoed: bool = False
    veto_reason: str | None = None

    @property
    def label(self) -> str:
        """Four states, in French, because this is read on a phone in a hurry."""
        if self.vetoed:
            return "BLOQUÉ"
        if self.score >= IMMINENT_THRESHOLD:
            return "IMMINENT"
        if self.score >= BUILDING_THRESHOLD:
            return "EN FORMATION"
        return "CALME"

    def why(self) -> list[str]:
        out: list[str] = []
        for c in sorted(self.components, key=lambda c: c.points, reverse=True):
            if c.points > 0:
                out.extend(c.reasons)
        return out

    def risks(self) -> list[str]:
        out: list[str] = []
        if self.veto_reason:
            out.append(self.veto_reason)
        for c in self.components:
            out.extend(c.caveats)
        return out

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            # A ranking out of 100. Never rendered with a percent sign.
            "explosion_score": round(self.score, 2),
            "explosion_label": self.label,
            "horizon_minutes": self.horizon_minutes,
            "vetoed": self.vetoed,
            "veto_reason": self.veto_reason,
            "engine_version": self.engine_version,
            "weights_fingerprint": self.weights_fingerprint,
            "timestamp_ms": self.timestamp_ms,
            "components": [c.to_dict() for c in self.components],
            "why": self.why(),
            "risks": self.risks(),
            "disclaimer": (
                f"Un classement de la probabilité d'un mouvement dans les "
                f"{self.horizon_minutes} minutes, ni une probabilité chiffrée ni un conseil. "
                "Ces pondérations n'ont jamais été validées ; c'est le tableau de la fenêtre "
                "15 min qui le fera un jour."
            ),
        }


class ExplosionEngine:
    """Stateless. One instance is safe to share across a whole scan."""

    def __init__(self) -> None:
        total = sum(EXPLOSION_WEIGHTS.values())
        if abs(total - 100.0) > 1e-6:
            raise ValueError(
                f"explosion weights must sum to 100, got {total}. "
                "Le score est présenté sur une échelle de 0 à 100 et doit y rester."
            )
        self.weights_fingerprint = self._fingerprint()

    def _fingerprint(self) -> str:
        payload = json.dumps(
            {
                "weights": EXPLOSION_WEIGHTS,
                "version": EXPLOSION_ENGINE_VERSION,
                # Part of the identity: the same weights over a different window
                # are a different claim and must not share a fingerprint.
                "horizon_minutes": EXPLOSION_HORIZON_MINUTES,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def score(
        self,
        af: AssetFeatures,
        *,
        timestamp_ms: int,
        maturity_score: float | None = None,
        hard_veto: bool = False,
        veto_reason: str | None = None,
    ) -> ExplosionResult:
        """Score one asset over the next `EXPLOSION_HORIZON_MINUTES` minutes.

        A hard veto zeroes the score rather than reducing it: on this horizon an
        illiquid token's ten-percent candle is a trap, not an opportunity, and a
        reduced score would still let it rank above a safe token.
        """
        short = af.get(Timeframe.M5)
        mid = af.get(Timeframe.M15)

        components = [
            _volume_burst(short, mid),
            _thrust(short, mid),
            _proximity(short, mid),
            _compression(mid, short),
            _book_pressure(af),
            _earliness(maturity_score, mid, short),
        ]
        total = sum(c.points for c in components)

        if hard_veto:
            return ExplosionResult(
                symbol=af.symbol,
                score=0.0,
                components=components,
                engine_version=EXPLOSION_ENGINE_VERSION,
                weights_fingerprint=self.weights_fingerprint,
                timestamp_ms=timestamp_ms,
                vetoed=True,
                veto_reason=(
                    veto_reason
                    or "blocked by a hard gate: a fast move on an untradeable book is a trap"
                ),
            )

        return ExplosionResult(
            symbol=af.symbol,
            score=max(0.0, min(100.0, total)),
            components=components,
            engine_version=EXPLOSION_ENGINE_VERSION,
            weights_fingerprint=self.weights_fingerprint,
            timestamp_ms=timestamp_ms,
        )


# --------------------------------------------------------------------------- #
# Components. Each explains itself; an unavailable input scores zero AND says so.
#
# Every one of these reads 5m or 15m only. A 4h reading cannot say anything
# about the next fifteen minutes, and letting one in would quietly turn this
# into a slower version of the opportunity score.
# --------------------------------------------------------------------------- #


def _mk(name: str) -> ComponentScore:
    return ComponentScore(name=name, points=0.0, max_points=EXPLOSION_WEIGHTS[name])


def _volume_burst(short: TimeframeFeatures | None, mid: TimeframeFeatures | None) -> ComponentScore:
    """Volume arriving now, on the two timeframes that can show it in time."""
    c = _mk("volume_burst")
    max_pts = c.max_points

    rvol_5m = short.rvol if short else None
    rvol_15m = mid.rvol if mid else None

    if rvol_5m is None and rvol_15m is None:
        c.available = False
        c.caveats.append(R.EXP_NO_RVOL())
        return c

    # The 5m reading carries most of the weight: it is the one that can change
    # inside the window this score is talking about.
    if rvol_5m is not None:
        if rvol_5m >= 1.5:
            c.points += min(max_pts * 0.6, (rvol_5m - 1.0) * max_pts * 0.30)
            c.reasons.append(R.EXP_TF_VOLUME_HIGH(timeframe="5 min", rvol=mult(rvol_5m, 1)))
        elif rvol_5m < 0.7:
            c.caveats.append(R.EXP_TF_VOLUME_LOW(timeframe="5 min", rvol=mult(rvol_5m, 1)))

    if rvol_15m is not None:
        if rvol_15m >= 1.3:
            c.points += min(max_pts * 0.4, (rvol_15m - 1.0) * max_pts * 0.25)
            c.reasons.append(R.EXP_TF_VOLUME_HIGH(timeframe="15 min", rvol=mult(rvol_15m, 1)))
        elif rvol_15m < 0.8:
            c.caveats.append(R.EXP_TF_VOLUME_LOW(timeframe="15 min", rvol=mult(rvol_15m, 1)))

    # A burst that is already fading is a worse sign than a smaller one still
    # building, so this reads the slope rather than only the level.
    if short is not None and short.rvol_slope is not None and short.rvol_slope < 0 and c.points > 0:
        c.caveats.append(R.EXP_RVOL_FALLING())

    c.points = min(c.points, max_pts)
    if c.points > 0 and not c.reasons:
        c.reasons.append(R.EXP_VOLUME_ELEVATED())
    return c


def _thrust(short: TimeframeFeatures | None, mid: TimeframeFeatures | None) -> ComponentScore:
    """Price already moving, and still speeding up rather than rolling over."""
    c = _mk("thrust")
    max_pts = c.max_points

    if short is None and mid is None:
        c.available = False
        c.caveats.append(R.EXP_NO_SHORT_HISTORY())
        return c

    roc_5m = short.roc3 if short else None       # last 15 minutes of 5m bars
    roc_5m_1 = short.roc1 if short else None     # the last 5 minutes alone
    roc_15m = mid.roc1 if mid else None

    if roc_5m is None and roc_15m is None:
        c.available = False
        c.caveats.append(R.EXP_NO_ROC())
        return c

    if roc_5m is not None:
        if roc_5m > 0.3:
            c.points += min(max_pts * 0.5, roc_5m * max_pts * 0.20)
            c.reasons.append(R.EXP_ROC_UP(change=pct(roc_5m, 2)))
        elif roc_5m < -0.3:
            c.caveats.append(R.EXP_ROC_DOWN(change=pct(roc_5m, 2)))

    if roc_15m is not None and roc_15m > 0.4:
        c.points += min(max_pts * 0.3, roc_15m * max_pts * 0.12)
        c.reasons.append(R.EXP_LAST_BAR(change=pct(roc_15m, 2)))

    # Acceleration: is the newest bar carrying more than the average of the three?
    if roc_5m is not None and roc_5m_1 is not None and roc_5m > 0:
        if roc_5m_1 > roc_5m / 3.0:
            c.points += max_pts * 0.2
            c.reasons.append(R.EXP_SPEEDING_UP())
        else:
            c.caveats.append(R.EXP_SLOWING())

    c.points = min(c.points, max_pts)
    if c.points > 0 and not c.reasons:
        c.reasons.append(R.EXP_PRICE_MOVING())
    return c


def _proximity(short: TimeframeFeatures | None, mid: TimeframeFeatures | None) -> ComponentScore:
    """How far the nearest resistance is, measured in ATR.

    This is the component that makes a fifteen-minute horizon mean something.
    Distance in percent would be useless across assets — 1% is nothing on a
    memecoin and a large move on BTC — so it is measured in the asset's own
    recent volatility. Roughly, one ATR is what this asset does in one bar.
    """
    c = _mk("proximity")
    max_pts = c.max_points

    st = None
    scale = None
    for f in (mid, short):
        if f is not None and f.structure is not None and f.structure.distance_to_resistance_atr is not None:
            st = f.structure
            scale = f.timeframe.value
            break

    if st is None:
        c.available = False
        c.caveats.append(R.EXP_NO_RESISTANCE())
        return c

    d = st.distance_to_resistance_atr
    assert d is not None  # guarded by the loop above

    if st.broke_out and not st.fake_breakout:
        c.points = max_pts
        c.reasons.append(R.EXP_THROUGH_RESISTANCE(timeframe=scale))
    elif d <= 0.5:
        c.points = max_pts * 0.9
        c.reasons.append(R.EXP_NEAR_RESISTANCE(distance=num(d), timeframe=scale))
    elif d <= 1.5:
        c.points = max_pts * (1.5 - d) / 1.0 * 0.6 + max_pts * 0.2
        c.reasons.append(R.EXP_UNDER_RESISTANCE(distance=num(d), timeframe=scale))
    elif d <= 3.0:
        c.points = max_pts * 0.1
        c.caveats.append(R.EXP_RESISTANCE_REACHABLE(distance=num(d)))
    else:
        c.caveats.append(R.EXP_RESISTANCE_TOO_FAR(distance=num(d)))

    if st.fake_breakout:
        c.points *= 0.4
        c.caveats.append(R.EXP_FAILED_BREAKOUT())

    return c


def _compression(mid: TimeframeFeatures | None, short: TimeframeFeatures | None) -> ComponentScore:
    """A coiled 15m range releases fast; a wide one has spent its energy."""
    c = _mk("compression")
    max_pts = c.max_points

    f = mid if (mid is not None and mid.compression is not None) else short
    if f is None or f.compression is None:
        c.available = False
        c.caveats.append(R.EXP_NO_COMPRESSION())
        return c

    # 0 = tightest in the lookback, 1 = widest.
    if f.compression <= 0.30:
        c.points = max_pts * (1.0 - f.compression / 0.30) * 0.75 + max_pts * 0.25
        c.reasons.append(
            R.EXP_RANGE_COMPRESSED(timeframe=f.timeframe.value, value=num(f.compression))
        )
    elif f.compression >= 0.85:
        c.caveats.append(
            R.EXP_RANGE_WIDEST(timeframe=f.timeframe.value, value=num(f.compression))
        )
    else:
        c.points = max_pts * 0.2
        c.reasons.append(R.EXP_RANGE_NEUTRAL(timeframe=f.timeframe.value))
    return c


def _book_pressure(af: AssetFeatures) -> ComponentScore:
    """Resting orders, weighted small on purpose.

    A book is intent that can be withdrawn in one second, and it is the input in
    this system most easily manipulated. It is also absent for most symbols —
    only the top of the first pass gets a book fetched — and an absent book
    scores zero and says so rather than being treated as neutral-positive.
    """
    c = _mk("book_pressure")
    max_pts = c.max_points

    if af.order_book_imbalance is None:
        c.available = False
        c.caveats.append(R.EXP_NO_BOOK())
        return c

    imb = af.order_book_imbalance
    if imb > 0.15:
        c.points += min(max_pts * 0.7, imb * max_pts * 1.4)
        c.reasons.append(R.EXP_BIDS_OUTWEIGH(imbalance=signed(imb)))
    elif imb < -0.15:
        c.caveats.append(R.EXP_ASKS_OUTWEIGH(imbalance=signed(imb)))

    if af.spread_bps is None:
        c.caveats.append(R.EXP_SPREAD_UNKNOWN())
    elif af.spread_bps <= 5.0:
        c.points += max_pts * 0.3
        c.reasons.append(R.EXP_SPREAD_OK(spread=num(af.spread_bps, 1)))
    elif af.spread_bps > 25.0:
        c.caveats.append(R.EXP_SPREAD_COSTLY(spread=num(af.spread_bps, 0)))

    c.points = min(c.points, max_pts)
    return c


def _earliness(
    maturity: float | None,
    mid: TimeframeFeatures | None,
    short: TimeframeFeatures | None,
) -> ComponentScore:
    """A move that is nearly finished does not explode again; it retraces."""
    c = _mk("earliness")
    max_pts = c.max_points

    if maturity is None:
        c.available = False
        c.caveats.append(R.EXP_MATURITY_UNKNOWN())
    else:
        if maturity <= 35.0:
            c.points += max_pts * 0.7
            c.reasons.append(R.EXP_BARELY_STARTED(maturity=num(maturity, 0)))
        elif maturity <= 60.0:
            c.points += max_pts * 0.3
            c.reasons.append(R.EXP_UNDER_WAY(maturity=num(maturity, 0)))
        else:
            c.caveats.append(R.EXP_ALREADY_MATURE(maturity=num(maturity, 0)))

    # Short-timeframe RSI is the fastest available exhaustion reading. It is a
    # caveat rather than a veto: a strong trend runs overbought for many bars.
    f = short if (short is not None and short.rsi14 is not None) else mid
    if f is None or f.rsi14 is None:
        c.caveats.append(R.EXP_RSI_UNAVAILABLE())
    elif f.rsi14 >= 80.0:
        c.caveats.append(R.EXP_RSI_STRETCHED(timeframe=f.timeframe.value, rsi=num(f.rsi14, 0)))
    elif f.rsi14 <= 70.0:
        c.points += max_pts * 0.3
        c.reasons.append(R.EXP_RSI_ROOM(timeframe=f.timeframe.value, rsi=num(f.rsi14, 0)))

    c.points = min(c.points, max_pts)
    return c
