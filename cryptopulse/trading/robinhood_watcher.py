"""The loop that only looks at the Robinhood tokens you actually hold.

WHY IT EXISTS SEPARATELY FROM THE DISCOVERY SEARCH

The discovery search reads the whole chain's new pools once a cycle, which is
the right cadence for *finding* things. It is the wrong cadence for something
you own — and, worse, it is the wrong *set*: a token bought two days ago has
long since fallen out of the age window discovery looks at, so the search that
found it will never look at it again.

    DISCOVERY   new pools, whole chain    every cycle   finding things
    WATCHER     held tokens only          faster        what you own

WHAT IT COSTS, AND WHY THAT IS STATED (spec §43, invariant 28)

Per cycle: one GeckoTerminal request *per held token*, plus one GoPlus request
per batch of thirty. Ten positions therefore cost eleven requests, not two —
GeckoTerminal has no token-to-pools batch endpoint (`/pools/multi/` takes pool
addresses, not token addresses), and claiming a batch this connector cannot
deliver would be exactly the kind of invented figure the rest of this project
refuses. GoPlus genuinely is batched, which is why safety is the cheap half.

`requests_used` is counted as the calls are made, not estimated, because a loop
that quietly spent hundreds would be discovered as a rate-limit ban rather than
as a number on a screen. `CP_ROBINHOOD_MAX_TRACKED_POSITIONS` bounds it.

WHY SAFETY IS RE-READ EVERY CYCLE AND NOT TRUSTED FROM ENTRY

The powers that make a token unsellable — mint, blacklist, a raised sell tax,
recovered ownership — are exercised *after* people have bought. That is the
mechanism. A safety verdict cached from the moment of purchase would be exactly
the verdict that cannot see the rug happening.

WHAT IT REFUSES TO DO

It never opens or closes a position, and it cannot do so even by accident: the
only writes it makes are `update_position` and `record_position_decision`.
Opening and closing are things the user does. A failure on one position never
stops the others, and a failure of the whole watcher never stops anything else.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from cryptopulse.alerts.robinhood_notify import (
    position_notice,
    worth_notifying_position,
)
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.logging import get_logger
from cryptopulse.database import repo
from cryptopulse.providers.robinhood import ROBINHOOD_CHAIN_ID
from cryptopulse.risk.robinhood_safety import assess_safety, unanalysed
from cryptopulse.scoring.robinhood_early import (
    assess_confidence,
    assess_maturity,
    score_early,
)
from cryptopulse.scoring.robinhood_explosion import score_explosion
from cryptopulse.trading.decision import TradeAction
from cryptopulse.trading.hysteresis import DecisionGate
from cryptopulse.trading.robinhood_decision import RobinhoodTradeDecisionEngine
from cryptopulse.trading.robinhood_exit_risk import assess_robinhood_exit_risk
from cryptopulse.trading.robinhood_health import compute_robinhood_health

log = get_logger("trading.robinhood_watcher")

__all__ = ["RobinhoodWatchReport", "RobinhoodPositionWatcher"]

# The chain string stored on a position that lives on Robinhood Chain.
ROBINHOOD_CHAIN = "ROBINHOOD"


@dataclass(slots=True)
class RobinhoodWatchReport:
    at_ms: int
    checked: int = 0
    changed: int = 0
    requests_used: int = 0
    duration_ms: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "at_ms": self.at_ms,
            "checked": self.checked,
            "changed": self.changed,
            # Counted as the calls are made, never estimated: one per held
            # token for the pools, one per batch of thirty for safety.
            "requests_used": self.requests_used,
            "duration_ms": self.duration_ms,
            "errors": self.errors,
            "notes": self.notes,
            "chain_id": ROBINHOOD_CHAIN_ID,
        }


class RobinhoodPositionWatcher:
    """Re-reads the held tokens and asks the decision engine what it thinks."""

    def __init__(
        self,
        settings,
        gecko,
        goplus,
        *,
        clock: Clock = SYSTEM_CLOCK,
        gate: DecisionGate | None = None,
        on_exit=None,
    ) -> None:
        self.settings = settings
        self.gecko = gecko
        self.goplus = goplus
        self.clock = clock
        self.engine = RobinhoodTradeDecisionEngine(settings)
        self.gate = gate or DecisionGate()
        # Called with a formatted notice when a held token turns 🔴 or 🟠.
        # Injected rather than imported so the watcher keeps no dependency on
        # a delivery channel — and so a test can assert what it would have sent.
        self.on_exit = on_exit
        self.last_report: RobinhoodWatchReport | None = None
        self._lock = asyncio.Lock()

    async def run_once(self) -> RobinhoodWatchReport:
        """One pass over the open Robinhood positions. Never raises."""
        async with self._lock:
            return await self._run()

    async def _run(self) -> RobinhoodWatchReport:
        started = time.perf_counter()
        now_ms = self.clock.now_ms()
        report = RobinhoodWatchReport(at_ms=now_ms)

        try:
            rows = await asyncio.to_thread(repo.positions, "OPEN", 500)
        except Exception as exc:
            report.errors["_database"] = type(exc).__name__
            log.error("rh_watcher_read_failed", error=type(exc).__name__)
            self.last_report = report
            return report

        held = [r for r in rows if (r.get("chain") or "").upper() == ROBINHOOD_CHAIN]
        held = [r for r in held if r.get("contract_address")]
        if not held:
            report.notes.append("aucune position Robinhood ouverte — le watcher ne coûte rien")
            self.last_report = report
            return report

        cap = getattr(self.settings, "max_tracked_positions", 20)
        if len(held) > cap:
            report.notes.append(
                f"{len(held)} positions Robinhood ouvertes, {cap} suivies — "
                "les plus récentes d'abord"
            )
            held = held[:cap]

        addresses = [r["contract_address"] for r in held]
        try:
            candidates, spent = await self._refresh(addresses, now_ms=now_ms)
            report.requests_used += spent
        except Exception as exc:
            report.errors["_refresh"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            log.warning("rh_watcher_refresh_failed", error=type(exc).__name__)
            self.last_report = report
            return report

        for row in held:
            address = row["contract_address"]
            candidate = candidates.get(address.lower())
            if candidate is None:
                # The indexer no longer lists the pool. That is a fact worth
                # surfacing, not a reason to guess a price — and emphatically
                # not a reason to invent a SELL.
                report.errors[row["symbol"]] = "pool absent de l'indexeur ce cycle"
                continue
            try:
                if await self._watch_one(row, candidate, now_ms):
                    report.changed += 1
                report.checked += 1
            except Exception as exc:
                report.errors[row["symbol"]] = f"{type(exc).__name__}: {str(exc)[:120]}"
                log.warning(
                    "rh_position_watch_failed", symbol=row["symbol"], error=type(exc).__name__
                )

        report.duration_ms = int((time.perf_counter() - started) * 1000)
        self.last_report = report
        log.info(
            "rh_position_watch",
            checked=report.checked, changed=report.changed,
            requests=report.requests_used, failed=len(report.errors),
        )
        return report

    # ------------------------------------------------------------- refresh #

    async def _refresh(self, addresses: list[str], *, now_ms: int) -> tuple[dict, int]:
        """Re-read the held tokens from both sources, batched.

        Returns the candidates keyed by lowercase address, and the number of
        requests actually spent — counted, not assumed, so the figure on screen
        is the one the rate limiter saw.
        """
        from cryptopulse.hunter.robinhood_discovery import TokenCandidate, _finalise

        spent = 0
        candidates: dict[str, TokenCandidate] = {}
        wanted = {a.lower() for a in addresses}

        for address in addresses:
            try:
                pools = await self.gecko.token_pools(address)
                spent += 1
            except Exception as exc:
                # One token's pools failing must not cost the others theirs.
                spent += 1
                log.warning(
                    "rh_watcher_pools_failed", address=address[:12], error=type(exc).__name__
                )
                continue
            for pool in pools:
                for side in (pool.base, pool.quote):
                    key = side.address.lower()
                    if key not in wanted:
                        continue
                    candidate = candidates.get(key)
                    if candidate is None:
                        candidate = TokenCandidate(
                            address=side.address, symbol=side.symbol, name=side.name
                        )
                        candidates[key] = candidate
                    candidate.pools.append(pool)

        for candidate in candidates.values():
            _finalise(candidate, now_ms=now_ms)

        # Safety, re-read rather than trusted from entry: the owner powers that
        # make a token unsellable are exercised after the purchase.
        if candidates:
            found: dict = {}
            held_addresses = [c.address for c in candidates.values()]
            batch = max(1, self.settings.goplus_batch_size)
            for start in range(0, len(held_addresses), batch):
                chunk = held_addresses[start : start + batch]
                try:
                    found.update(
                        await self.goplus.token_security(chunk, chain_id=ROBINHOOD_CHAIN_ID)
                    )
                except Exception as exc:
                    log.warning("rh_watcher_safety_failed", error=type(exc).__name__)
                finally:
                    spent += 1
            for candidate in candidates.values():
                sec = found.get(candidate.address.lower())
                candidate.safety = (
                    assess_safety(sec, self.settings)
                    if sec is not None
                    else unanalysed(
                        candidate.address,
                        self.settings,
                        reason="aucune donnée de sécurité ce cycle",
                    )
                )

        for candidate in candidates.values():
            candidate.maturity = assess_maturity(candidate)
            candidate.early = score_early(candidate, self.settings, maturity=candidate.maturity)
            candidate.confidence = assess_confidence(candidate)
            candidate.explosion = score_explosion(candidate, self.settings)

        return candidates, spent

    # --------------------------------------------------------------- one #

    async def _watch_one(self, row: dict, candidate, now_ms: int) -> bool:
        view = _EntryView(row)
        price = candidate.price_usd

        health = compute_robinhood_health(view, candidate, current_price=price)
        exit_risk = assess_robinhood_exit_risk(view, candidate, current_price=price)
        decision = self.engine.decide_position(
            view, candidate, health, exit_risk, now_ms=now_ms, current_price=price
        )

        # The noise gate, with the same exemption as the Binance watcher: a move
        # toward the exit is never delayed by a timer (invariant 53).
        escalating = (
            exit_risk.invalidation_broken
            or bool(exit_risk.critical)
            or decision.action in (TradeAction.SELL, TradeAction.REDUCE)
        )
        outcome = self.gate.evaluate(
            row["symbol"],
            decision.action,
            now_ms=now_ms,
            override_reason=(
                exit_risk.messages(1)[0] if escalating and exit_risk.signals else None
            ),
        )

        if price is not None:
            await asyncio.to_thread(
                repo.update_position,
                row["id"],
                price=price,
                now_ms=now_ms,
                health=health.score,
            )

        # An exit notice bypasses every rate limit by design (invariant 53 and
        # 44 together): the watcher's own gate has already confirmed the change,
        # and holding back a rug in progress to be polite is how the loud
        # notification becomes the one that was missed.
        if outcome.changed and self.on_exit is not None and worth_notifying_position(
            outcome.action.value
        ):
            try:
                await self.on_exit(
                    position_notice(row, decision, health, exit_risk)
                )
            except Exception as exc:
                # A notification failure costs a notification, never a cycle.
                log.warning("rh_exit_notify_failed", error=type(exc).__name__)

        if outcome.changed:
            await asyncio.to_thread(
                repo.record_position_decision,
                row["id"],
                {
                    "decision": outcome.action.value,
                    "price": price if price is not None else 0.0,
                    "health_score": health.score,
                    # The early score is journalled under `opportunity_score`
                    # because that is the column's role — "what the entry engine
                    # said" — not because the two engines are the same. Which
                    # engine produced it is recoverable from the position's
                    # chain, and blending is impossible: a Binance row and a
                    # Robinhood row never share a position.
                    "opportunity_score": candidate.early.score if candidate.early else None,
                    "explosion_score": (
                        candidate.explosion.score if candidate.explosion else None
                    ),
                    "reasons": decision.reasons,
                    "risks": decision.risks,
                },
                now_ms=now_ms,
            )
        return outcome.changed

    def forget(self, symbol: str) -> None:
        self.gate.forget(symbol)

    def status(self) -> dict:
        return {
            "chain_id": ROBINHOOD_CHAIN_ID,
            "engine_version": self.engine.weights_fingerprint,
            "last_run": self.last_report.to_dict() if self.last_report else None,
            "note": (
                "Ne suit que les positions Robinhood ouvertes. Une requête par token "
                "détenu pour les pools, plus une requête par lot de trente pour la "
                "sécurité : l'indexeur n'offre pas de lot token → pools."
            ),
        }


class _EntryView:
    """The entry state a position row records, as attributes.

    The health and exit-risk engines read attributes rather than dict keys so
    they can be exercised against plain objects in tests; this adapts one to the
    other without either side knowing about the database.
    """

    __slots__ = (
        "symbol", "invalidation_price", "trigger_price", "entry_price",
        "entry_early", "entry_liquidity_usd", "entry_rug_risk", "entry_safety",
    )

    def __init__(self, row: dict) -> None:
        self.symbol = row["symbol"]
        self.invalidation_price = row.get("invalidation_price")
        self.trigger_price = row.get("trigger_price")
        self.entry_price = row.get("actual_entry_price") or row.get("entry_price")
        self.entry_early = row.get("entry_early")
        self.entry_liquidity_usd = row.get("entry_liquidity_usd")
        self.entry_rug_risk = row.get("entry_rug_risk")
        self.entry_safety = row.get("entry_safety")
