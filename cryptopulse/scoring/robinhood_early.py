"""ROBINHOOD_EARLY_SCORE_V1 — is a big move *starting*, on this token, now?

A FOURTH QUESTION, AND WHY IT IS NOT ANY OF THE OTHER THREE

  Opportunity (SCORE_ENGINE_V1)  "is this a good setup?"        — CEX, hours/days
  Discovery (DISCOVERY_V1)       "has behaviour just changed?"  — CEX, hours
  Explosion (EXPLOSION_V1)       "will it move in 15 minutes?"  — CEX, minutes
  Early (this)                   "is a move starting *here*?"   — on-chain, minutes

The three existing engines all read candles, order books and multi-timeframe
structure. None of that exists for a token minted eleven minutes ago: there is
no 4h series, no swing structure, no resting book. What there is instead is
flow — volume, transactions, unique buyers, liquidity, and how fast each is
changing. So this is a different engine reading different inputs, not the
opportunity score with new thresholds.

It is also deliberately biased toward the *beginning* of a move. A token
already up 400 % scores badly here even though it looks spectacular, because
the question is not "is something happening" but "is there anything left". That
is what `ROBINHOOD_PUMP_MATURITY` measures, and the early score reads it as an
input rather than duplicating it.

THREE OUTPUTS, ONE SNAPSHOT

  early_score       0-100  how much this looks like the start of something
  pump_maturity     0-100  0 = not started, 100 = probably very late
  data_confidence   0-100  how much of the above rests on data we actually have

They are separate on purpose (spec §52): a token two minutes old can genuinely
score high on earliness and low on confidence at the same time, and collapsing
them would either hide the opportunity or hide the ignorance. The UI shows
both, and no BUY is authorised on thin data (spec §53).

WHAT NO PART OF THIS IS

None of these numbers is a probability, and none of the weights has been fitted
to anything: there is no Robinhood outcome history yet, and inventing one from
Binance would be worse than admitting the gap (spec §54). They are a stated
hypothesis, versioned and fingerprinted, so the day there is a history the old
scores are never silently reinterpreted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.core.logging import get_logger
from cryptopulse.i18n import num

log = get_logger("scoring.robinhood_early")

__all__ = [
    "EARLY_ENGINE_VERSION",
    "EARLY_WEIGHTS",
    "MATURITY_VERSION",
    "ComponentScore",
    "EarlyScore",
    "PumpMaturity",
    "DataConfidence",
    "score_early",
    "assess_maturity",
    "assess_confidence",
    "early_fingerprint",
]

EARLY_ENGINE_VERSION = "ROBINHOOD_EARLY_SCORE_V1"
MATURITY_VERSION = "ROBINHOOD_PUMP_MATURITY_V1"
CONFIDENCE_VERSION = "ROBINHOOD_DATA_CONFIDENCE_V1"

# Seven components, 100 points. Flow dominates structure because on a token
# this young there is no structure — only who is buying and how fast.
EARLY_WEIGHTS: dict[str, float] = {
    "volume_acceleration": 20.0,   # is the current rate above the hourly average
    "transaction_acceleration": 15.0,  # same question on trade count, harder to fake with one whale
    "buyer_dominance": 15.0,       # buys vs sells, and unique buyers vs sellers
    "freshness": 15.0,             # how young the pool is, with a floor
    "liquidity_quality": 15.0,     # enough depth to enter and exit at all
    "earliness": 10.0,             # inverse pump maturity
    "room_left": 10.0,             # FDV small enough that a move is still possible
}


@dataclass(slots=True)
class ComponentScore:
    """One weighted component, always able to explain itself."""

    name: str
    label_fr: str
    points: float
    max_points: float
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    # True when the inputs this component needs were not available at all. It
    # scores zero either way, but "absent" and "measured and poor" are
    # different facts and only one of them is the token's fault.
    unavailable: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label_fr": self.label_fr,
            "points": round(self.points, 2),
            "max_points": self.max_points,
            "reasons": self.reasons,
            "caveats": self.caveats,
            "unavailable": self.unavailable,
        }


@dataclass(slots=True)
class EarlyScore:
    score: float
    components: list[ComponentScore] = field(default_factory=list)
    engine_version: str = EARLY_ENGINE_VERSION
    weights_fingerprint: str = ""

    @property
    def why(self) -> list[str]:
        out: list[str] = []
        for c in self.components:
            out.extend(c.reasons)
        return out

    @property
    def risks(self) -> list[str]:
        out: list[str] = []
        for c in self.components:
            out.extend(c.caveats)
        return out

    @property
    def unavailable_components(self) -> list[str]:
        return [c.name for c in self.components if c.unavailable]

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            # Never a percentage. 84/100 is a rank under fixed weights, and
            # there is no calibration behind it.
            "label": f"{self.score:.0f}/100",
            "components": [c.to_dict() for c in self.components],
            "why": self.why,
            "risks": self.risks,
            "unavailable": self.unavailable_components,
            "engine_version": self.engine_version,
            "weights_fingerprint": self.weights_fingerprint,
        }


@dataclass(slots=True)
class PumpMaturity:
    """0 = probably has not started, 100 = probably very late."""

    score: float
    reasons: list[str] = field(default_factory=list)
    known: bool = True
    version: str = MATURITY_VERSION

    @property
    def is_late(self) -> bool:
        return self.score >= 70.0

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "label": f"{self.score:.0f}/100",
            "is_late": self.is_late,
            "reasons": self.reasons,
            "known": self.known,
            "version": self.version,
        }


@dataclass(slots=True)
class DataConfidence:
    score: float
    present: int
    total: int
    missing: list[str] = field(default_factory=list)
    version: str = CONFIDENCE_VERSION

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "label": f"{self.score:.0f}/100",
            "present": self.present,
            "total": self.total,
            "missing": self.missing,
            "version": self.version,
        }


def early_fingerprint(settings: RobinhoodSettings) -> str:
    payload = json.dumps(
        {
            "weights": EARLY_WEIGHTS,
            "version": EARLY_ENGINE_VERSION,
            "maturity_version": MATURITY_VERSION,
            "min_liquidity": settings.discovery_min_liquidity_usd,
            "max_age_hours": settings.discovery_max_age_hours,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Pump maturity — read first, because earliness depends on it
# --------------------------------------------------------------------------- #


def assess_maturity(candidate) -> PumpMaturity:
    """How far along is this move already?

    Deliberately built from price extension and flow exhaustion rather than
    from a chart pattern: there is no chart yet. Every signal that fires is
    named, and a token about which nothing is known returns `known=False` with
    a mid-scale score rather than a confident zero — "probably has not started"
    is a claim, and it must not be made from an absence of data.
    """
    reasons: list[str] = []
    points = 0.0
    inputs_seen = 0

    change_h1 = _first(candidate.pools, "price_change_pct", "h1")
    change_h6 = _first(candidate.pools, "price_change_pct", "h6")
    change_h24 = _first(candidate.pools, "price_change_pct", "h24")

    # 1. Vertical extension. The single strongest sign that the easy part is over.
    for value, window, weight in ((change_h1, "1 h", 25.0), (change_h6, "6 h", 20.0),
                                  (change_h24, "24 h", 15.0)):
        if value is None:
            continue
        inputs_seen += 1
        if value >= 300:
            points += weight
            reasons.append(f"+{value:.0f} % sur {window} — mouvement déjà très étendu")
        elif value >= 100:
            points += weight * 0.6
            reasons.append(f"+{value:.0f} % sur {window} — mouvement bien engagé")
        elif value >= 30:
            points += weight * 0.25
            reasons.append(f"+{value:.0f} % sur {window}")
        elif value < -20:
            # A retracement after a run is late, not early: the move happened
            # and is now being given back.
            points += weight * 0.5
            reasons.append(f"{value:.0f} % sur {window} — repli après hausse")

    # 2. Volume climax: the current five-minute rate far above the 24h average
    #    means the crowd has already arrived.
    m5 = candidate._sum("volume_usd", "m5")
    h24 = candidate._sum("volume_usd", "h24")
    if m5 is not None and h24 is not None and h24 > 0:
        inputs_seen += 1
        # m5 * 288 is the 24h-equivalent rate of the last five minutes.
        climax = (m5 * 288.0) / h24
        if climax >= 8:
            points += 20.0
            reasons.append(f"volume actuel {climax:.0f}× la moyenne 24 h — climax probable")
        elif climax >= 4:
            points += 10.0
            reasons.append(f"volume actuel {climax:.0f}× la moyenne 24 h")

    # 3. Sellers taking over. Buyers thinning while sellers accelerate is the
    #    shape of a top, and it is visible in flow before it is visible in price.
    buys = candidate._sum("buys", "m5")
    sells = candidate._sum("sells", "m5")
    if buys is not None and sells is not None and (buys + sells) > 0:
        inputs_seen += 1
        sell_share = sells / (buys + sells)
        if sell_share > 0.6:
            points += 20.0
            reasons.append(f"{sell_share * 100:.0f} % de ventes sur 5 min — vendeurs majoritaires")
        elif sell_share > 0.5:
            points += 8.0
            reasons.append(f"{sell_share * 100:.0f} % de ventes sur 5 min")

    if inputs_seen == 0:
        # Nothing to judge on. 50 rather than 0: claiming a move has not started
        # is a claim, and it cannot be made from missing data.
        return PumpMaturity(
            score=50.0,
            reasons=["Aucune donnée de mouvement — maturité inconnue, valeur neutre affichée"],
            known=False,
        )

    return PumpMaturity(score=max(0.0, min(100.0, points)), reasons=reasons, known=True)


# --------------------------------------------------------------------------- #
# Data confidence
# --------------------------------------------------------------------------- #

# (attribute, French label, seconds of history the figure needs to be real).
#
# The third column is the part that matters on this chain. An indexer will
# happily return an `h1` volume for a pool ninety seconds old, but that hour
# mostly predates the pool — the figure exists and means nothing. Same
# reasoning as invariant 14: a window that has not elapsed is absent, not
# settled. A five-minute figure needs five minutes; a price needs nothing.
_CONFIDENCE_INPUTS: tuple[tuple[str, str, float], ...] = (
    ("price_usd", "prix", 0.0),
    ("liquidity_usd", "liquidité", 0.0),
    ("fdv_usd", "FDV", 0.0),
    ("pool_age_seconds", "âge du pool", 0.0),
    ("volume_acceleration", "accélération du volume (5 min)", 300.0),
    ("volume_h1", "volume 1 h", 3600.0),
    ("buyers_h1", "acheteurs uniques 1 h", 3600.0),
    ("buy_sell_ratio_h1", "ratio achats/ventes 1 h", 3600.0),
)


def assess_confidence(candidate) -> DataConfidence:
    """How much of the picture is actually there.

    Two things make an input absent, and the second is the one that matters on
    this chain:

    1. the source did not return the field;
    2. **the window it claims to cover has not elapsed.** An indexer will
       happily return an `h1` volume for a pool ninety seconds old, but that
       hour mostly predates the pool. Counting it would make the youngest
       tokens — the ones this screen exists to surface — look like the
       best-measured ones.

    Deliberately per-input rather than one blanket cap on age: at ten minutes
    the five-minute figures are genuinely real and the hourly ones are not, and
    a single ramp would either discard the first or accept the second.

    Kept apart from the score itself (spec §52): a token minted two minutes ago
    can legitimately look early *and* be barely measurable, and averaging the
    two into one number would hide whichever fact the reader needed.
    """
    missing: list[str] = []
    present = 0
    age = candidate.pool_age_seconds

    for attr, label, needs_seconds in _CONFIDENCE_INPUTS:
        if getattr(candidate, attr, None) is None:
            missing.append(label)
            continue
        if needs_seconds > 0:
            if age is None:
                missing.append(f"{label} — âge du pool inconnu, profondeur invérifiable")
                continue
            if age < needs_seconds:
                missing.append(f"{label} — fenêtre non écoulée ({age / 60:.0f} min d'historique)")
                continue
        present += 1

    total = len(_CONFIDENCE_INPUTS)
    return DataConfidence(
        score=100.0 * present / total, present=present, total=total, missing=missing
    )


# --------------------------------------------------------------------------- #
# The early score
# --------------------------------------------------------------------------- #


def _first(pools, attr: str, key: str) -> float | None:
    """One interval value, from the deepest pool that reports it."""
    ordered = sorted(
        pools, key=lambda p: (p.liquidity_usd if p.liquidity_usd is not None else -1.0), reverse=True
    )
    for pool in ordered:
        value = getattr(pool, attr, {}).get(key)
        if value is not None:
            return value
    return None


def score_early(
    candidate, settings: RobinhoodSettings, *, maturity: PumpMaturity | None = None
) -> EarlyScore:
    """Score one candidate. Never raises; missing inputs score zero and say so."""
    maturity = maturity or assess_maturity(candidate)
    components: list[ComponentScore] = []

    def add(name: str, label: str, points: float, reasons=None, caveats=None, unavailable=False):
        components.append(ComponentScore(
            name=name, label_fr=label, points=points, max_points=EARLY_WEIGHTS[name],
            reasons=list(reasons or []), caveats=list(caveats or []), unavailable=unavailable,
        ))

    # -- 1. volume acceleration --------------------------------------------- #
    accel = candidate.volume_acceleration
    cap = EARLY_WEIGHTS["volume_acceleration"]
    if accel is None:
        add("volume_acceleration", "Accélération du volume", 0.0,
            caveats=["Accélération du volume indisponible"], unavailable=True)
    elif accel >= 3:
        add("volume_acceleration", "Accélération du volume", cap,
            reasons=[f"volume actuel {num(accel, 1)}× le rythme horaire"])
    elif accel >= 1.5:
        add("volume_acceleration", "Accélération du volume", cap * 0.6,
            reasons=[f"volume {num(accel, 1)}× le rythme horaire"])
    elif accel >= 1.0:
        add("volume_acceleration", "Accélération du volume", cap * 0.25,
            reasons=[f"volume légèrement au-dessus du rythme horaire ({num(accel, 1)}×)"])
    else:
        add("volume_acceleration", "Accélération du volume", 0.0,
            caveats=[f"volume en dessous du rythme horaire ({num(accel, 1)}×)"])

    # -- 2. transaction acceleration ----------------------------------------- #
    # Trade count rather than dollars: one whale can move volume, but not the
    # number of distinct trades.
    tx_m5 = _sum_pair(candidate, "buys", "m5", "sells", "m5")
    tx_h1 = _sum_pair(candidate, "buys", "h1", "sells", "h1")
    cap = EARLY_WEIGHTS["transaction_acceleration"]
    if tx_m5 is None or tx_h1 is None or tx_h1 <= 0:
        add("transaction_acceleration", "Accélération des transactions", 0.0,
            caveats=["Rythme des transactions indisponible"], unavailable=True)
    else:
        ratio = (tx_m5 * 12.0) / tx_h1
        if ratio >= 3:
            add("transaction_acceleration", "Accélération des transactions", cap,
                reasons=[f"{num(ratio, 1)}× plus de transactions que le rythme horaire"])
        elif ratio >= 1.5:
            add("transaction_acceleration", "Accélération des transactions", cap * 0.6,
                reasons=[f"transactions {num(ratio, 1)}× le rythme horaire"])
        elif ratio >= 1.0:
            add("transaction_acceleration", "Accélération des transactions", cap * 0.25,
                reasons=[f"transactions au rythme horaire ({num(ratio, 1)}×)"])
        else:
            add("transaction_acceleration", "Accélération des transactions", 0.0,
                caveats=[f"transactions en ralentissement ({num(ratio, 1)}×)"])

    # -- 3. buyer dominance --------------------------------------------------- #
    cap = EARLY_WEIGHTS["buyer_dominance"]
    ratio = candidate.buy_sell_ratio_h1
    buyers = candidate.buyers_h1
    if ratio is None:
        add("buyer_dominance", "Domination des acheteurs", 0.0,
            caveats=["Ratio achats/ventes indisponible"], unavailable=True)
    else:
        points = 0.0
        reasons, caveats = [], []
        if ratio >= 3:
            points = cap * 0.7
            reasons.append(f"{num(ratio, 1)} achats par vente")
        elif ratio >= 1.5:
            points = cap * 0.45
            reasons.append(f"{num(ratio, 1)} achats par vente")
        elif ratio >= 1.0:
            points = cap * 0.2
            reasons.append(f"achats et ventes équilibrés ({num(ratio, 1)})")
        else:
            caveats.append(f"plus de ventes que d'achats ({num(ratio, 2)})")
        # Unique buyers is the part that is hard to fake: one wallet can make
        # forty buys, but it stays one buyer.
        if buyers is not None and buyers >= 30:
            points += cap * 0.3
            reasons.append(f"{buyers:.0f} acheteurs distincts sur 1 h")
        elif buyers is not None and buyers >= 10:
            points += cap * 0.15
            reasons.append(f"{buyers:.0f} acheteurs distincts sur 1 h")
        elif buyers is not None:
            caveats.append(f"seulement {buyers:.0f} acheteurs distincts sur 1 h")
        add("buyer_dominance", "Domination des acheteurs", min(points, cap), reasons, caveats)

    # -- 4. freshness ---------------------------------------------------------- #
    cap = EARLY_WEIGHTS["freshness"]
    age = candidate.pool_age_seconds
    if age is None:
        add("freshness", "Fraîcheur", 0.0, caveats=["Âge du pool inconnu"], unavailable=True)
    elif age < 120:
        # Deliberately not the maximum. Under two minutes there is barely any
        # flow to read, so a top score here would be confidence without data —
        # the confidence figure is where that shows, but the score should not
        # reward pure novelty either.
        add("freshness", "Fraîcheur", cap * 0.6,
            reasons=[f"pool ouvert il y a {age:.0f} s"],
            caveats=["moins de 2 minutes d'historique — presque rien n'est mesurable"])
    elif age < 900:
        add("freshness", "Fraîcheur", cap, reasons=[f"pool ouvert il y a {age / 60:.0f} min"])
    elif age < 3600:
        add("freshness", "Fraîcheur", cap * 0.7, reasons=[f"pool ouvert il y a {age / 60:.0f} min"])
    elif age < 6 * 3600:
        add("freshness", "Fraîcheur", cap * 0.35, reasons=[f"pool ouvert il y a {num(age / 3600, 1)} h"])
    else:
        add("freshness", "Fraîcheur", 0.0, caveats=[f"pool déjà âgé de {age / 3600:.0f} h"])

    # -- 5. liquidity quality --------------------------------------------------- #
    cap = EARLY_WEIGHTS["liquidity_quality"]
    liq = candidate.liquidity_usd
    floor = settings.discovery_min_liquidity_usd
    if liq is None:
        add("liquidity_quality", "Liquidité", 0.0,
            caveats=["Liquidité inconnue — impossible de juger une sortie"], unavailable=True)
    elif liq < floor:
        add("liquidity_quality", "Liquidité", 0.0,
            caveats=[f"liquidité de {liq:,.0f} $ — sortie non garantie"])
    elif liq >= 100_000:
        add("liquidity_quality", "Liquidité", cap, reasons=[f"{liq:,.0f} $ de liquidité"])
    elif liq >= 25_000:
        add("liquidity_quality", "Liquidité", cap * 0.7, reasons=[f"{liq:,.0f} $ de liquidité"])
    else:
        add("liquidity_quality", "Liquidité", cap * 0.35,
            reasons=[f"{liq:,.0f} $ de liquidité"],
            caveats=["liquidité faible — le slippage sera important"])
    if candidate.liquidity_is_partial and components[-1].points > 0:
        components[-1].caveats.append("total de liquidité partiel : un pool n'a pas répondu")

    # -- 6. earliness ------------------------------------------------------------ #
    cap = EARLY_WEIGHTS["earliness"]
    if not maturity.known:
        add("earliness", "Précocité", 0.0,
            caveats=["Maturité du mouvement inconnue"], unavailable=True)
    else:
        points = cap * max(0.0, (100.0 - maturity.score) / 100.0)
        if maturity.score >= 70:
            add("earliness", "Précocité", points,
                caveats=[f"mouvement probablement tardif ({maturity.score:.0f}/100)"])
        else:
            add("earliness", "Précocité", points,
                reasons=[f"mouvement encore précoce ({maturity.score:.0f}/100 de maturité)"])

    # -- 7. room left -------------------------------------------------------------- #
    cap = EARLY_WEIGHTS["room_left"]
    fdv = candidate.fdv_usd
    if fdv is None:
        add("room_left", "Marge restante", 0.0,
            caveats=["FDV inconnue — marge de progression non évaluable"], unavailable=True)
    elif fdv < 100_000:
        add("room_left", "Marge restante", cap, reasons=[f"FDV de {fdv:,.0f} $"])
    elif fdv < 1_000_000:
        add("room_left", "Marge restante", cap * 0.6, reasons=[f"FDV de {fdv:,.0f} $"])
    elif fdv < 10_000_000:
        add("room_left", "Marge restante", cap * 0.25, reasons=[f"FDV de {fdv:,.0f} $"])
    else:
        add("room_left", "Marge restante", 0.0,
            caveats=[f"FDV de {fdv:,.0f} $ — la marge d'un x est faible"])

    # A component resting on a window the pool has not lived through still
    # scores — spec §52 is explicit that a young token may look early while
    # being barely measurable, and the confidence figure is where that shows.
    # But it must say so, or the reasons list would read as evidence when it is
    # partly extrapolation. The decision layer refuses a BUY on thin data
    # (§53); this is what tells the reader why.
    age = candidate.pool_age_seconds
    if age is not None:
        for component, needs in (
            ("volume_acceleration", 300.0),
            ("transaction_acceleration", 300.0),
            ("buyer_dominance", 3600.0),
        ):
            if age >= needs:
                continue
            for c in components:
                if c.name == component and not c.unavailable:
                    c.caveats.append(
                        f"fenêtre non écoulée : {age / 60:.0f} min d'historique pour une "
                        f"mesure sur {needs / 60:.0f} min"
                    )

    total = sum(c.points for c in components)
    result = EarlyScore(
        score=max(0.0, min(100.0, total)),
        components=components,
        weights_fingerprint=early_fingerprint(settings),
    )
    log.debug("robinhood_early", token=candidate.address[:10], score=round(result.score, 1))
    return result


def _sum_pair(candidate, attr_a: str, key_a: str, attr_b: str, key_b: str) -> float | None:
    a = candidate._sum(attr_a, key_a)
    b = candidate._sum(attr_b, key_b)
    if a is None and b is None:
        return None
    return (a or 0.0) + (b or 0.0)
