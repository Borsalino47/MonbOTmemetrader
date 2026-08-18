"""Turning the Robinhood readings into something readable in five seconds.

WHY THIS MODULE EXISTS AT ALL, AND WHAT IT IS NOT

Nothing here computes an opinion. Every value it returns is derived from
numbers the engines already produced, and no threshold in `RobinhoodSettings`
is read to *decide* anything — only to say what the decision was measured
against. If this module were deleted the decisions would be identical; only the
screen would get worse.

That separation is deliberate. It was written after a real token, DRCO, came
back from a live Android with:

    Explosion 15 min : 92/100        ← the biggest number on the card
    Confiance        : 50/100
    Liquidité        : 2 471 $       ← minimum requis 25 000 $
    ~2 minutes of history

The engine refused the purchase, correctly. But "92/100" was the thing the eye
landed on, and a spectacular raw score visually outranking every guard rail is
how a guard rail stops working. So:

A SCORE IS NEVER A PROBABILITY, AND NEVER LOOKS LIKE ONE

92/100 is a configuration rank under fixed weights. It is not "92 % chance".
Every predictive score returned here carries `not_a_probability` and the
sentence that says so.

A SCORE IS NEVER SHOWN WITHOUT ITS RELIABILITY

The same 92 means two different things at 50/100 data confidence and at 95/100.
`describe_score` returns the pair, and the *tone* — what the interface colours
it — is the weaker of the two. A high score on thin data is amber, never green.
Green is reserved for a decision (spec §12), never earned by one number.

EASY TO MOVE IS NOT THE SAME AS WORTH BUYING (spec §20)

A pool with 2 471 dollars in it can double on one buy *because* it is thin.
That is a property of the pool, not an opportunity, and the caveat says so in
those words rather than calling it "liquidité faible".

UNKNOWN IS NOT SAFE (spec §10, invariant 71)

`RUG RISK FAIBLE` on a token where four checks came back empty is a sentence
that reads as reassurance and is not one. The safety block returned here always
carries coverage and names what was not checked, and the headline becomes
"aucun risque critique détecté" — which is what was actually established.
"""

from __future__ import annotations

from cryptopulse.core.logging import get_logger
from cryptopulse.i18n import money, num

log = get_logger("scoring.robinhood_presentation")

__all__ = [
    "RELIABILITY_BANDS",
    "describe_reliability",
    "describe_score",
    "describe_history",
    "describe_safety",
    "describe_liquidity",
    "why_not_buy",
    "build_presentation",
]

# Data quality only — never the token's potential (spec §3).
# (floor, emoji, label)
RELIABILITY_BANDS: tuple[tuple[float, str, str], ...] = (
    (90.0, "🟢", "TRÈS BONNE"),
    (75.0, "🟢", "BONNE"),
    (60.0, "🟡", "MOYENNE"),
    (40.0, "🟠", "FAIBLE"),
    (0.0, "🔴", "TRÈS FAIBLE"),
)

# Above this a score may be rendered as strong. Below it, a high score is shown
# with a warning tone however high it is: the number is real, the ground under
# it is not.
_TRUSTWORTHY = 60.0

# A token younger than this has windows that are mechanically spectacular —
# "12× le rythme horaire" over two minutes of existence — and the card says so
# (spec §13).
VERY_RECENT_SECONDS = 300.0

# Which measures need how much elapsed history, mirrored from
# `_CONFIDENCE_INPUTS` so the screen can name the gap rather than only score it.
_HISTORY_REQUIREMENTS: tuple[tuple[str, float], ...] = (
    ("accélération du volume (5 min)", 300.0),
    ("volume 1 h", 3600.0),
    ("acheteurs uniques 1 h", 3600.0),
    ("ratio achats/ventes 1 h", 3600.0),
)


