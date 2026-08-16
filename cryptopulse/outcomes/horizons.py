"""Multi-horizon follow-up: what actually happened 15m, 1h, 4h and 24h later.

The triple-barrier label in `outcomes/tracker.py` answers one question — did the
target come before the stop? — and answers it well. But it collapses a whole
price path into a single word, and it cannot tell you that a signal was up 3%
after an hour and gave it all back by the next morning.

This module records the path instead of a verdict:

  * the price at each horizon, and the change from entry;
  * the best the trade ever looked (max gain);
  * the worst it ever looked (max drawdown);
  * whether it met a documented success criterion at that horizon.

The two are complementary and both are kept. A barrier label is what you would
have *traded*; a horizon snapshot is what the market actually *did*.

SUCCESS CRITERION, stated once and applied everywhere: a horizon is a success
when the change from entry, **after the modelled round-trip cost**, is greater
than zero. That is deliberately unforgiving — a signal that ends flat is not a
win, and paying the spread means flat is a small loss. The criterion lives in
`HorizonResult.is_success` and nowhere else, so it cannot drift between the
storage layer and the statistics.

Every rule from the barrier tracker applies here too:

  * entry is the **open of the bar after** the signal, never the signal's close;
  * the signal's bar must match by exact close time, never by nearest neighbour;
  * a horizon that has not fully elapsed stays PENDING rather than being settled
    at whatever price happens to be current;
  * bars that can no longer be fetched produce UNRESOLVABLE with a reason, and
    are excluded from every rate rather than counted as losses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from cryptopulse.backtest.engine import CostModel
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import Timeframe
from cryptopulse.outcomes.tracker import MAX_FETCH_BARS, PendingSignal
from cryptopulse.providers.base import MarketDataProvider

log = get_logger("outcomes.horizons")

__all__ = ["Horizon", "HORIZONS", "HorizonStatus", "HorizonResult", "HorizonReport", "HorizonTracker"]


class HorizonStatus(str, Enum):
    RESOLVED = "RESOLVED"
    PENDING = "PENDING"          # not enough time has passed yet
    UNRESOLVABLE = "UNRESOLVABLE"  # the bars needed can no longer be fetched


@dataclass(frozen=True, slots=True)
class Horizon:
    name: str
    minutes: int

    def bars(self, timeframe: Timeframe) -> int:
        """How many bars of `timeframe` this horizon spans, at least one."""
        return max(1, (self.minutes * 60) // timeframe.seconds)


# The four follow-up windows. 24h on a 5m primary timeframe is 288 bars, well
# inside the 1000-bar fetch, so a single request covers every horizon at once.
HORIZONS: tuple[Horizon, ...] = (
    Horizon("15m", 15),
    Horizon("1h", 60),
    Horizon("4h", 240),
    Horizon("24h", 1440),
)


@dataclass(slots=True)
class HorizonResult:
    signal_id: int
    symbol: str
    horizon: str
    status: HorizonStatus
    entry_price: float | None = None
    price_at_horizon: float | None = None
    change_pct: float | None = None
    net_change_pct: float | None = None
    max_gain_pct: float | None = None       # best unrealised gain in the window
    max_drawdown_pct: float | None = None   # worst unrealised loss in the window
    bars_seen: int | None = None
    note: str | None = None

    @property
    def is_success(self) -> bool | None:
        """The one place the success rule lives: net change above zero.

        `None` when the horizon has no verdict — a pending or unresolvable
        window is not a failure, and must never be counted as one.
        """
        if self.status is not HorizonStatus.RESOLVED or self.net_change_pct is None:
            return None
        return self.net_change_pct > 0.0

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "horizon": self.horizon,
            "status": self.status.value,
            "entry_price": self.entry_price,
            "price_at_horizon": self.price_at_horizon,
            "change_pct": _r(self.change_pct),
            "net_change_pct": _r(self.net_change_pct),
            "max_gain_pct": _r(self.max_gain_pct),
            "max_drawdown_pct": _r(self.max_drawdown_pct),
            "bars_seen": self.bars_seen,
            "success": self.is_success,
            "note": self.note,
        }


def _r(x, nd: int = 4):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if np.isfinite(v) else None


@dataclass(slots=True)
class HorizonReport:
    checked: int = 0
    resolved: int = 0
    pending: int = 0
    unresolvable: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    results: list[HorizonResult] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "checked_signals": self.checked,
            "resolved_horizons": self.resolved,
            "pending_horizons": self.pending,
            "unresolvable_horizons": self.unresolvable,
            "errors": self.errors,
            "duration_ms": self.duration_ms,
            "horizons": [h.name for h in HORIZONS],
        }


class HorizonTracker:
    """Follows each signal across the four horizons, one fetch per symbol."""

    def __init__(
        self,
        settings: CryptoPulseSettings,
        provider: MarketDataProvider,
        *,
        costs: CostModel | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.costs = costs or CostModel()
        self.clock = clock
        self.timeframe = settings.scanner.primary_timeframe

    def ready_before_ms(self, horizon: Horizon | None = None) -> int:
        """Newest signal timestamp with any chance of a resolvable horizon.

        Defaults to the shortest window: a signal older than 15 minutes can have
        at least its first horizon settled, even if 24h is still open.
        """
        h = horizon or HORIZONS[0]
        return self.clock.now_ms() - (h.bars(self.timeframe) + 2) * self.timeframe.ms

    async def track(self, signals: list[PendingSignal]) -> HorizonReport:
        import time

        t0 = time.perf_counter()
        report = HorizonReport(checked=len(signals))
        if not signals:
            return report

        by_symbol: dict[str, list[PendingSignal]] = {}
        for sig in signals:
            by_symbol.setdefault(sig.symbol, []).append(sig)

        sem = asyncio.Semaphore(self.settings.providers.max_concurrent_requests)

        async def handle(symbol: str, sigs: list[PendingSignal]) -> list[HorizonResult] | str:
            async with sem:
                try:
                    series = (await self.provider.get_ohlcv(symbol, self.timeframe, MAX_FETCH_BARS)).closed()
                except Exception as exc:
                    return f"{type(exc).__name__}: {exc}"
            if len(series) == 0:
                return "no closed candles returned"
            out: list[HorizonResult] = []
            for sig in sigs:
                out.extend(self._track_one(sig, series))
            return out

        gathered = await asyncio.gather(*(handle(s, v) for s, v in by_symbol.items()))

        for (symbol, _sigs), outcome in zip(by_symbol.items(), gathered, strict=True):
            if isinstance(outcome, str):
                report.errors[symbol] = outcome
                continue
            for res in outcome:
                report.results.append(res)
                if res.status is HorizonStatus.RESOLVED:
                    report.resolved += 1
                elif res.status is HorizonStatus.PENDING:
                    report.pending += 1
                else:
                    report.unresolvable += 1

        report.duration_ms = int((time.perf_counter() - t0) * 1000)
        log.info(
            "horizons_tracked",
            signals=report.checked,
            resolved=report.resolved,
            pending=report.pending,
            unresolvable=report.unresolvable,
        )
        return report

    def _track_one(self, sig: PendingSignal, series) -> list[HorizonResult]:
        """Every horizon for one signal, sharing a single fetched series."""
        matches = np.flatnonzero(series.close_time_ms == sig.timestamp_ms)
        if matches.size == 0:
            oldest = int(series.close_time_ms[0]) if len(series) else 0
            note = (
                f"signal bar predates the deepest fetchable history ({MAX_FETCH_BARS} bars)"
                if sig.timestamp_ms < oldest
                else "the signal's candle is missing from the provider's series (feed gap)"
            )
            return [
                HorizonResult(sig.id, sig.symbol, h.name, HorizonStatus.UNRESOLVABLE, note=note)
                for h in HORIZONS
            ]

        idx = int(matches[0])
        entry_index = idx + 1
        if entry_index >= len(series):
            # The bar after the signal has not printed yet: nothing to enter on.
            return [
                HorizonResult(sig.id, sig.symbol, h.name, HorizonStatus.PENDING) for h in HORIZONS
            ]

        entry = float(series.open[entry_index])
        if entry <= 0:
            return [
                HorizonResult(
                    sig.id, sig.symbol, h.name, HorizonStatus.UNRESOLVABLE,
                    note="entry price is not positive",
                )
                for h in HORIZONS
            ]

        out: list[HorizonResult] = []
        for h in HORIZONS:
            span = h.bars(self.timeframe)
            last_index = entry_index + span - 1
            if last_index >= len(series):
                # The window has not fully elapsed. Reporting the current price
                # here would silently turn "not finished" into a result.
                out.append(HorizonResult(sig.id, sig.symbol, h.name, HorizonStatus.PENDING))
                continue

            window_high = series.high[entry_index : last_index + 1]
            window_low = series.low[entry_index : last_index + 1]
            close = float(series.close[last_index])

            change = (close - entry) / entry * 100.0
            out.append(
                HorizonResult(
                    signal_id=sig.id,
                    symbol=sig.symbol,
                    horizon=h.name,
                    status=HorizonStatus.RESOLVED,
                    entry_price=entry,
                    price_at_horizon=close,
                    change_pct=change,
                    net_change_pct=self.costs.apply(change),
                    max_gain_pct=(float(np.max(window_high)) - entry) / entry * 100.0,
                    max_drawdown_pct=(float(np.min(window_low)) - entry) / entry * 100.0,
                    bars_seen=span,
                )
            )
        return out
