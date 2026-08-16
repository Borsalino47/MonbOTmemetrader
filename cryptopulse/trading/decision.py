"""TRADE_DECISION_V1 — six decisions, in words rather than numbers.

WHY THIS IS AN ENGINE AND NOT A COMPRESSION

`scoring/verdict.py` is deliberately not an opinion: it reads what the score
already computed and says it in four words, introducing no new threshold. This
is the opposite. Saying ACHETER means asserting that a specific combination of
readings is worth acting on, and that assertion is not contained in any of the
three scores — it is new. So this carries its own version and its own weights
fingerprint, and every decision is journalled with them. Change a threshold and
the fingerprint changes, so a decision taken last week is never reread as though
it had been taken under today's rules.

CONVERGENCE, NOT A MAXIMUM

A single high score never produces ACHETER. Every floor must be cleared at once:
opportunity, explosion, confidence, safety, maturity, liquidity, and a setup
that has actually triggered. That is what makes the decision a claim about the
*combination* rather than about whichever number happened to spike. A test
raises one score to 100 with the rest at zero and asserts the answer is not BUY.

WHAT AN UNAVAILABLE INPUT DOES

It blocks, and says so — with one deliberate exception. The discovery score only
exists after a deep scan, so requiring it would mean the engine could never say
ACHETER during an ordinary cycle. Instead: if a discovery score is present it
must clear its floor, and if it is absent the decision can still be BUY but can
never reach VERY_STRONG, and the caveat is attached. Everything else that is
missing blocks, because a decision taken on an unknown safety score is not a
decision.

A HARD VETO IS NOT A DEDUCTION

Liquidity DANGEROUS, a safety veto, a broken invalidation, a rug-risk flag: each
produces ⚫ NE PAS ACHETER outright, whatever the scores say. A reduced score
would still let a dangerous token outrank a safe one in a sorted list, which is
exactly how a veto stops meaning anything.

FIVE COLOURS, AND WHY GREEN IS RESERVED

Green means *open a new position* and nothing else. A held position that is
doing well is blue. Using green for both is the single most expensive confusion
this screen could create: a glance would not distinguish "there is something to
do" from "there is nothing to do". The colour never travels alone — every
rendering carries icon, text and colour together.

THIS ENGINE PLACES NO ORDERS

It produces a recommendation and a notification. The user acts manually and then
tells the application what they did. There is no order-placing code in this
repository and none may be added.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

from cryptopulse.config.settings import TradeSettings
from cryptopulse.core.logging import get_logger
from cryptopulse.risk.liquidity import LiquidityStatus
from cryptopulse.scoring.states import SetupState

log = get_logger("trading.decision")

__all__ = [
    "TRADE_ENGINE_VERSION",
    "TradeAction",
    "SignalStrength",
    "TradeDecision",
    "TradeDecisionEngine",
    "PRESENTATION",
]

TRADE_ENGINE_VERSION = "TRADE_DECISION_V1"

# Setups where the entry condition has actually occurred. A coiled ARMED setup
# is a WATCH: the thing that would make it a buy has not happened yet.
TRIGGERED_STATES = frozenset({SetupState.BREAKOUT, SetupState.RETEST, SetupState.CONTINUATION})


class TradeAction(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    WATCH = "WATCH"
    REDUCE = "REDUCE"
    SELL = "SELL"
    AVOID = "AVOID"


class SignalStrength(str, Enum):
    """How much cleared the floors, for §28 statistics.

    The screen keeps saying "🟢 ACHETER" whatever this is; the strength appears
    underneath. Splitting the display would create three buy buttons where the
    product deliberately has one.
    """

    STANDARD = "STANDARD"
    STRONG = "STRONG"
    VERY_STRONG = "VERY_STRONG"


# action: (emoji, French label, CSS token, English label)
# The single source for the colour code. Any screen that renders a decision
# reads this, so the six colours cannot diverge between two views.
PRESENTATION: dict[TradeAction, tuple[str, str, str, str]] = {
    TradeAction.BUY:    ("🟢", "ACHETER", "buy", "Buy"),
    TradeAction.HOLD:   ("🔵", "CONSERVER", "hold", "Hold"),
    TradeAction.WATCH:  ("🟡", "SURVEILLER", "watch", "Watch"),
    TradeAction.REDUCE: ("🟠", "RÉDUIRE / PROTÉGER", "reduce", "Reduce / protect"),
    TradeAction.SELL:   ("🔴", "VENDRE", "sell", "Sell"),
    TradeAction.AVOID:  ("⚫", "NE PAS ACHETER", "avoid", "Do not buy"),
}

_STRENGTH_FR = {
    SignalStrength.STANDARD: "CORRECTE",
    SignalStrength.STRONG: "FORTE",
    SignalStrength.VERY_STRONG: "TRÈS FORTE",
}

_DISCLAIMER = (
    "Une recommandation produite par des règles fixes, jamais validées sur des "
    "résultats réels. Ce n'est ni un conseil, ni une probabilité. Aucun ordre "
    "n'est passé automatiquement : vous décidez et vous exécutez vous-même."
)


@dataclass(slots=True)
class TradeDecision:
    symbol: str
    action: TradeAction
    strength: SignalStrength | None          # BUY only; None everywhere else
    timestamp_ms: int
    price: float

    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    # The two prices that matter for acting on this, or None. Never invented:
    # a level that could not be measured is absent, not guessed.
    trigger_price: float | None = None
    invalidation_price: float | None = None

    # Which floors were cleared and which were not, so the answer is arguable
    # rather than merely stated.
    checks: list[dict] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)

    engine_version: str = TRADE_ENGINE_VERSION
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
        """Does this ask the user to do something right now?"""
        return self.action in (TradeAction.BUY, TradeAction.SELL, TradeAction.REDUCE)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "action": self.action.value,
            "emoji": self.emoji,
            "label_fr": self.label_fr,
            "label_en": PRESENTATION[self.action][3],
            "tone": self.tone,
            "strength": self.strength.value if self.strength else None,
            "strength_fr": _STRENGTH_FR[self.strength] if self.strength else None,
            "timestamp_ms": self.timestamp_ms,
            "price": self.price,
            "reasons": self.reasons,
            "risks": self.risks,
            "trigger_price": self.trigger_price,
            "invalidation_price": self.invalidation_price,
            "checks": self.checks,
            "blocking": self.blocking,
            "is_actionable": self.is_actionable,
            "engine_version": self.engine_version,
            "weights_fingerprint": self.weights_fingerprint,
            "disclaimer": _DISCLAIMER,
        }


class TradeDecisionEngine:
    """Stateless. Anti-noise lives in `trading/hysteresis.py`, not here.

    Keeping them apart matters: this answers "what does the data say right
    now?", and that answer must stay computable from one moment in isolation so
    it can be replayed over history. Deciding whether the *user* should be told
    is a separate question with a memory.
    """

    def __init__(self, settings: TradeSettings | None = None) -> None:
        self.cfg = settings or TradeSettings()
        try:
            self.min_liquidity = LiquidityStatus(self.cfg.buy_min_liquidity.upper())
        except ValueError as exc:
            raise ValueError(
                f"CP_TRADE_BUY_MIN_LIQUIDITY={self.cfg.buy_min_liquidity!r} is not a liquidity "
                f"status. Expected one of: {', '.join(s.value for s in LiquidityStatus)}"
            ) from exc
        self.weights_fingerprint = self._fingerprint()

    def _fingerprint(self) -> str:
        """Every threshold that can change the answer goes in.

        A decision journalled under one set of floors must never be read as
        though it had been taken under another — that is the same rule the three
        scoring engines follow, for the same reason.
        """
        payload = json.dumps(
            {
                "version": TRADE_ENGINE_VERSION,
                "buy_min_opportunity": self.cfg.buy_min_opportunity,
                "buy_min_explosion": self.cfg.buy_min_explosion,
                "buy_min_discovery": self.cfg.buy_min_discovery,
                "buy_min_confidence": self.cfg.buy_min_confidence,
                "buy_min_safety": self.cfg.buy_min_safety,
                "buy_max_pump_maturity": self.cfg.buy_max_pump_maturity,
                "buy_min_liquidity": self.min_liquidity.value,
                "buy_requires_triggered_setup": self.cfg.buy_requires_triggered_setup,
                "watch_min_opportunity": self.cfg.watch_min_opportunity,
                "watch_min_confidence": self.cfg.watch_min_confidence,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    # ---------------------------------------------------------------- entry #

    def decide_entry(self, result, *, discovery_score: float | None = None) -> TradeDecision:
        """Should a position be opened in something not currently held?

        Three possible answers and no others: 🟢 ACHETER, 🟡 SURVEILLER,
        ⚫ NE PAS ACHETER. HOLD, REDUCE and SELL are about a position that
        exists, and offering them here would be meaningless.
        """
        decision = TradeDecision(
            symbol=result.symbol,
            action=TradeAction.AVOID,
            strength=None,
            timestamp_ms=result.timestamp_ms,
            price=result.price,
            weights_fingerprint=self.weights_fingerprint,
        )
        decision.trigger_price, decision.invalidation_price = _levels(result)

        # 1. Hard vetoes. Outright, never as a deduction.
        vetoes = _hard_vetoes(result)
        if vetoes:
            decision.action = TradeAction.AVOID
            decision.blocking = vetoes
            decision.risks = vetoes + result.risks()[:4]
            decision.reasons = []
            return decision

        # 2. The floors, each recorded whether or not it passed.
        checks = self._entry_checks(result, discovery_score)
        decision.checks = [c.to_dict() for c in checks]
        failed = [c for c in checks if not c.passed]

        if not failed:
            decision.action = TradeAction.BUY
            decision.strength = self._strength(result, checks, discovery_score)
            decision.reasons = _buy_reasons(result)
            decision.risks = result.risks()[:5]
            if discovery_score is None:
                decision.risks.append(
                    "aucun score de recherche pour ce token (deep scan non lancé) — "
                    "la convergence est établie sur un critère de moins"
                )
            return decision

        decision.blocking = [c.why for c in failed]

        # 3. Not a buy. Is it worth keeping an eye on, or is it nothing?
        unavailable = [c for c in failed if c.unavailable]
        confidence = result.confidence.score
        if unavailable:
            # A missing input is not a low reading. It cannot be scored as one.
            decision.action = TradeAction.AVOID
            decision.risks = [c.why for c in unavailable] + result.risks()[:3]
            return decision

        if (
            result.final_score >= self.cfg.watch_min_opportunity
            and confidence >= self.cfg.watch_min_confidence
        ):
            decision.action = TradeAction.WATCH
            decision.reasons = result.why()[:4]
            decision.risks = [c.why for c in failed][:4]
            return decision

        decision.action = TradeAction.AVOID
        decision.risks = [c.why for c in failed][:4] or result.risks()[:4]
        return decision

    # --------------------------------------------------------------- checks #

    def _entry_checks(self, result, discovery_score: float | None) -> list[_Check]:
        cfg = self.cfg
        explosion = result.explosion.score if result.explosion else None
        checks = [
            _floor("opportunité", result.final_score, cfg.buy_min_opportunity),
            _floor("explosion 15m", explosion, cfg.buy_min_explosion),
            _floor("confiance des données", result.confidence.score, cfg.buy_min_confidence),
            _floor("sécurité", result.safety.score, cfg.buy_min_safety),
            _ceiling("maturité du mouvement", result.maturity.score, cfg.buy_max_pump_maturity),
        ]

        # Discovery is the one optional floor: it exists only after a deep scan,
        # and requiring it would mean an ordinary cycle could never say BUY. If
        # it is there it must pass; if it is not, the strength is capped instead.
        if discovery_score is not None:
            checks.append(_floor("recherche", discovery_score, cfg.buy_min_discovery))

        status = result.liquidity.status
        checks.append(
            _Check(
                name="liquidité",
                passed=status.rank >= self.min_liquidity.rank,
                value=None,
                threshold=None,
                unavailable=status is LiquidityStatus.UNKNOWN,
                why=(
                    "liquidité inconnue — impossible de juger l'entrée"
                    if status is LiquidityStatus.UNKNOWN
                    else f"liquidité {status.value}, minimum requis {self.min_liquidity.value}"
                ),
            )
        )

        if cfg.buy_requires_triggered_setup:
            state = result.state.state
            checks.append(
                _Check(
                    name="setup déclenché",
                    passed=state in TRIGGERED_STATES,
                    value=None,
                    threshold=None,
                    why=f"setup en {state.value} — la condition d'entrée ne s'est pas produite",
                )
            )
        return checks

    def _strength(self, result, checks: list[_Check], discovery_score: float | None) -> SignalStrength:
        """How far past the floors, not how high the top score is.

        Deliberately measured as margin across *all* the floors, so a signal
        cannot become VERY_STRONG by having one spectacular number.
        """
        margins = [c.margin for c in checks if c.margin is not None]
        if not margins:
            return SignalStrength.STANDARD
        worst = min(margins)

        if worst >= 10.0 and discovery_score is not None:
            return SignalStrength.VERY_STRONG
        # Without a discovery reading the convergence rests on one criterion
        # fewer, so the top grade is not available however good the rest looks.
        if worst >= 10.0 or (worst >= 5.0 and discovery_score is not None):
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
        """How far past its floor, in points. `None` when not a numeric floor."""
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
        why=f"{name} {value:.0f}, minimum requis {threshold:.0f}",
    )


def _ceiling(name: str, value: float | None, threshold: float) -> _Check:
    if value is None:
        return _Check(
            name=name, passed=False, value=None, threshold=threshold, unavailable=True,
            why=f"{name} indisponible — la décision ne peut pas être prise sans",
        )
    return _Check(
        name=name, passed=value <= threshold, value=value, threshold=threshold,
        why=f"{name} {value:.0f}, maximum toléré {threshold:.0f}",
    )


# --------------------------------------------------------------------------- #
# Vetoes and levels
# --------------------------------------------------------------------------- #


def _hard_vetoes(result) -> list[str]:
    """Conditions that produce ⚫ whatever the scores say.

    Each one describes a way of losing money that a high score does not
    compensate for. They are listed rather than summed: "three vetoes" is not
    worse than one, both are simply not a buy.
    """
    out: list[str] = []
    if result.liquidity.veto or result.liquidity.status is LiquidityStatus.DANGEROUS:
        out.append("liquidité dangereuse — une sortie ne serait pas exécutable au prix affiché")
    if result.safety.hard_veto:
        out.append("veto de sécurité — " + (result.safety.reasons[0] if result.safety.reasons else "actif signalé"))
    if result.state.state is SetupState.INVALIDATED:
        out.append("setup déjà invalidé — la raison d'acheter n'existe plus")
    if result.explosion is not None and result.explosion.vetoed:
        out.append(result.explosion.veto_reason or "bloqué par une porte de risque")
    return out


def _levels(result) -> tuple[float | None, float | None]:
    """The entry trigger and the invalidation, as prices.

    Both come from measured structure. When the structure could not produce a
    level the value is `None` and the screen shows a dash — a made-up entry
    price is the single most dangerous number this product could display.
    """
    if result.features is None:
        return None, None
    st = result.features.primary.structure
    if st is None:
        return None, None

    resistance = st.nearest_resistance.price if st.nearest_resistance else None
    support = st.nearest_support.price if st.nearest_support else None

    if st.broke_out and not st.fake_breakout:
        # Through the level: the trigger has happened, and that same level is
        # what a close back below would invalidate.
        return resistance, (resistance or support)
    return resistance, support


def _buy_reasons(result) -> list[str]:
    """Why this is a buy, best-supported first, capped at what fits a card."""
    reasons = result.why()[:4]
    if result.explosion is not None:
        reasons.extend(r for r in result.explosion.why()[:2] if r not in reasons)
    return reasons[:5]