def describe_reliability(score: float | None) -> dict:
    """The data-quality band. Says nothing about the token."""
    if score is None:
        return {
            "score": None,
            "label_fr": "INCONNUE",
            "emoji": "⚫",
            "trustworthy": False,
            "note": "Fiabilité des données non calculable.",
        }
    emoji, label = next(
        (e, lbl) for floor, e, lbl in RELIABILITY_BANDS if score >= floor
    )
    return {
        "score": round(score, 1),
        "label_fr": label,
        "emoji": emoji,
        "trustworthy": score >= _TRUSTWORTHY,
        "note": (
            "Qualité et couverture des données, pas le potentiel du token."
        ),
    }


def describe_score(
    label: str, score: float | None, confidence: float | None, *, horizon: str | None = None
) -> dict:
    """A predictive score and the reliability of the data under it.

    `tone` is the weaker of the two on purpose. A 92 measured on two minutes of
    history is not a strong signal — it is a strong number, and the interface
    must not let the first read as the second.
    """
    reliability = describe_reliability(confidence)
    if score is None:
        tone = "unknown"
    elif not reliability["trustworthy"]:
        # However high the score, the ground under it is thin.
        tone = "caution"
    elif score >= 70.0:
        tone = "strong"
    elif score >= 45.0:
        tone = "moderate"
    else:
        tone = "weak"

    return {
        "label_fr": label,
        "horizon_fr": horizon,
        "score": None if score is None else round(score, 1),
        # Never "%". 92/100 is a rank under fixed weights (spec §1).
        "display": "—" if score is None else f"{num(score, 0)}/100",
        "tone": tone,
        "reliability": reliability,
        "not_a_probability": True,
        "note": "Score de configuration — ce n'est pas une probabilité.",
        "warning": (
            None
            if reliability["trustworthy"] or score is None
            else f"Score élevé mais fiabilité {reliability['label_fr'].lower()} "
                 f"({num(reliability['score'], 0)}/100) : les données sont encore minces."
            if score >= 70.0
            else f"Fiabilité {reliability['label_fr'].lower()} "
                 f"({num(reliability['score'], 0)}/100)."
        ),
    }


def describe_history(candidate) -> dict:
    """How much history actually exists, and what that is not enough for.

    Put at the top of the card rather than several lines down: on a token four
    minutes old this is the fact that qualifies every other number.
    """
    age = getattr(candidate, "pool_age_seconds", None)
    if age is None:
        return {
            "available_seconds": None,
            "available_fr": "inconnu",
            "very_recent": False,
            "insufficient_for": [],
            "warning": "Âge du pool inconnu — la profondeur des mesures est invérifiable.",
        }

    short = [label for label, needed in _HISTORY_REQUIREMENTS if age < needed]
    very_recent = age < VERY_RECENT_SECONDS
    return {
        "available_seconds": round(age, 1),
        "available_fr": _duration(age),
        "very_recent": very_recent,
        "insufficient_for": [
            {
                "measure": label,
                "needs_fr": _duration(needed),
                "have_fr": _duration(age),
            }
            for label, needed in _HISTORY_REQUIREMENTS
            if age < needed
        ],
        "warning": (
            "TOKEN EXTRÊMEMENT RÉCENT — certaines accélérations sont calculées sur "
            "une fenêtre encore incomplète et doivent être lues avec prudence."
            if very_recent
            else (
                f"{len(short)} mesure(s) reposent sur une fenêtre plus longue que "
                f"l'historique disponible ({_duration(age)})."
                if short
                else None
            )
        ),
    }


