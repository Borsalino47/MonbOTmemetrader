"""What is going wrong with a held DEX position, named one signal at a time.

WHY SEPARATE FROM THE HEALTH SCORE

Health answers "is the thesis still standing?" as one number — the thing you
glance at. But a number cannot be argued with, and the moment that actually
matters, the one where someone decides to sell, deserves named facts rather
than "42/100".

WHY SEPARATE FROM `trading/exit_risk.py`

The Binance engine reads structure breaks, bias flips and spread widening. None
of those exist here. What exists instead is a contract whose owner can still act
on it and a pool someone can withdraw. Two of the signals below have no CEX
analogue at all — a token becoming a honeypot *after* you bought it, and the
liquidity provider pulling the floor out — and they are precisely the ones that
take a position to zero rather than down.

SEVERITY IS COUNTED, NEVER SUMMED (invariant 52)

Five cosmetic warnings must not outweigh one withdrawn pool. The report has no
total at all, and a test asserts it.

THE INVALIDATION OUTRANKS EVERYTHING (invariant 51)

It was agreed before the position existed and before any of this was emotional.
A price through it ends the thesis whatever the flow is doing.

NOTHING HERE SELLS ANYTHING

The report is read by `RobinhoodTradeDecisionEngine.decide_position`, which
produces a recommendation a human then acts on manually.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.core.logging import get_logger
from cryptopulse.i18n import money, num, pct
from cryptopulse.risk.robinhood_safety import RugRisk
from cryptopulse.trading.exit_risk import ExitSignal, Severity

log = get_logger("trading.robinhood_exit_risk")

__all__ = ["RobinhoodExitRisk", "assess_robinhood_exit_risk"]

_RANK = {Severity.MINOR: 0, Severity.MAJOR: 1, Severity.CRITICAL: 2}


@dataclass(slots=True)
class RobinhoodExitRisk:
    signals: list[ExitSignal] = field(default_factory=list)
    # Kept apart from the signals because the decision rules treat it as a
    # different kind of fact: the term agreed in advance.
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
            # Counts, deliberately with no total: summing would let five
            # cosmetic warnings outweigh one withdrawn pool.
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


def assess_robinhood_exit_risk(position, candidate, *, current_price=None) -> RobinhoodExitRisk:
    """Name every deterioration on a held token. Never raises."""
    report = RobinhoodExitRisk()
    price = current_price if current_price is not None else candidate.price_usd

    def add(name: str, severity: Severity, message: str) -> None:
        report.signals.append(ExitSignal(name=name, severity=severity, message=message))

    # -- 1. The terms of the trade -------------------------------------------- #
    level = getattr(position, "invalidation_price", None)
    if level and price is not None and price <= level:
        report.invalidation_broken = True
        add(
            "INVALIDATION",
            Severity.CRITICAL,
            "Niveau d'invalidation franchi — la raison initiale de l'achat n'est plus valide",
        )
    elif not level:
        report.unavailable.append("invalidation")

    # -- 2. Can it still be sold? --------------------------------------------- #
    # These have no Binance analogue and are the reason this engine exists: a
    # contract's owner can act on it *after* the purchase, which is the whole
    # mechanism of a rug.
    safety = getattr(candidate, "safety", None)
    if safety is None or not safety.analysed:
        report.unavailable.append("safety")
    else:
        entry_rug = getattr(position, "entry_rug_risk", None)
        for finding in safety.blocking_findings:
            add(
                f"SAFETY_{finding.code}",
                Severity.CRITICAL,
                f"Sécurité : {finding.label_fr}"
                + (f" — {finding.detail}" if finding.detail else ""),
            )
        if safety.rug_risk is RugRisk.CRITICAL:
            add("RUG_CRITICAL", Severity.CRITICAL, "Risque de rug devenu CRITIQUE")
        elif safety.rug_risk is RugRisk.HIGH:
            add("RUG_HIGH", Severity.MAJOR, "Risque de rug devenu ÉLEVÉ")

        # A level that *worsened* since entry is a different fact from a level
        # that was always mediocre: something changed under the position.
        if entry_rug and safety.rug_risk.value != entry_rug and _worsened(entry_rug, safety.rug_risk):
            add(
                "RUG_WORSENED",
                Severity.MAJOR,
                f"Risque de rug dégradé depuis l'achat : {entry_rug} → {safety.rug_risk.value}",
            )

    # -- 3. Is the floor still there? ----------------------------------------- #
    now = getattr(candidate, "liquidity_usd", None)
    at_entry = getattr(position, "entry_liquidity_usd", None)
    if now is None:
        report.unavailable.append("liquidity")
    elif at_entry:
        ratio = now / at_entry
        if ratio <= 0.3:
            add(
                "LIQUIDITY_PULLED",
                Severity.CRITICAL,
                f"Liquidité retirée : {money(now)} contre {money(at_entry)} à l'achat",
            )
        elif ratio <= 0.6:
            add(
                "LIQUIDITY_DRAINING",
                Severity.MAJOR,
                f"Pool amputé de {pct(-(1 - ratio) * 100, 0)} depuis l'achat",
            )
        elif ratio <= 0.85:
            add("LIQUIDITY_SOFT", Severity.MINOR, f"Pool en recul léger : {money(now)}")

    # -- 4. Who is on the other side? ----------------------------------------- #
    ratio = getattr(candidate, "buy_sell_ratio_h1", None)
    if ratio is None:
        report.unavailable.append("flow")
    elif ratio < 0.4:
        add("SELLING_RUSH", Severity.MAJOR, f"Ruée vendeuse sur 1 h ({num(ratio, 2)}×)")
    elif ratio < 0.7:
        add("SELLERS_LEADING", Severity.MINOR, f"Vendeurs majoritaires sur 1 h ({num(ratio, 2)}×)")

    # -- 5. Has interest simply gone? ----------------------------------------- #
    volume_h1 = getattr(candidate, "volume_h1", None)
    if volume_h1 is not None and volume_h1 < 500.0:
        add(
            "VOLUME_GONE",
            Severity.MAJOR,
            f"Volume quasi nul sur la dernière heure ({money(volume_h1)}) — "
            "une sortie déplacerait le prix à elle seule",
        )

    # -- 6. What the engine thinks now ---------------------------------------- #
    early = getattr(candidate, "early", None)
    entry_early = getattr(position, "entry_early", None)
    if early is not None and entry_early is not None and early.score < entry_early - 25:
        add(
            "SCORE_COLLAPSE",
            Severity.MINOR,
            f"Score de précocité passé de {num(entry_early, 0)} à {num(early.score, 0)}",
        )

    return report


# Worst to best, matching the decision engine's ordering.
_RUG_ORDER = {"UNKNOWN": 0, "CRITICAL": 1, "HIGH": 2, "MEDIUM": 3, "LOW": 4}


def _worsened(entry: str, now: RugRisk) -> bool:
    """Did the level get worse, rather than merely change?

    UNKNOWN is the lowest rank, so a token that became unanalysable counts as
    worse — which it is: we can no longer tell whether the exit still exists.
    """
    return _RUG_ORDER.get(now.value, 0) < _RUG_ORDER.get(entry, 0)
