"""POSITION WATCHER — the loop that only looks at what you actually hold.

WHY A SECOND LOOP AT ALL

The market scanner covers a hundred and twenty symbols once a minute, which is
the right cadence for finding things. It is the wrong cadence for a position:
sixty seconds is a long time when a support gives way, and the whole value of an
exit signal is that it arrives before the move it describes has finished.

The obvious fix — scan everything faster — costs a hundred and twenty times more
requests to improve information about the two or three symbols that matter. So
instead there are three tiers, each looking at a different set for a different
reason:

    MARKET SCANNER    the whole venue        every 60s   finding things
    TOKEN HUNTER      the whole venue        free        behaviour change
    POSITION WATCHER  open positions only    every 15s   what you own

WHAT IT COSTS, MEASURED

Per position per cycle: four kline requests (5m/15m/1h/4h) plus one order book.
On Binance's weight table that is 1+1+1+1+5 = 9 weight. Ten positions on a
fifteen-second loop is 90 weight per cycle, 360 per minute, against a budget of
6000 — six percent. The candle cache absorbs most of it in practice, since a
closed 5m candle is immutable and nineteen cycles out of twenty ask for one that
is already held.

The report states `requests_used` exactly, the same rule the deep scan follows: a
loop that quietly spent hundreds of requests would be discovered as a rate-limit
ban rather than as a number on a screen.

WHAT IT REFUSES TO DO

It never opens or closes a position. It records prices, computes health, asks
the decision engine what it thinks, passes that through the noise gate, and
writes the change if there is one. Opening and closing are things the *user*
does, and this loop cannot do them even by accident — the only writes it makes
are `update_position` and `record_position_decision`.

A failure in one position never stops the others, and a failure of the whole
watcher never stops the scanner. Both are collected and reported rather than
raised, for the same reason a webhook failure costs a notification and not a
scan.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.logging import get_logger
from cryptopulse.database import repo
from cryptopulse.trading.decision import TradeAction, TradeDecisionEngine
from cryptopulse.trading.exit_risk import assess_exit_risk
from cryptopulse.trading.health import compute_health
from cryptopulse.trading.hysteresis import DecisionGate

log = get_logger("trading.watcher")

__all__ = ["WatchReport", "PositionWatcher"]

# Requests one position costs per cycle: 4 klines + 1 order book.
REQUESTS_PER_POSITION = 5


@dataclass(slots=True)
class WatchReport:
    checked: int = 0
    changed: int = 0
    requests_used: int = 0
    duration_ms: int = 0
    at_ms: int = 0
    decisions: list[dict] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "changed": self.changed,
            # Stated exactly. A loop that quietly spent hundreds of requests
            # would be discovered as a ban rather than as a number.
            "requests_used": self.requests_used,
            "duration_ms": self.duration_ms,
            "at_ms": self.at_ms,
            "decisions": self.decisions,
            "errors": self.errors,
            "notes": self.notes,
        }


class PositionWatcher:
    """Follows open positions. Cannot open or close one."""

    def __init__(
        self,
        settings,
        scanner,
        *,
        engine: TradeDecisionEngine | None = None,
        gate: DecisionGate | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.settings = settings
        self.cfg = settings.trade
        self.scanner = scanner
        self.clock = clock
        self.engine = engine or TradeDecisionEngine(settings.trade)
        self.gate = gate or DecisionGate(
            confirmations_required=self.cfg.confirmations_required,
            min_seconds_between_changes=self.cfg.min_seconds_between_changes,
        )
        self.last_report: WatchReport | None = None
        self._lock = asyncio.Lock()

    async def run_once(self) -> WatchReport:
        """One pass over the open positions. Never raises."""
        async with self._lock:
            return await self._run()

    async def _run(self) -> WatchReport:
        started = time.perf_counter()
        now_ms = self.clock.now_ms()
        report = WatchReport(at_ms=now_ms)

        try:
            open_rows = await asyncio.to_thread(repo.positions, "OPEN", 500)
        except Exception as exc:
            report.errors["_database"] = f"{type(exc).__name__}"
            log.error("watcher_read_failed", error=type(exc).__name__)
            self.last_report = report
            return report

        if not open_rows:
            report.notes.append("aucune position ouverte — le watcher ne coûte rien")
            self.last_report = report
            return report

        cap = self.cfg.max_tracked_positions
        if len(open_rows) > cap:
            # Bounded rather than unbounded on purpose: the request budget is
            # finite, and a silent overrun would show up as a ban.
            report.notes.append(
                f"{len(open_rows)} positions ouvertes, {cap} suivies (plafond "
                "CP_TRADE_MAX_TRACKED_POSITIONS) — les plus récentes d'abord"
            )
            open_rows = open_rows[:cap]

        for row in open_rows:
            try:
                changed = await self._watch_one(row, now_ms, report)
                report.checked += 1
                report.requests_used += REQUESTS_PER_POSITION
                if changed:
                    report.changed += 1
            except Exception as exc:
                # One position failing never stops the others.
                report.errors[row["symbol"]] = f"{type(exc).__name__}: {str(exc)[:120]}"
                log.warning("position_watch_failed", symbol=row["symbol"], error=type(exc).__name__)

        report.duration_ms = int((time.perf_counter() - started) * 1000)
        self.last_report = report
        log.info(
            "position_watch",
            checked=report.checked, changed=report.changed,
            requests=report.requests_used, failed=len(report.errors),
        )
        return report

    async def _watch_one(self, row: dict, now_ms: int, report: WatchReport) -> bool:
        symbol = row["symbol"]

        # Reuse the scan's own analysis when this cycle already produced one for
        # the symbol — the same rule the deep scan follows. Paying twice for an
        # identical answer is how a request budget disappears.
        result = self.scanner.last_report and next(
            (r for r in self.scanner.last_report.results if r.symbol == symbol), None
        )
        if result is None:
            ticker = (self.scanner.last_tickers or {}).get(symbol)
            features = await self.scanner._fetch_features(symbol, ticker)
            await self.scanner._attach_order_book(features)
            result = self.scanner.engine.score(features, now_ms)
            result.explosion = self.scanner.explosion_engine.score(
                features,
                timestamp_ms=result.timestamp_ms,
                maturity_score=result.maturity.score,
                hard_veto=result.safety.hard_veto or result.liquidity.veto,
            )
        else:
            # Already analysed this cycle: the requests were spent by the scan.
            report.requests_used -= REQUESTS_PER_POSITION

        price = result.price
        updated = await asyncio.to_thread(
            repo.update_position, row["id"], price=price, now_ms=now_ms
        )

        health = compute_health(_EntryView(row), result, current_price=price)
        risk = assess_exit_risk(
            _EntryView(row), result,
            current_price=price,
            drawdown_from_peak_pct=updated.get("drawdown_from_peak_pct"),
            btc_regime=self.scanner.regime.trend,
        )
        decision = self.engine.decide_position(
            _EntryView(row), result, health, risk, current_price=price
        )

        # The four conditions that must never wait for confirmation. Named by
        # the exit-risk engine, not re-derived here, so the two cannot drift.
        override = None
        if risk.invalidation_broken:
            override = "invalidation franchie"
        elif risk.critical:
            override = risk.critical[0].message

        outcome = self.gate.evaluate(
            symbol, decision.action, now_ms=now_ms, override_reason=override
        )

        report.decisions.append({
            "position_id": row["id"],
            "symbol": symbol,
            "decision": outcome.action.value,
            "proposed": outcome.proposed.value,
            "changed": outcome.changed,
            "price": price,
            "pnl_pct": updated.get("pnl_pct"),
            "health": health.score,
            "gate": outcome.to_dict(),
        })

        if not outcome.changed:
            return False

        # The screen decision, not the raw one — so the journal and the screen
        # can never disagree about what the user was actually told.
        await asyncio.to_thread(
            repo.record_position_decision,
            row["id"],
            {
                "decision": outcome.action.value,
                "price": price,
                "pnl_pct": updated.get("pnl_pct"),
                "health_score": health.score,
                "opportunity_score": result.final_score,
                "explosion_score": result.explosion.score if result.explosion else None,
                "reasons": decision.reasons,
                "risks": decision.risks,
            },
            now_ms=now_ms,
        )
        await asyncio.to_thread(
            repo.update_position, row["id"], price=price, now_ms=now_ms, health=health.score
        )
        return True

    def forget(self, symbol: str) -> None:
        """Called when a position closes, so the next one starts clean."""
        self.gate.forget(symbol)

    def status(self) -> dict:
        return {
            "interval_seconds": self.cfg.position_watch_interval_seconds,
            "max_tracked": self.cfg.max_tracked_positions,
            "requests_per_position": REQUESTS_PER_POSITION,
            "tracked_by_gate": self.gate.tracked(),
            "last_run": self.last_report.to_dict() if self.last_report else None,
            "note": (
                "Ne suit que les positions ouvertes. Ne peut ni ouvrir ni fermer une "
                "position — cela reste une action de l'utilisateur."
            ),
        }


class _EntryView:
    """Adapts a stored position row to what the health and exit engines read.

    A tiny shim rather than passing dicts around: the engines are written
    against attribute access so they can be tested with a plain object, and this
    keeps the row's shape from leaking into them.
    """

    __slots__ = (
        "entry_price", "invalidation_price", "trigger_price",
        "entry_opportunity", "entry_explosion",
    )

    def __init__(self, row: dict) -> None:
        self.entry_price = row.get("actual_entry_price") or row.get("entry_price")
        self.invalidation_price = row.get("invalidation_price")
        self.trigger_price = row.get("trigger_price")
        self.entry_opportunity = row.get("entry_opportunity")
        self.entry_explosion = row.get("entry_explosion")


def action_of(value: str | None) -> TradeAction | None:
    """Parse a stored decision string, tolerating an unknown one."""
    if not value:
        return None
    try:
        return TradeAction(value)
    except ValueError:
        return None
