"""ROBINHOOD_POSITION_HEALTH_V1 — is the buy thesis still standing on-chain?

WHY THE BINANCE HEALTH ENGINE CANNOT BE REUSED

`trading/health.py` weighs structure, momentum, volume, invalidation, score
drift and tradeability — five of the six read candles and a resting order book.
A token on a DEX has neither. What it has instead is a pool, a contract, and
what has traded through them since the position was opened.

So the components are different, and one of them has no Binance equivalent at
all:

    sellability (30)   Can the position still be closed? A contract that became
                       a honeypot, a sell tax that was raised after you bought,
                       a blacklist you are now on. This is the way a DEX
                       position goes to zero, and no amount of price appreciation
                       compensates for it — hence the largest weight.
    liquidity (25)     Pool depth now against depth at entry. A pool that has
                       lost most of its depth is a rug in progress, and the
                       price on screen stops meaning anything you could get.
    flow (20)          Buys against sells over the last hour. Is anyone still
                       on the other side.
    invalidation (15)  Distance to the line agreed before the position existed.
    drift (10)         The early score now against the early score at entry.

HEALTH IS NOT PnL (invariant 49)

A position up 300 % on a pool that just lost 80 % of its depth is unhealthy, and
saying so is the entire point — that is the moment the gain is about to be
impossible to realise. PnL is shown beside this, and the two are meant to
disagree visibly.

AN UNKNOWN HEALTH IS NOT A BAD ONE (invariant 50)

Below half coverage the score is `None` and the decision becomes SURVEILLER,
never VENDRE. Telling someone to sell because an indexer request failed is the
worst false alarm this engine could produce — and on a chain whose indexers are
young, a failed request is not rare.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.core.logging import get_logger
from cryptopulse.i18n import money, num, pct

log = get_logger("trading.robinhood_health")

__all__ = [
    "RH_HEALTH_VERSION",
    "RH_HEALTH_WEIGHTS",
    "RobinhoodHealth",
    "compute_robinhood_health",
]

RH_HEALTH_VERSION = "ROBINHOOD_POSITION_HEALTH_V1"

# Below this share of measurable weight the score is None rather than low.
MIN_COVERAGE = 0.5

RH_HEALTH_WEIGHTS: dict[str, float] = {
    "sellability": 30.0,
    "liquidity": 25.0,
    "flow": 20.0,
    "invalidation": 15.0,
    "drift": 10.0,
}

if abs(sum(RH_HEALTH_WEIGHTS.values()) - 100.0) > 1e-9:  # pragma: no cover - import guard
    raise ValueError("ROBINHOOD_POSITION_HEALTH weights must sum to 100")


@dataclass(slots=True)
class HealthComponent:
    name: str
    label_fr: str
    points: float
    max_points: float
    available: bool = True
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    # A ceiling this component imposes on the whole score, or None.
    #
    # Found by writing the test for invariant 49 and watching it fail against
    # this engine: a position up 300 % whose pool had lost 90 % of its depth
    # scored 73.5 and rendered as SAINE, because liquidity is only 25 of the
    # 100 points and everything else was perfect. But a withdrawn pool is not
    # "a quarter of a problem" — it is the reason a DEX position goes to zero,
    # and a merely-reduced score is exactly how a veto stops meaning anything
    # (invariants 40 and 72, same rule, applied here).
    caps_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label_fr": self.label_fr,
            "points": round(self.points, 1),
            "max_points": self.max_points,
            "available": self.available,
            "caps_at": self.caps_at,
            "reasons": self.reasons,
            "caveats": self.caveats,
        }


@dataclass(slots=True)
class RobinhoodHealth:
    score: float | None                  # None when too little could be measured
    coverage: float
    components: list[HealthComponent] = field(default_factory=list)
    version: str = RH_HEALTH_VERSION

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
                "Santé de la thèse d'achat, pas du gain. Une position en forte hausse "
                "dont le pool a perdu sa profondeur est en mauvaise santé — c'est "
                "précisément le moment où le gain devient irréalisable."
            ),
        }


def compute_robinhood_health(position, candidate, *, current_price: float | None = None):
    """Score the surviving thesis for one held token.

    `position` supplies the state that justified entering; `candidate` is what
    the indexers say now. The comparison is the measurement — neither side
    alone means anything.
    """
    price = current_price if current_price is not None else candidate.price_usd
    components = [
        _sellability(candidate),
        _liquidity(position, candidate),
        _flow(candidate),
        _invalidation(position, price),
        _drift(position, candidate),
    ]

    available = [c for c in components if c.available]
    total_weight = sum(c.max_points for c in available)
    coverage = total_weight / sum(RH_HEALTH_WEIGHTS.values())

    if coverage < MIN_COVERAGE:
        return RobinhoodHealth(score=None, coverage=coverage, components=components)

    earned = sum(c.points for c in available)
    # Rescaled over the weight actually measured, so a missing component does
    # not quietly lower every score.
    score = (earned / total_weight) * 100.0 if total_weight else 0.0
    score = max(0.0, min(100.0, score))

    # A single catastrophic reading caps the whole score rather than costing it
    # points. The lowest cap wins; a cap is never averaged with anything.
    caps = [c.caps_at for c in components if c.caps_at is not None]
    if caps:
        score = min(score, min(caps))

    return RobinhoodHealth(score=score, coverage=coverage, components=components)


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #


def _mk(name: str, label: str) -> HealthComponent:
    return HealthComponent(name=name, label_fr=label, points=0.0, max_points=RH_HEALTH_WEIGHTS[name])


def _sellability(candidate) -> HealthComponent:
    """Can this still be sold at all?

    Re-read every cycle rather than trusted from entry: the powers that make a
    token unsellable — mint, blacklist, tax changes, ownership recovery — are
    exercised *after* people have bought, which is the whole mechanism.
    """
    c = _mk("sellability", "Revendabilité")
    safety = getattr(candidate, "safety", None)
    if safety is None or not safety.analysed:
        c.available = False
        c.caveats.append("Contrôle de sécurité indisponible ce cycle")
        return c

    if safety.hard_veto:
        blocking = safety.blocking_findings
        c.points = 0.0
        # Nothing else about the position matters if it cannot be sold.
        c.caps_at = 15.0
        c.caveats.append(
            blocking[0].label_fr if blocking else f"Sécurité bloquante : {safety.label_fr}"
        )
        return c

    score = safety.score
    if score is None:
        c.available = False
        c.caveats.append("Score de sécurité non calculable")
        return c

    c.points = c.max_points * max(0.0, min(1.0, score / 100.0))
    if score >= 85.0:
        c.reasons.append(f"Contrat toujours sain, sécurité {num(score, 0)}/100")
    else:
        c.caveats.append(f"Sécurité retombée à {num(score, 0)}/100")
    return c


def _liquidity(position, candidate) -> HealthComponent:
    """Depth now against depth at entry.

    The ratio matters more than the absolute figure: a pool that was always
    small is a known risk taken deliberately, while a pool that has halved
    since entry is a fact that changed under the position.
    """
    c = _mk("liquidity", "Liquidité du pool")
    now = getattr(candidate, "liquidity_usd", None)
    at_entry = getattr(position, "entry_liquidity_usd", None)

    if now is None:
        c.available = False
        c.caveats.append("Profondeur du pool inconnue ce cycle")
        return c

    if not at_entry:
        # No baseline: score the absolute depth rather than refusing, and say
        # that the comparison is missing.
        c.points = c.max_points * (0.8 if now >= 25_000 else 0.4 if now >= 5_000 else 0.1)
        c.caveats.append("Aucune profondeur enregistrée à l'entrée — pas de comparaison possible")
        return c

    ratio = now / at_entry
    if ratio >= 0.9:
        c.points = c.max_points
        c.reasons.append(f"Profondeur du pool intacte : {money(now)}")
    elif ratio >= 0.7:
        c.points = c.max_points * 0.7
        c.caveats.append(f"Pool en recul de {pct(-(1 - ratio) * 100, 0)} depuis l'entrée")
    elif ratio >= 0.5:
        c.points = c.max_points * 0.35
        c.caps_at = 55.0
        c.caveats.append(f"Pool amputé de près de la moitié : {money(now)}")
    else:
        c.points = 0.0
        # A rug in progress. However well the rest reads, this is not a healthy
        # position — the gain on screen is one that cannot be realised.
        c.caps_at = 30.0
        c.caveats.append(
            f"Profondeur effondrée à {money(now)}, contre {money(at_entry)} à l'entrée"
        )
    return c


def _flow(candidate) -> HealthComponent:
    """Is anyone still on the other side of a sale?"""
    c = _mk("flow", "Flux d'ordres")
    ratio = getattr(candidate, "buy_sell_ratio_h1", None)
    if ratio is None:
        c.available = False
        c.caveats.append("Flux acheteurs/vendeurs indisponible")
        return c

    if ratio >= 1.5:
        c.points = c.max_points
        c.reasons.append(f"Achats toujours dominants sur 1 h ({num(ratio, 2)}×)")
    elif ratio >= 1.0:
        c.points = c.max_points * 0.7
        c.reasons.append(f"Flux équilibré sur 1 h ({num(ratio, 2)}×)")
    elif ratio >= 0.6:
        c.points = c.max_points * 0.35
        c.caveats.append(f"Vendeurs majoritaires sur 1 h ({num(ratio, 2)}×)")
    else:
        c.points = 0.0
        c.caveats.append(f"Vente massivement dominante sur 1 h ({num(ratio, 2)}×)")
    return c


def _invalidation(position, price: float | None) -> HealthComponent:
    """Distance to the line agreed before the position existed."""
    c = _mk("invalidation", "Niveau d'invalidation")
    level = getattr(position, "invalidation_price", None)
    if level is None or not level or price is None:
        c.available = False
        c.caveats.append("Aucun niveau d'invalidation enregistré à l'entrée")
        return c

    if price <= level:
        c.points = 0.0
        # The terms agreed before the position existed (invariant 51).
        c.caps_at = 20.0
        c.caveats.append("Niveau d'invalidation franchi — la raison d'achat n'existe plus")
        return c

    margin = (price - level) / level * 100.0
    if margin >= 25.0:
        c.points = c.max_points
        c.reasons.append(f"Prix encore {pct(margin, 0)} au-dessus de l'invalidation")
    elif margin >= 10.0:
        c.points = c.max_points * 0.6
        c.reasons.append(f"Marge de {pct(margin, 0)} avant l'invalidation")
    else:
        c.points = c.max_points * 0.2
        c.caveats.append(f"Plus que {pct(margin, 0)} avant le niveau d'invalidation")
    return c


def _drift(position, candidate) -> HealthComponent:
    """What the early engine thinks now, against what it thought at entry."""
    c = _mk("drift", "Évolution du score")
    early = getattr(candidate, "early", None)
    now = early.score if early else None
    at_entry = getattr(position, "entry_early", None)

    if now is None:
        c.available = False
        c.caveats.append("Score de précocité indisponible ce cycle")
        return c
    if at_entry is None:
        c.points = c.max_points * 0.5
        c.caveats.append("Aucun score de précocité enregistré à l'entrée")
        return c

    delta = now - at_entry
    if delta >= 0:
        c.points = c.max_points
        c.reasons.append(f"Score de précocité stable ou en hausse ({num(now, 0)}/100)")
    elif delta >= -15:
        c.points = c.max_points * 0.6
        c.caveats.append(f"Score de précocité en repli de {num(-delta, 0)} points")
    else:
        c.points = 0.0
        c.caveats.append(
            f"Score de précocité effondré : {num(at_entry, 0)} à l'entrée, {num(now, 0)} maintenant"
        )
    return c
