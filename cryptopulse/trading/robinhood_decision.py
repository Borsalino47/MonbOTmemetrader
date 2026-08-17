"""ROBINHOOD_TRADE_DECISION_V1 — the same six words, from different evidence.

WHY THIS IS NOT `trading/decision.py` WITH A FLAG

The two engines answer the same question — should I open a position? — and they
share the six decisions, the six colours and the vocabulary, because a user who
learns what 🟢 means on one screen must not have to relearn it on the other
(spec §21). What they do not share is a single input.

    Binance                      Robinhood Chain
    ------------------------     ------------------------------------------
    opportunity score            early score (flow, not candles)
    setup state machine          pool age and what has traded since minting
    liquidity rank (ACCEPTABLE)  liquidity in dollars, because the question is
                                 whether the position can be closed at all
    CEX safety score             ROBINHOOD_SAFETY_V1 + RUG_RISK from GoPlus
    pump maturity from candles   maturity from an indexer's rolling windows

A shared engine with a mode switch would have to hold both sets of thresholds,
both fingerprints and both notions of liquidity, and every future change to one
market would have to be reasoned about in the other. Worse, one fingerprint
covering both would mean changing a Binance floor silently reinterprets a
Robinhood decision taken last week. So: two engines, two fingerprints, one
vocabulary.

THE BAR IS HIGHER HERE, DELIBERATELY

A listed pair can be sold badly. A token minted this morning can become
unsellable. The floors in `RobinhoodSettings` sit above their Binance
equivalents for that reason, and the liquidity floor is an absolute dollar
depth rather than a rank.

SECURITY OUTRANKS EVERY SCORE (spec §18-19)

An honeypot with an early score of 100, an explosion of 100 and perfect
momentum is ⚫ NE PAS ACHETER. `SafetyReport.hard_veto` already folds in
honeypot, RUG RISK HIGH, CRITICAL and UNKNOWN; this engine adds the ones that
are about the pool rather than the contract. A veto is never a deduction: a
merely reduced score still outranks a sound token in a sorted list, which is
precisely how a veto stops meaning anything (invariants 40, 72).

CONVERGENCE, NOT A MAXIMUM

Every floor must clear at once. A test sets the early score to 100 with the
rest at the floor and asserts the answer is not a buy.

TWO SOURCES THAT DISAGREE ARE NOT AN AVERAGE

The cross-check is optional — it costs a second indexer call and only runs on
demand. When it is present and the two sources disagree, that blocks: a price
they disagree about is a price nobody should act on (invariant 75). When it is
absent, the decision can still be a buy but can never reach TRÈS FORTE, and the
caveat is attached. Same shape as the discovery score on the Binance side, and
for the same reason.

THIS ENGINE PLACES NO ORDERS

It produces a recommendation. The user acts manually and then tells the
application what they did. There is no order-placing code in this repository
and none may be added (spec §39-40).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.core.logging import get_logger
from cryptopulse.i18n import money, num
from cryptopulse.risk.robinhood_safety import RugRisk
from cryptopulse.trading.decision import (
    PRESENTATION,
    SignalStrength,
    TradeAction,
)

log = get_logger("trading.robinhood_decision")

__all__ = [
    "RH_TRADE_ENGINE_VERSION",
    "RobinhoodTradeDecision",
    "RobinhoodTradeDecisionEngine",
]

RH_TRADE_ENGINE_VERSION = "ROBINHOOD_TRADE_DECISION_V1"

# The cross-check's states, compared as plain strings rather than by importing
# `Agreement`. Importing it would create the cycle
# `robinhood_decision` -> `robinhood_detail` -> `robinhood_discovery` ->
# `robinhood_decision`, which is exactly the cycle `i18n/labels.py` already had
# to break — and for the same reason the fix is the same: an engine that reads
# a value does not need to import the module that defines it.
_AGREE = "AGREE"
_DISAGREE = "DISAGREE"

# Ordered worst to best, so "at most LOW" is a comparison rather than a set.
_RUG_ORDER: dict[RugRisk, int] = {
    RugRisk.UNKNOWN: 0,
    RugRisk.CRITICAL: 1,
    RugRisk.HIGH: 2,
    RugRisk.MEDIUM: 3,
    RugRisk.LOW: 4,
}

_STRENGTH_FR = {
    SignalStrength.STANDARD: "CORRECTE",
    SignalStrength.STRONG: "FORTE",
    SignalStrength.VERY_STRONG: "TRÈS FORTE",
}

_DISCLAIMER = (
    "Une recommandation produite par des règles fixes, jamais validées sur des "
    "résultats réels. Ce n'est ni un conseil, ni une probabilité. Un token "
    "récent peut devenir invendable : la sécurité prime sur tous les scores. "
    "Aucun ordre n'est passé automatiquement — vous décidez et vous exécutez "
    "vous-même."
)


@dataclass(slots=True)
class RobinhoodTradeDecision:
    """One decision about one token, with the arithmetic left visible."""

    address: str
    symbol: str | None
    action: TradeAction
    strength: SignalStrength | None          # buys only; None everywhere else
    timestamp_ms: int
    price_usd: float | None

    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)

    engine_version: str = RH_TRADE_ENGINE_VERSION
    weights_fingerprint: str = ""

    @property
    def emoji(self) -> str:
        return PRESENTATION[self.action][0]

    @property
    def label_fr(self) -> str:
        return PRESENTATION[self.action][1]

    @property
    def tone(self) -> str:
        return PRESENTATION[self.action][2]

    @property
    def is_actionable(self) -> bool:
        return self.action in (TradeAction.BUY, TradeAction.SELL, TradeAction.REDUCE)

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "symbol": self.symbol,
            "action": self.action.value,
            "emoji": self.emoji,
            "label_fr": self.label_fr,
            "tone": self.tone,
            "strength": self.strength.value if self.strength else None,
            "strength_fr": _STRENGTH_FR[self.strength] if self.strength else None,
            "timestamp_ms": self.timestamp_ms,
            "price_usd": self.price_usd,
            "reasons": self.reasons,
            "risks": self.risks,
            "checks": self.checks,
            "blocking": self.blocking,
            "is_actionable": self.is_actionable,
            "engine_version": self.engine_version,
            "weights_fingerprint": self.weights_fingerprint,
            "disclaimer": _DISCLAIMER,
        }


class RobinhoodTradeDecisionEngine:
    """Stateless, like its Binance sibling. Anti-noise lives in the hysteresis
    layer, so this stays replayable over a single moment in isolation."""

    def __init__(self, settings: RobinhoodSettings | None = None) -> None:
        self.cfg = settings or RobinhoodSettings()
        try:
            self.max_rug_risk = RugRisk(self.cfg.buy_max_rug_risk.upper())
        except ValueError as exc:
            raise ValueError(
                f"CP_ROBINHOOD_BUY_MAX_RUG_RISK={self.cfg.buy_max_rug_risk!r} n'est pas un "
                f"niveau de rug risk. Attendu : {', '.join(r.value for r in RugRisk)}"
            ) from exc
        self.weights_fingerprint = self._fingerprint()

    def _fingerprint(self) -> str:
        """Every threshold that can change the answer, and nothing else.

        Same rule as the three scoring engines: change a floor and the
        fingerprint changes, so a decision journalled last week is never reread
        as though it had been taken under today's rules (invariant 13).
        """
        payload = json.dumps(
            {
                "version": RH_TRADE_ENGINE_VERSION,
                "buy_min_early": self.cfg.buy_min_early,
                "buy_min_explosion": self.cfg.buy_min_explosion,
                "buy_min_confidence": self.cfg.buy_min_confidence,
                "buy_min_safety": self.cfg.buy_min_safety,
                "buy_max_maturity": self.cfg.buy_max_maturity,
                "buy_min_liquidity_usd": self.cfg.buy_min_liquidity_usd,
                "danger_liquidity_usd": self.cfg.danger_liquidity_usd,
                "buy_max_rug_risk": self.max_rug_risk.value,
                "watch_min_early": self.cfg.watch_min_early,
                "watch_min_confidence": self.cfg.watch_min_confidence,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    # ---------------------------------------------------------------- entry #

    def decide_entry(
        self,
        candidate,
        *,
        now_ms: int,
        cross_check=None,
    ) -> RobinhoodTradeDecision:
        """Should a position be opened in a token not currently held?

        Three answers and no others: 🟢 ACHETER, 🟡 SURVEILLER, ⚫ NE PAS
        ACHETER. CONSERVER, RÉDUIRE and VENDRE are about a position that
        exists; offering them for a token nobody holds would be meaningless.
        """
        decision = RobinhoodTradeDecision(
            address=candidate.address,
            symbol=candidate.symbol,
            action=TradeAction.AVOID,
            strength=None,
            timestamp_ms=now_ms,
            price_usd=candidate.price_usd,
            weights_fingerprint=self.weights_fingerprint,
        )

        # 1. Hard vetoes. Outright, and each one carries its own reason —
        #    a veto with an empty `blocking` list would render as "non
        #    analysé" on a token that was fully analysed (invariant 73).
        vetoes = self._hard_vetoes(candidate, cross_check)
        if vetoes:
            decision.action = TradeAction.AVOID
            decision.blocking = vetoes
            decision.risks = vetoes + _token_risks(candidate)[:3]
            return decision

        # 2. The floors, each recorded whether or not it cleared, so the answer
        #    is arguable rather than merely stated.
        checks = self._entry_checks(candidate)
        decision.checks = [c.to_dict() for c in checks]
        failed = [c for c in checks if not c.passed]

        if not failed:
            decision.action = TradeAction.BUY
            decision.strength = self._strength(checks, cross_check)
            decision.reasons = _buy_reasons(candidate)
            decision.risks = _token_risks(candidate)[:5]
            if cross_check is None:
                decision.risks.append(
                    "prix et liquidité non recoupés avec une deuxième source — "
                    "la convergence repose sur un critère de moins"
                )
            return decision

        decision.blocking = [c.why for c in failed]

        # 3. Not a buy. An input that is missing is not a low reading and must
        #    not be scored as one: it produces ⚫, never 🟡 (spec §51, §53).
        unavailable = [c for c in failed if c.unavailable]
        if unavailable:
            decision.action = TradeAction.AVOID
            decision.risks = [c.why for c in unavailable] + _token_risks(candidate)[:3]
            return decision

        early = candidate.early.score if candidate.early else None
        confidence = candidate.confidence.score if candidate.confidence else None
        if (
            early is not None
            and confidence is not None
            and early >= self.cfg.watch_min_early
            and confidence >= self.cfg.watch_min_confidence
        ):
            decision.action = TradeAction.WATCH
            decision.reasons = (candidate.early.why if candidate.early else [])[:4]
            decision.risks = [c.why for c in failed][:4]
            return decision

        decision.action = TradeAction.AVOID
        decision.risks = [c.why for c in failed][:4] or _token_risks(candidate)[:4]
        return decision

    # -------------------------------------------------------------- holding #

    def decide_position(self, position, candidate, health, exit_risk, *, now_ms, current_price=None):
        """What to do about a token already held: 🔵 / 🟡 / 🟠 / 🔴.

        ACHETER is deliberately not among the answers. Recommending a purchase
        of something already owned is a different instruction — adding to a
        position — which this product does not model and must not appear to.

        The rules are ordered by how much argument they need. A broken
        invalidation needs none: it was agreed before the position existed and
        it ends the thesis whatever the flow is doing (invariant 51).
        """
        price = current_price if current_price is not None else candidate.price_usd
        decision = RobinhoodTradeDecision(
            address=candidate.address,
            symbol=candidate.symbol,
            action=TradeAction.HOLD,
            strength=None,
            timestamp_ms=now_ms,
            price_usd=price,
            weights_fingerprint=self.weights_fingerprint,
        )
        decision.risks = exit_risk.messages(6)

        # 1. The terms of the trade. Nothing outranks this, by design.
        if exit_risk.invalidation_broken:
            decision.action = TradeAction.SELL
            decision.blocking = [
                "SETUP INVALIDÉ — la raison initiale de l'achat n'est plus valide"
            ]
            return decision

        critical = exit_risk.critical
        if critical:
            decision.action = TradeAction.SELL
            decision.blocking = [s.message for s in critical]
            return decision

        # 2. An unknown health is not a bad one (invariant 50). On a chain whose
        #    indexers are young, a failed request is ordinary, and telling
        #    someone to sell because of one is the worst false alarm here.
        score = health.score
        if score is None:
            decision.action = TradeAction.WATCH
            decision.reasons = [
                "santé de la position non calculable — trop de données manquantes ce cycle"
            ]
            decision.risks = decision.risks or ["données insuffisantes pour juger"]
            return decision

        # 3. Weight of evidence, counted by severity rather than summed.
        major = len(exit_risk.major)
        minor = len(exit_risk.minor)

        if score < 30.0 or major >= 3:
            decision.action = TradeAction.SELL
            decision.blocking = [s.message for s in exit_risk.major[:4]]
            return decision

        if score < 50.0 or major >= 2:
            decision.action = TradeAction.REDUCE
            decision.reasons = health.reasons()[:2]
            return decision

        if score < 70.0 or major >= 1 or minor >= 3:
            decision.action = TradeAction.WATCH
            decision.reasons = health.reasons()[:3]
            return decision

        decision.action = TradeAction.HOLD
        decision.reasons = health.reasons()[:4]
        return decision

    # --------------------------------------------------------------- checks #

    def _hard_vetoes(self, candidate, cross_check) -> list[str]:
        """Conditions that produce ⚫ whatever the scores say.

        Each describes a way of losing the whole position that a high score
        does not compensate for. They are listed rather than counted: three
        vetoes is not worse than one — both are simply not a purchase.
        """
        out: list[str] = []
        safety_vetoed = False

        safety = candidate.safety
        if safety is None:
            out.append(
                "sécurité non analysée — aucun achat ne peut être recommandé sur "
                "un token que personne n'a contrôlé"
            )
        elif safety.hard_veto:
            safety_vetoed = True
            blocking = safety.blocking_findings
            if blocking:
                out.extend(f"veto de sécurité : {f.label_fr}" for f in blocking[:3])
            else:
                # hard_veto is also true on the level alone; the level is then
                # the reason, and there is always one.
                out.append(f"veto de sécurité : {safety.label_fr}")

        liquidity = candidate.liquidity_usd
        if liquidity is not None and liquidity < self.cfg.danger_liquidity_usd:
            out.append(
                f"liquidité de {money(liquidity)} — sous ce niveau une sortie n'est "
                "pas exécutable au prix affiché"
            )

        # The explosion score relays the safety veto as its own gate, so on a
        # honeypot both engines produce the same fact in slightly different
        # words. Found by running it: the card showed "veto de sécurité :
        # HONEYPOT" immediately above "veto sécurité : HONEYPOT", which reads
        # as a bug rather than as emphasis. The relay is skipped; a veto that
        # is genuinely the explosion engine's own — a pool too thin for a
        # fifteen-minute move — is still listed.
        explosion = candidate.explosion
        if explosion is not None and explosion.vetoed and not safety_vetoed:
            out.append(explosion.veto_reason or "bloqué par une porte de risque")

        if cross_check is not None and cross_check.agreement.value == _DISAGREE:
            fields = ", ".join(c.label_fr for c in cross_check.disagreements[:3])
            out.append(
                f"les deux sources ne sont pas d'accord ({fields}) — un prix sur "
                "lequel elles divergent n'est pas un prix sur lequel agir"
            )

        return out

    def _entry_checks(self, candidate) -> list[_Check]:
        cfg = self.cfg
        early = candidate.early.score if candidate.early else None
        explosion = candidate.explosion.score if candidate.explosion else None
        confidence = candidate.confidence.score if candidate.confidence else None
        safety = candidate.safety.score if candidate.safety else None

        checks = [
            _floor("score de précocité", early, cfg.buy_min_early),
            _floor("explosion 15 min", explosion, cfg.buy_min_explosion),
            _floor("confiance des données", confidence, cfg.buy_min_confidence),
            _floor("sécurité", safety, cfg.buy_min_safety),
        ]

        # Maturity is 50 with `known=False` when there was nothing to measure
        # (invariant 80). Reading that as "well under the ceiling" would let a
        # token with no measurable history buy on an absence of data.
        maturity = candidate.maturity
        if maturity is None or not maturity.known:
            checks.append(
                _Check(
                    name="maturité du mouvement",
                    passed=False,
                    value=None,
                    threshold=cfg.buy_max_maturity,
                    unavailable=True,
                    why=(
                        "maturité du mouvement inconnue — un token sans historique "
                        "mesurable ressemble exactement à un token qui n'a pas démarré"
                    ),
                )
            )
        else:
            checks.append(
                _ceiling("maturité du mouvement", maturity.score, cfg.buy_max_maturity)
            )

        checks.append(
            _liquidity_floor(candidate.liquidity_usd, cfg.buy_min_liquidity_usd)
        )

        rug = candidate.safety.rug_risk if candidate.safety else RugRisk.UNKNOWN
        checks.append(
            _Check(
                name="rug risk",
                passed=_RUG_ORDER[rug] >= _RUG_ORDER[self.max_rug_risk],
                value=None,
                threshold=None,
                unavailable=rug is RugRisk.UNKNOWN,
                why=(
                    f"rug risk {rug.value}, maximum toléré pour un achat "
                    f"{self.max_rug_risk.value}"
                ),
            )
        )
        return checks

    def _strength(self, checks: list[_Check], cross_check) -> SignalStrength:
        """How far past every floor, never how high the best score is.

        Measured as the *worst* margin across all the numeric floors, so a
        token cannot reach TRÈS FORTE by having one spectacular number and
        scraping past the rest.
        """
        margins = [c.margin for c in checks if c.margin is not None]
        if not margins:
            return SignalStrength.STANDARD
        worst = min(margins)
        recouped = cross_check is not None and cross_check.agreement.value == _AGREE

        if worst >= 10.0 and recouped:
            return SignalStrength.VERY_STRONG
        # Without two sources agreeing, the convergence rests on one criterion
        # fewer, so the top grade is unavailable however good the rest looks.
        if worst >= 10.0 or (worst >= 5.0 and recouped):
            return SignalStrength.STRONG
        return SignalStrength.STANDARD


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Check:
    name: str
    passed: bool
    value: float | None
    threshold: float | None
    why: str
    unavailable: bool = False

    @property
    def margin(self) -> float | None:
        """Points past the floor. `None` when this is not a numeric floor, so a
        pass/fail check cannot inflate or deflate the strength."""
        if self.value is None or self.threshold is None or not self.passed:
            return None
        return abs(self.value - self.threshold)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "value": None if self.value is None else round(self.value, 1),
            "threshold": self.threshold,
            "unavailable": self.unavailable,
            "why": self.why,
        }


def _floor(name: str, value: float | None, threshold: float) -> _Check:
    if value is None:
        return _Check(
            name=name, passed=False, value=None, threshold=threshold, unavailable=True,
            why=f"{name} indisponible — la décision ne peut pas être prise sans",
        )
    return _Check(
        name=name, passed=value >= threshold, value=value, threshold=threshold,
        why=f"{name} {num(value, 0)}, minimum requis {num(threshold, 0)}",
    )


def _ceiling(name: str, value: float | None, threshold: float) -> _Check:
    if value is None:
        return _Check(
            name=name, passed=False, value=None, threshold=threshold, unavailable=True,
            why=f"{name} indisponible — la décision ne peut pas être prise sans",
        )
    return _Check(
        name=name, passed=value <= threshold, value=value, threshold=threshold,
        why=f"{name} {num(value, 0)}, maximum toléré {num(threshold, 0)}",
    )


def _liquidity_floor(value: float | None, threshold: float) -> _Check:
    """Dollars of depth, not a rank.

    Kept out of `_floor` because the margin is in dollars: feeding it to the
    strength calculation alongside score points would compare 24 000 with 12
    and make every deep pool look VERY_STRONG. The margin is deliberately None.
    """
    if value is None:
        return _Check(
            name="liquidité", passed=False, value=None, threshold=None, unavailable=True,
            why="liquidité inconnue — impossible de juger si la position pourrait être fermée",
        )
    return _Check(
        name="liquidité", passed=value >= threshold, value=None, threshold=None,
        why=f"liquidité {money(value)}, minimum requis {money(threshold)}",
    )


# --------------------------------------------------------------------------- #
# Wording
# --------------------------------------------------------------------------- #


def _buy_reasons(candidate) -> list[str]:
    """Why this is a buy, best-supported first, capped at what fits a card."""
    reasons: list[str] = []
    if candidate.early is not None:
        reasons.extend(candidate.early.why[:4])
    if candidate.explosion is not None:
        reasons.extend(r for r in candidate.explosion.reasons[:2] if r not in reasons)
    return reasons[:5]


def _token_risks(candidate) -> list[str]:
    """Everything arguing against, from all three readings of the snapshot.

    They are allowed to disagree (spec §52) and are not merged: the caveats of
    a component resting on a window that has not elapsed belong beside its
    reasons, not folded into them.
    """
    risks: list[str] = []
    if candidate.early is not None:
        risks.extend(candidate.early.risks)
    if candidate.explosion is not None:
        risks.extend(candidate.explosion.caveats)
    if candidate.safety is not None:
        risks.extend(
            f.label_fr for f in candidate.safety.findings if f.severity.value != "UNKNOWN"
        )
    if candidate.confidence is not None and candidate.confidence.missing:
        risks.append(
            "données manquantes : " + ", ".join(candidate.confidence.missing[:3])
        )
    # Deduplicate while keeping the order the engines produced them in.
    seen: set[str] = set()
    return [r for r in risks if not (r in seen or seen.add(r))]
