"""Anti-noise: deciding whether a *change* of decision is real.

THE PROBLEM THIS SOLVES

`trading/decision.py` answers "what does the data say right now?" from a single
moment, which is what makes it replayable over history. But a threshold read
once a minute crosses back and forth on ordinary noise, and a screen that goes
ACHETER → VENDRE → ACHETER → VENDRE over four candles is not merely annoying:
it destroys the user's ability to believe any of them, including the one that
was right.

So the decision and the *announcement* of the decision are separate. The engine
keeps producing an honest instantaneous answer; this decides when that answer
has earned the right to replace the one on screen.

THREE MECHANISMS, AND WHY EACH IS NEEDED SEPARATELY

**Confirmation.** A new decision must repeat for N consecutive evaluations
before it takes effect. This filters single-cycle flickers, which are the most
common noise by a wide margin. It costs latency exactly equal to N cycles, which
is why the position watcher runs faster than the scanner: on a 15-second loop,
two confirmations cost 30 seconds, not two minutes.

**Cooldown.** After a change lands, no further change for a period. Confirmation
alone does not stop a slow oscillation — a decision that flips every three
cycles would confirm every time. The cooldown is what makes that impossible.

**Escalation exemption.** A cooldown that blocked *everything* would be worse
than no cooldown, because the change it blocked might be the one that mattered.
So a move toward the exit — HOLD → REDUCE → SELL — needs confirmation but is
never blocked by the cooldown. Getting out late because a timer was running is
not a trade-off worth making.

FOUR THINGS BYPASS EVERYTHING

A broken invalidation, a hard safety veto, a liquidity collapse, a critical rug
risk. These are not evidence to be weighed; they are the terms of the trade
ending or the asset becoming untradeable. Waiting two cycles to mention a broken
invalidation would be indefensible, and the code says so where it happens.

This holds no opinion about markets and produces no numbers. It is a state
machine over decisions, and it is deliberately the only stateful thing in the
package.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.core.logging import get_logger
from cryptopulse.trading.decision import TradeAction

log = get_logger("trading.hysteresis")

__all__ = ["GateOutcome", "DecisionGate", "EXIT_ORDER", "is_escalation"]

# How far toward the exit each decision sits. Only used to detect a move toward
# the door, which the cooldown must never delay.
EXIT_ORDER: dict[TradeAction, int] = {
    TradeAction.BUY: 0,
    TradeAction.HOLD: 1,
    TradeAction.WATCH: 2,
    TradeAction.REDUCE: 3,
    TradeAction.SELL: 4,
    TradeAction.AVOID: 4,
}

# Decisions cheap enough to show immediately. Telling someone to keep watching
# costs nothing; telling them to buy or sell costs money.
FREE_ACTIONS = frozenset({TradeAction.HOLD, TradeAction.WATCH})


def is_escalation(previous: TradeAction | None, proposed: TradeAction) -> bool:
    """Is this a move toward the exit? Those are never delayed by a cooldown."""
    if previous is None:
        return False
    return EXIT_ORDER[proposed] > EXIT_ORDER[previous]


@dataclass(slots=True)
class GateOutcome:
    """What the gate decided, and why — silence is always explainable."""

    action: TradeAction          # what should be on screen now
    changed: bool                # did it just change
    proposed: TradeAction        # what the engine said this cycle
    confirmations: int           # how many consecutive cycles have proposed it
    required: int
    held_reason: str | None = None
    override: str | None = None  # set when something bypassed the gate entirely

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "changed": self.changed,
            "proposed": self.proposed.value,
            "confirmations": self.confirmations,
            "required": self.required,
            "held_reason": self.held_reason,
            "override": self.override,
        }


@dataclass(slots=True)
class _State:
    current: TradeAction | None = None
    changed_at_ms: int = 0
    pending: TradeAction | None = None
    pending_count: int = 0


class DecisionGate:
    """One instance per process; keyed by symbol internally."""

    def __init__(self, *, confirmations_required: int = 2, min_seconds_between_changes: int = 300) -> None:
        self.required = max(1, confirmations_required)
        self.cooldown_ms = max(0, min_seconds_between_changes) * 1000
        self._states: dict[str, _State] = {}

    def evaluate(
        self,
        symbol: str,
        proposed: TradeAction,
        *,
        now_ms: int,
        override_reason: str | None = None,
    ) -> GateOutcome:
        """Should `proposed` replace what is on screen for `symbol`?

        `override_reason` is passed by the caller when something happened that
        no amount of confirmation should delay — a broken invalidation, a safety
        veto, a liquidity collapse. The gate does not decide what qualifies;
        naming those conditions belongs to the exit-risk engine, and duplicating
        the list here would let the two drift apart.
        """
        state = self._states.setdefault(symbol.upper(), _State())

        # 1. The four things that bypass everything. Waiting two cycles to
        #    mention a broken invalidation would be indefensible.
        if override_reason:
            changed = state.current is not proposed
            state.current = proposed
            state.pending = None
            state.pending_count = 0
            if changed:
                state.changed_at_ms = now_ms
            return GateOutcome(
                action=proposed, changed=changed, proposed=proposed,
                confirmations=self.required, required=self.required,
                override=override_reason,
            )

        # 2. Nothing on screen yet: the first reading is the reading.
        if state.current is None:
            state.current = proposed
            state.changed_at_ms = now_ms
            return GateOutcome(
                action=proposed, changed=True, proposed=proposed,
                confirmations=1, required=self.required,
            )

        # 3. Unchanged. Clear any half-formed alternative so a flicker two
        #    cycles apart does not accumulate toward a change.
        if proposed is state.current:
            state.pending = None
            state.pending_count = 0
            return GateOutcome(
                action=state.current, changed=False, proposed=proposed,
                confirmations=0, required=self.required,
            )

        # 4. A different answer. Count how many times in a row it has been made.
        if state.pending is proposed:
            state.pending_count += 1
        else:
            state.pending = proposed
            state.pending_count = 1

        required = 1 if proposed in FREE_ACTIONS else self.required
        if state.pending_count < required:
            return GateOutcome(
                action=state.current, changed=False, proposed=proposed,
                confirmations=state.pending_count, required=required,
                held_reason=(
                    f"{proposed.value} proposé {state.pending_count}/{required} fois — "
                    "en attente de confirmation"
                ),
            )

        # 5. Confirmed. The cooldown may still hold it, unless it moves toward
        #    the exit: getting out late because a timer was running is not a
        #    trade-off worth making.
        escalating = is_escalation(state.current, proposed)
        age = now_ms - state.changed_at_ms
        if not escalating and age < self.cooldown_ms:
            remaining = (self.cooldown_ms - age) // 1000
            return GateOutcome(
                action=state.current, changed=False, proposed=proposed,
                confirmations=state.pending_count, required=required,
                held_reason=(
                    f"{proposed.value} confirmé mais la décision a changé il y a moins de "
                    f"{self.cooldown_ms // 1000}s — encore {remaining}s"
                ),
            )

        previous = state.current
        state.current = proposed
        state.changed_at_ms = now_ms
        state.pending = None
        state.pending_count = 0
        log.info(
            "decision_changed",
            symbol=symbol, previous=previous.value, now=proposed.value, escalating=escalating,
        )
        return GateOutcome(
            action=proposed, changed=True, proposed=proposed,
            confirmations=required, required=required,
        )

    # -- introspection, for /api/health and tests ---------------------------- #

    def current(self, symbol: str) -> TradeAction | None:
        state = self._states.get(symbol.upper())
        return state.current if state else None

    def forget(self, symbol: str) -> None:
        """Called when a position closes: the next open starts clean."""
        self._states.pop(symbol.upper(), None)

    def tracked(self) -> int:
        return len(self._states)


@dataclass(slots=True)
class GateStats:
    """Small summary for the health endpoint."""

    tracked: int = 0
    held: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"tracked": self.tracked, "held": self.held[:6]}