def describe_safety(safety) -> dict:
    """Risk found, coverage, and what was never checked — three separate facts.

    Spec §9-10: "RUG RISK FAIBLE" on a token with four empty checks reads as
    reassurance and is not one. The headline becomes what was actually
    established — no critical risk *detected* — and the coverage sits beside it.
    """
    if safety is None or not getattr(safety, "analysed", False):
        return {
            "analysed": False,
            "headline_fr": "SÉCURITÉ NON ANALYSÉE",
            "emoji": "⚫",
            "risk_level_fr": "INCONNU",
            "coverage_pct": None,
            "coverage_label_fr": "INCONNUE",
            "unknown_checks": [],
            "confidence_fr": "AUCUNE",
            "note": (
                "Aucun contrôle n'a été effectué. Absence de contrôle n'est pas "
                "absence de danger."
            ),
        }

    coverage = float(getattr(safety, "coverage", 0.0) or 0.0)
    coverage_pct = coverage * 100.0
    unknown = [
        f.label_fr for f in safety.findings if f.severity.value == "UNKNOWN"
    ]
    blocking = safety.blocking_findings

    if blocking:
        headline, emoji = f"DANGER : {blocking[0].label_fr}", "🔴"
    elif coverage_pct >= 85.0:
        headline, emoji = "Aucun risque critique détecté", "🟢"
    else:
        # Deliberately not "RUG RISK FAIBLE": what was established is that
        # nothing critical was *found*, on a partial reading.
        headline, emoji = "Aucun risque critique détecté sur les contrôles effectués", "🟢"

    return {
        "analysed": True,
        "headline_fr": headline,
        "emoji": emoji,
        # The engine's level, kept separate from the headline so a level and a
        # coverage can visibly disagree.
        "risk_level_fr": _RUG_FR.get(safety.rug_risk.value, safety.rug_risk.value),
        "coverage_pct": round(coverage_pct, 0),
        "coverage_label_fr": _coverage_label(coverage_pct),
        "unknown_checks": unknown,
        "unknown_count": len(unknown),
        # Not the same question as the risk level: how much do we trust the
        # verdict, rather than how bad is it (spec §10).
        "confidence_fr": _coverage_label(coverage_pct),
        "partial": coverage_pct < 85.0,
        "note": (
            f"Couverture {num(coverage_pct, 0)} % des contrôles."
            + (
                f" {len(unknown)} contrôle(s) inconnu(s) — un contrôle non effectué "
                "n'est jamais lu comme un contrôle réussi."
                if unknown
                else ""
            )
        ),
    }


def describe_liquidity(candidate, minimum_usd: float) -> dict:
    """Depth in dollars, and what too little of it actually costs.

    Spec §8 and §20: a pool with 2 471 dollars in it is not "un peu faible", it
    is a pool where a single order moves the price and an exit may not be
    executable. And a token that can explode *because* it is thin is not an
    opportunity — that distinction is the whole reason this block exists.
    """
    depth = getattr(candidate, "liquidity_usd", None)
    if depth is None:
        return {
            "usd": None,
            "display_fr": "inconnue",
            "minimum_usd": minimum_usd,
            "minimum_fr": money(minimum_usd),
            "severity": "unknown",
            "headline_fr": "LIQUIDITÉ INCONNUE",
            "consequences": [
                "Impossible de savoir si une sortie serait exécutable.",
            ],
        }

    ratio = depth / minimum_usd if minimum_usd else 1.0
    if ratio >= 1.0:
        severity, headline = "ok", "Liquidité suffisante"
        consequences: list[str] = []
    elif ratio >= 0.5:
        severity, headline = "low", "LIQUIDITÉ SOUS LE MINIMUM"
        consequences = [
            "Glissement supérieur à la normale sur une sortie.",
        ]
    else:
        severity, headline = "critical", "LIQUIDITÉ TRÈS FAIBLE"
        consequences = [
            "Glissement potentiellement important à l'achat comme à la vente.",
            "Prix facilement manipulable par un seul ordre.",
            "Sortie potentiellement difficile, voire impossible au prix affiché.",
        ]

    return {
        "usd": depth,
        "display_fr": money(depth),
        "minimum_usd": minimum_usd,
        "minimum_fr": money(minimum_usd),
        "ratio": round(ratio, 3),
        "severity": severity,
        "headline_fr": headline,
        "consequences": consequences,
    }


