"""Which Robinhood decisions are worth interrupting someone for.

TWO KINDS OF NOTIFICATION, AND ONLY TWO

    🟢 ACHETER on a token nobody holds — rare, and the whole point of the
                                         discovery search. Worth a buzz.
    🔴 VENDRE / 🟠 RÉDUIRE on a held token — urgent, and the one case where
                                             being late costs the position.

Nothing else. ⚫ NE PAS ACHETER is the ordinary answer for most of a chain and
notifying it would be a phone that vibrates all day; 🟡 SURVEILLER is an
invitation to look, not an instruction, and it belongs on the screen.

THE ANTI-SPAM RULE, AND THE ONE THING IT MUST NOT DELAY (invariant 44)

`trading/hysteresis.py` guards *positions*: a decision only reaches the watcher's
notification path once it has actually changed. Entries have no such memory —
the discovery search re-decides every candidate every cycle, so a token sitting
at 🟢 ACHETER for an hour would buzz every cycle without the gate below.

The gate is escalation-only in the same sense as `alerts/notify.py`: a token
notifies on its first ACHETER, and again only if it *stops* being a buy and
becomes one again. A token that quietly stays a buy is not news twice.

What the gate must never delay is a rug in progress. Exit notices bypass it
entirely — they arrive from the watcher, which has already applied its own
confirmation and already exempts a broken invalidation, a safety veto and a
liquidity collapse from any cooldown (invariant 53). Rate-limiting the sell is
how the loud notification ends up being the one that was missed.

WHY THE NOTICE IS BUILT HERE AND NOT IN `alerts/notify.py`

`notify.py` is about delivery — it must not know what a decision means, or it
would need to import `trading/`. So this module reads the decision and produces
the already-formatted `DecisionNotice` that module expects.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.alerts.notify import DecisionNotice
from cryptopulse.core.logging import get_logger
from cryptopulse.i18n import money, num
from cryptopulse.i18n import price as price_fr

log = get_logger("alerts.robinhood_notify")

__all__ = [
    "RobinhoodEntryGate",
    "entry_notice",
    "position_notice",
    "worth_notifying_entry",
    "worth_notifying_position",
]

# The only entry decision that interrupts someone.
_ENTRY_ACTIONS = frozenset({"BUY"})
# Exits that do. RÉDUIRE is included because protecting a gain has a deadline
# too — telling someone about it once the move is over is not telling them.
_EXIT_ACTIONS = frozenset({"SELL", "REDUCE"})


def worth_notifying_entry(action: str) -> bool:
    return action.upper() in _ENTRY_ACTIONS


def worth_notifying_position(action: str) -> bool:
    return action.upper() in _EXIT_ACTIONS


@dataclass(slots=True)
class RobinhoodEntryGate:
    """First ACHETER notifies; staying a buy does not notify again.

    Keyed on the contract address rather than the symbol: symbols are not
    unique on a permissionless chain, and two tokens calling themselves FRONG
    would otherwise silence each other — which is precisely the trick a
    copycat would use.
    """

    _armed: dict[str, str] = field(default_factory=dict)

    def allow(self, address: str, action: str) -> bool:
        key = address.lower()
        action = action.upper()
        previous = self._armed.get(key)
        self._armed[key] = action
        if not worth_notifying_entry(action):
            # Not a buy: nothing is sent, but the memory moves so that becoming
            # a buy again later is news.
            return False
        return previous != action

    def forget(self, address: str) -> None:
        self._armed.pop(address.lower(), None)

    def reset(self) -> None:
        self._armed.clear()

    def tracked(self) -> int:
        return len(self._armed)


def entry_notice(candidate) -> DecisionNotice:
    """🟢 ACHETER on a token nobody holds, formatted for a lock screen.

    Two seconds and one hand: the symbol, the price, why, and the one figure
    that decides whether it is even exitable. The scores go last because a
    number on a lock screen with no scale beside it is noise.
    """
    decision = candidate.decision
    lines: list[str] = list(decision.reasons[:3])

    if candidate.liquidity_usd is not None:
        lines.append(f"Liquidité {money(candidate.liquidity_usd)}")
    if candidate.safety is not None:
        lines.append(f"{candidate.safety.emoji} {candidate.safety.label_fr}")
    if candidate.early is not None:
        early = num(candidate.early.score, 0)
        explosion = (
            f" · explosion {num(candidate.explosion.score, 0)}/100"
            if candidate.explosion is not None
            else ""
        )
        lines.append(f"Précocité {early}/100{explosion}")
    # Never a probability, on the one surface where a caveat does not fit.
    lines.append("Classement sur 100, pas une probabilité. Vous exécutez vous-même.")

    return DecisionNotice(
        action="BUY",
        symbol=candidate.symbol or _short(candidate.address),
        price=price_fr(candidate.price_usd) + " $" if candidate.price_usd else "—",
        lines=lines[:6],
        strength=decision.strength.value if decision.strength else None,
    )


def position_notice(row: dict, decision, health, exit_risk) -> DecisionNotice:
    """🔴 VENDRE / 🟠 RÉDUIRE on something held.

    The named reasons come first and the PnL last: someone woken by this needs
    to know *what broke*, and seeing the gain first is how a decision gets made
    on the wrong fact.
    """
    lines: list[str] = list(decision.blocking[:2]) or list(decision.risks[:3])
    if health is not None and health.score is not None:
        lines.append(f"Santé {num(health.score, 0)}/100 — {health.label_fr}")
    elif health is not None:
        lines.append("Santé non calculable ce cycle")
    if exit_risk is not None and exit_risk.critical:
        lines.append(f"{len(exit_risk.critical)} signal(aux) critique(s)")

    pnl = row.get("pnl_pct")
    return DecisionNotice(
        action=decision.action.value,
        symbol=row.get("symbol") or _short(row.get("contract_address", "")),
        price=price_fr(decision.price_usd) + " $" if decision.price_usd else "—",
        lines=lines[:6],
        health=(
            None if health is None or health.score is None
            else f"{num(health.score, 0)}/100"
        ),
        pnl=None if pnl is None else f"{num(pnl, 1)} %",
    )


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else (address or "?")
