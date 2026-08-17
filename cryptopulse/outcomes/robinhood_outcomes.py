"""What the price actually did after each Robinhood decision.

WHY THIS IS THE MOST IMPORTANT MODULE IN THE ROBINHOOD HALF

Every other Robinhood engine produces an opinion under weights nobody has
validated. This is the only thing that can ever show one of them to be wrong.
Until it has run on a few hundred real decisions, `ROBINHOOD_EARLY_SCORE_V1`
and `ROBINHOOD_TRADE_DECISION_V1` are hypotheses — and the 15m row is the
explosion engine's own scorecard, the window it makes a claim about
(invariant 38).

WHY CANDLES AND NOT "LOOK AGAIN LATER"

The obvious implementation is to note the price now and read it again in
fifteen minutes. It is also wrong: a window observed three minutes late is not
the window the engine made a claim about, and the error is silent and always in
the direction of whenever the loop happened to run.

GeckoTerminal exposes pool OHLCV with one- and five-minute aggregates, so the
window can be resolved *exactly*, retroactively, and long after the fact. That
is what makes a decision from last Tuesday gradeable today.

THE RULES ARE THE ONES THE BINANCE TRACKER ALREADY FOLLOWS

  * the decision's own bar must match by exact open time, never by nearest
    neighbour — resolving against a neighbour shifts every window by a bar;
  * entry is the **open of the bar after** the decision, never its close: the
    close is the price that produced the decision, and entering at it is
    look-ahead;
  * a window that has not fully elapsed is PENDING and is never written
    (invariant 14). Reporting the current price for an unfinished window turns
    "not yet" into a result;
  * bars that can no longer be fetched make the window UNRESOLVABLE with a
    reason, and it is excluded from every rate rather than counted as a loss
    (invariant 11);
  * success is defined in exactly one place — `RobinhoodHorizonResult.
    is_success` — so storage and statistics cannot drift apart (invariant 15).

COSTS ARE MODELLED, AND WORSE HERE THAN ON A VENUE

A DEX swap pays the pool fee, gas, and slippage against a pool that is often
thin. The default round trip is deliberately larger than the Binance one, and
it is a *modelled* figure rather than a measured one — which the payload says.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from cryptopulse.core.logging import get_logger
from cryptopulse.outcomes.horizons import HORIZONS, Horizon

log = get_logger("outcomes.robinhood")

__all__ = [
    "RH_HORIZONS",
    "RobinhoodCosts",
    "RobinhoodHorizonStatus",
    "RobinhoodHorizonResult",
    "RobinhoodHorizonTracker",
]

# The same four windows as the Binance side, deliberately. Two engines whose
# results are reported over different windows cannot be compared at all, and
# 15m is the window the explosion engine makes its claim about.
RH_HORIZONS: tuple[Horizon, ...] = HORIZONS

# The candle aggregate used to resolve. Five minutes divides every horizon here
# and is the finest aggregate the indexer offers alongside 1m; 1m would triple
# the payload for no extra resolution at these windows.
CANDLE_MINUTES = 5


class RobinhoodHorizonStatus(str, Enum):
    RESOLVED = "RESOLVED"
    PENDING = "PENDING"            # not enough time has passed yet
    UNRESOLVABLE = "UNRESOLVABLE"  # the bars needed can no longer be fetched


@dataclass(frozen=True, slots=True)
class RobinhoodCosts:
    """A modelled round trip, not a measured one — and the payload says so.

    A DEX swap pays the pool fee twice, gas twice, and slippage against a pool
    that may be a few tens of thousands deep. 3 % is a deliberately pessimistic
    placeholder: flattering the cost model would make every rate here look
    better than anything achievable, which is the opposite of what this table
    is for.
    """

    round_trip_pct: float = 3.0

    def describe(self) -> dict:
        return {
            "round_trip_pct": self.round_trip_pct,
            "note": (
                "Coût aller-retour modélisé : frais du pool, gas et glissement. "
                "C'est une hypothèse volontairement pessimiste, pas une mesure."
            ),
        }


@dataclass(slots=True)
class RobinhoodHorizonResult:
    signal_id: int
    contract_address: str
    symbol: str | None
    horizon: str
    status: RobinhoodHorizonStatus
    entry_price: float | None = None
    price_at_horizon: float | None = None
    change_pct: float | None = None
    net_change_pct: float | None = None
    max_gain_pct: float | None = None
    max_drawdown_pct: float | None = None
    note: str | None = None

    @property
    def is_success(self) -> bool | None:
        """Net change above zero after the modelled round trip.

        The single definition, read by both storage and statistics so the
        criterion cannot drift between them (invariant 15). A window with no
        verdict returns None, never False — "we could not tell" and "it lost"
        are different facts.
        """
        if self.status is not RobinhoodHorizonStatus.RESOLVED:
            return None
        if self.net_change_pct is None:
            return None
        return self.net_change_pct > 0.0

    def to_row(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "contract_address": self.contract_address,
            "horizon": self.horizon,
            "status": self.status.value,
            "entry_price": self.entry_price,
            "price_at_horizon": self.price_at_horizon,
            "change_pct": self.change_pct,
            "net_change_pct": self.net_change_pct,
            "max_gain_pct": self.max_gain_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "success": self.is_success,
            "note": self.note,
        }


@dataclass(slots=True)
class RobinhoodTrackReport:
    resolved: int = 0
    pending: int = 0
    unresolvable: int = 0
    requests_used: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "resolved": self.resolved,
            "pending": self.pending,
            "unresolvable": self.unresolvable,
            "requests_used": self.requests_used,
            "errors": self.errors,
            "horizons": [h.name for h in RH_HORIZONS],
            "note": (
                "Une fenêtre non écoulée est absente, jamais réglée au prix actuel. "
                "Une fenêtre dont les bougies sont introuvables est NON RÉSOLUE et "
                "sort de tous les taux, elle n'est pas comptée comme une perte."
            ),
        }


class RobinhoodHorizonTracker:
    """Grades decisions whose windows have elapsed. Never raises."""

    def __init__(self, gecko, *, costs: RobinhoodCosts | None = None, clock=None) -> None:
        self.gecko = gecko
        self.costs = costs or RobinhoodCosts()
        self.clock = clock

    def ready_before_ms(self, now_ms: int) -> int:
        """Decisions older than the shortest window may have something to grade."""
        return now_ms - RH_HORIZONS[0].minutes * 60_000

    async def track(self, rows: list[dict], *, now_ms: int) -> tuple[list[RobinhoodHorizonResult], RobinhoodTrackReport]:
        """One pass. `rows` come from `repo.robinhood_signals_pending_horizons`."""
        report = RobinhoodTrackReport()
        out: list[RobinhoodHorizonResult] = []

        for row in rows:
            wanted = [h for h in RH_HORIZONS if h.name not in set(row.get("recorded_horizons") or [])]
            if not wanted:
                continue

            # Nothing to resolve against without a pool: the candle endpoint is
            # keyed on the pool, not the token. Recorded as unresolvable with
            # its reason rather than skipped silently.
            pool = row.get("pool_address")
            if not pool:
                for horizon in wanted:
                    out.append(
                        self._unresolvable(
                            row, horizon.name,
                            "aucun pool enregistré avec la décision — "
                            "les bougies ne peuvent pas être retrouvées",
                        )
                    )
                    report.unresolvable += 1
                continue

            # Only fetch if at least one window has actually elapsed. A row
            # whose windows are all still open costs nothing.
            elapsed = [h for h in wanted if now_ms >= row["timestamp_ms"] + h.minutes * 60_000]
            if not elapsed:
                report.pending += len(wanted)
                continue

            try:
                candles = await self.gecko.pool_ohlcv(
                    pool, timeframe="minute", aggregate=CANDLE_MINUTES, limit=1000
                )
                report.requests_used += 1
            except Exception as exc:
                report.errors[row["contract_address"][:12]] = type(exc).__name__
                log.warning(
                    "rh_horizon_fetch_failed",
                    address=row["contract_address"][:12], error=type(exc).__name__,
                )
                continue

            for horizon in wanted:
                result = self._resolve_one(row, horizon, candles, now_ms=now_ms)
                if result.status is RobinhoodHorizonStatus.PENDING:
                    report.pending += 1
                    continue
                if result.status is RobinhoodHorizonStatus.UNRESOLVABLE:
                    report.unresolvable += 1
                else:
                    report.resolved += 1
                out.append(result)

        return out, report

    # ------------------------------------------------------------------ one #

    def _resolve_one(self, row, horizon: Horizon, candles, *, now_ms: int) -> RobinhoodHorizonResult:
        step_ms = CANDLE_MINUTES * 60_000
        signal_ms = int(row["timestamp_ms"])
        end_ms = signal_ms + horizon.minutes * 60_000

        if now_ms < end_ms:
            # Not yet. Absent, never settled at whatever is current
            # (invariant 14).
            return self._pending(row, horizon.name)

        if not candles:
            return self._unresolvable(row, horizon.name, "aucune bougie disponible pour ce pool")

        # The decision's own bar, matched by exact open time. Resolving against
        # the nearest one would shift every window by up to a full bar, always
        # in whichever direction the clock happened to fall.
        anchor_open = (signal_ms // step_ms) * step_ms
        index = next((i for i, c in enumerate(candles) if c[0] == anchor_open), None)
        if index is None:
            return self._unresolvable(
                row, horizon.name,
                "la bougie de la décision est absente de la série (trou dans l'indexeur)",
            )
        if index + 1 >= len(candles):
            return self._unresolvable(
                row, horizon.name, "aucune bougie après la décision : entrée impossible à situer"
            )

        # Entry is the open of the bar *after* the decision. Entering at the
        # close that produced the decision would be look-ahead.
        entry = candles[index + 1][1]
        if entry <= 0:
            return self._unresolvable(row, horizon.name, "prix d'entrée non positif")

        target_open = (end_ms // step_ms) * step_ms
        target = next((c for c in candles if c[0] == target_open), None)
        if target is None:
            return self._unresolvable(
                row, horizon.name,
                "la bougie de fin de fenêtre est absente de la série",
            )

        window = [c for c in candles if candles[index + 1][0] <= c[0] <= target_open]
        if not window:
            return self._unresolvable(row, horizon.name, "fenêtre vide")

        close = target[4]
        change = (close - entry) / entry * 100.0
        net = change - self.costs.round_trip_pct
        high = max(c[2] for c in window)
        low = min(c[3] for c in window)

        return RobinhoodHorizonResult(
            signal_id=row["id"],
            contract_address=row["contract_address"],
            symbol=row.get("symbol"),
            horizon=horizon.name,
            status=RobinhoodHorizonStatus.RESOLVED,
            entry_price=entry,
            price_at_horizon=close,
            change_pct=round(change, 4),
            net_change_pct=round(net, 4),
            max_gain_pct=round((high - entry) / entry * 100.0, 4),
            max_drawdown_pct=round((low - entry) / entry * 100.0, 4),
        )

    def _pending(self, row, horizon: str) -> RobinhoodHorizonResult:
        return RobinhoodHorizonResult(
            signal_id=row["id"], contract_address=row["contract_address"],
            symbol=row.get("symbol"), horizon=horizon,
            status=RobinhoodHorizonStatus.PENDING,
        )

    def _unresolvable(self, row, horizon: str, note: str) -> RobinhoodHorizonResult:
        return RobinhoodHorizonResult(
            signal_id=row["id"], contract_address=row["contract_address"],
            symbol=row.get("symbol"), horizon=horizon,
            status=RobinhoodHorizonStatus.UNRESOLVABLE, note=note,
        )