def why_not_buy(decision, *, liquidity: dict | None = None) -> list[dict]:
    """Every reason the purchase was refused, worst first.

    The section the user asked for by name (spec §7). Built from the decision's
    own checks and vetoes rather than recomputed, so it can never disagree with
    the answer it is explaining.
    """
    if decision is None or decision.action.value == "BUY":
        return []

    out: list[dict] = []

    # Vetoes first: they are not "one criterion among several".
    for reason in decision.blocking:
        if any(c["why"] == reason for c in decision.checks):
            continue   # a failed floor, handled below with its numbers
        out.append({
            "kind": "veto",
            "label_fr": "Veto",
            "detail_fr": reason,
            "value_fr": None,
            "minimum_fr": None,
        })

    for check in decision.checks:
        if check["passed"]:
            continue
        entry = {
            "kind": "unavailable" if check["unavailable"] else "floor",
            "label_fr": check["name"].capitalize(),
            "detail_fr": check["why"],
            "value_fr": None if check["value"] is None else f"{num(check['value'], 0)}/100",
            "minimum_fr": (
                None if check["threshold"] is None else f"{num(check['threshold'], 0)}/100"
            ),
        }
        # Liquidity is dollars, not points: showing "2471/100" would be absurd.
        if check["name"] == "liquidité" and liquidity is not None:
            entry["value_fr"] = liquidity["display_fr"]
            entry["minimum_fr"] = liquidity["minimum_fr"]
        out.append(entry)

    return out


def build_presentation(candidate, settings) -> dict:
    """Everything the card needs, derived and never recomputed.

    Attached to the token payload so the interface reads one block instead of
    reassembling the reasoning from six others — which is how two screens end
    up disagreeing about the same token.
    """
    confidence = candidate.confidence.score if candidate.confidence else None
    liquidity = describe_liquidity(candidate, settings.buy_min_liquidity_usd)
    history = describe_history(candidate)

    explosion = describe_score(
        "Explosion",
        candidate.explosion.score if candidate.explosion else None,
        confidence,
        horizon="15 min",
    )
    early = describe_score(
        "Précocité",
        candidate.early.score if candidate.early else None,
        confidence,
    )

    caveats: list[str] = []
    # Spec §20, the distinction the whole card turns on.
    if (
        candidate.explosion is not None
        and candidate.explosion.score >= 70.0
        and liquidity["severity"] == "critical"
    ):
        caveats.append(
            "Forte capacité de mouvement détectée, MAIS sur une liquidité très "
            "faible : un mouvement y est facile à provoquer et difficile à "
            "négocier proprement. Ce n'est pas la même chose qu'une opportunité."
        )
    if history["very_recent"]:
        caveats.append(history["warning"])

    missing = list(candidate.confidence.missing) if candidate.confidence else []

    return {
        "decision_first": True,
        "why_not_buy": why_not_buy(candidate.decision, liquidity=liquidity),
        "scores": {"explosion": explosion, "early": early},
        "reliability": describe_reliability(confidence),
        "history": history,
        "safety": describe_safety(candidate.safety),
        "liquidity": liquidity,
        "missing_data": {
            "count": len(missing),
            "fields": missing,
            "note": (
                "Ces absences alimentent la fiabilité des données. Une donnée "
                "manquante n'est jamais comptée comme une donnée favorable."
            ),
        },
        "caveats": caveats,
    }


# --------------------------------------------------------------------------- #
# Wording
# --------------------------------------------------------------------------- #

_RUG_FR = {
    "LOW": "FAIBLE",
    "MEDIUM": "MOYEN",
    "HIGH": "ÉLEVÉ",
    "CRITICAL": "CRITIQUE",
    "UNKNOWN": "INCONNU",
}


def _coverage_label(pct: float | None) -> str:
    if pct is None:
        return "INCONNUE"
    for floor, _, label in RELIABILITY_BANDS:
        if pct >= floor:
            return label
    return "TRÈS FAIBLE"


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{num(seconds, 0)} s"
    if seconds < 3600:
        return f"{num(seconds / 60, 0)} min"
    return f"{num(seconds / 3600, 1)} h"
