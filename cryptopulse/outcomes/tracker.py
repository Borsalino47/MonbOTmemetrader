"""Outcome tracker — resolves emitted signals against what actually happened.

This is the module that turns the scanner from a thing that produces opinions
into a thing that can be held to account. Every signal in the journal starts with
NULL outcome columns; this job revisits them once enough time has passed, fetches
the bars that followed, applies the triple-barrier label, and records the result.

**Why this is not look-ahead.** Look-ahead is using future data to *make* a
decision. This uses future data to *grade* a decision that was already made and
already written to disk with its timestamp. The signal's score was computed from
bars closed at or before `signal.timestamp_ms`; resolution only reads bars strictly
after it. The two never touch.

Guarantees:

* **Exact bar match required, within a timeframe.** The signal's `timestamp_ms`
  is the close time of the candle it was scored on. When the label resolves on
  that same timeframe, resolution looks for that exact close time and refuses to
  fall back on a neighbour — an off-by-one bar silently shifts every barrier.
* **Across timeframes, the shift is stated rather than hidden.** A ×10 thesis is
  graded on daily bars even though the signal fired on a 5-minute close, so no
  daily bar closes at the signal's timestamp. Resolution then takes the daily bar
  the signal fired *inside*, and entry is the open of the one after it — the
  earliest price anyone could actually have paid. The resulting lag is recorded
  in the resolution note, never silently absorbed.
* **The ATR is the one recorded at signal time**, never recomputed from data the
  signal did not have.
* **Too early is not a result.** If the horizon has not elapsed, the signal stays
  pending. It is not settled at whatever price happens to be current.
* **Too late is stated, not faked.** If the bars needed have fallen out of the
  provider's reachable window, the signal is marked `UNRESOLVABLE` with a reason
  and excluded from every rate. It is not quietly dropped, and it never counts as
  a loss.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import numpy as np

from cryptopulse.backtest.engine import CostModel
from cryptopulse.backtest.labels import DEFAULT_LABEL_CONFIGS, LabelConfig, Outcome, label_signal
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import Timeframe
from cryptopulse.providers.base import MarketDataProvider

log = get_logger("outcomes.tracker")

__all__ = ["OutcomeTracker", "ResolutionReport", "PendingSignal", "Resolution"]

# Binance caps a klines request at 1000 rows, so this is the deepest lookback a
# single fetch can cover. Signals older than this many bars are unresolvable.
MAX_FETCH_BARS = 1000


@dataclass(slots=True)
class PendingSignal:
    """The subset of a journal row that resolution needs."""

    id: int
    symbol: str
    timestamp_ms: int
    price: float
    atr: float | None
    timeframe: Timeframe


@dataclass(slots=True)
class Resolution:
    signal_id: int
    symbol: str
    label: str
    label_config: str
    horizon_bars: int
    return_pct: float | None
    net_return_pct: float | None
    mfe_atr: float | None
    mae_atr: float | None
    bars_held: int | None
    entry_price: float | None
    exit_price: float | None
    note: str | None = None
    # Highest multiple of entry touched inside the horizon. Recorded on every
    # row, so the journal can be asked "how many ever reached x10" without
    # re-grading anything.
    max_multiple: float | None = None


@dataclass(slots=True)
class ResolutionReport:
    checked: int = 0
    resolved: int = 0
    still_pending: int = 0
    unresolvable: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    by_label: dict[str, int] = field(default_factory=dict)
    resolutions: list[Resolution] = field(default_factory=list)
    label_config: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "resolved": self.resolved,
            "still_pending": self.still_pending,
            "unresolvable": self.unresolvable,
            "by_label": self.by_label,
            "errors": self.errors,
            "label_config": self.label_config,
            "duration_ms": self.duration_ms,
        }


class OutcomeTracker:
    """Resolves pending signals in batches, one provider fetch per symbol."""

    def __init__(
        self,
        settings: CryptoPulseSettings,
        provider: MarketDataProvider,
        *,
        label_config: LabelConfig | None = None,
        costs: CostModel | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.label = label_config or DEFAULT_LABEL_CONFIGS[1]  # standard_2R
        self.costs = costs or CostModel()
        self.clock = clock
        # The timeframe the *outcome* is measured on. A weeks-long thesis cannot
        # be graded on 5-minute bars: 1000 of them reach back three days.
        self.timeframe = self.label.timeframe or settings.scanner.primary_timeframe

    # ---------------------------------------------------------------- timing #

    def ready_before_ms(self) -> int:
        """Newest signal timestamp that could possibly be resolved yet.

        A signal needs its entry bar plus `horizon_bars` more to close. Anything
        newer than this cannot have a verdict, so there is no point fetching for it.
        """
        needed_bars = self.label.horizon_bars + 2
        return self.clock.now_ms() - needed_bars * self.timeframe.ms

    def unresolvable_before_ms(self) -> int:
        """Oldest signal timestamp a single provider fetch can still reach."""
        return self.clock.now_ms() - (MAX_FETCH_BARS - self.label.horizon_bars - 2) * self.timeframe.ms

    # ------------------------------------------------------------- resolution #

    async def resolve(self, pending: list[PendingSignal]) -> ResolutionReport:
        """Resolve as many of `pending` as the available bars allow."""
        import time

        t0 = time.perf_counter()
        report = ResolutionReport(checked=len(pending), label_config=self.label.name)
        if not pending:
            return report

        by_symbol: dict[str, list[PendingSignal]] = {}
        for sig in pending:
            by_symbol.setdefault(sig.symbol, []).append(sig)

        sem = asyncio.Semaphore(self.settings.providers.max_concurrent_requests)

        async def handle(symbol: str, signals: list[PendingSignal]) -> list[Resolution] | str:
            async with sem:
                try:
                    series = (await self.provider.get_ohlcv(symbol, self.timeframe, MAX_FETCH_BARS)).closed()
                except Exception as exc:
                    return f"{type(exc).__name__}: {exc}"
            if len(series) == 0:
                return "no closed candles returned"
            return [self._resolve_one(sig, series) for sig in signals]

        results = await asyncio.gather(*(handle(s, sigs) for s, sigs in by_symbol.items()))

        for (symbol, _sigs), outcome in zip(by_symbol.items(), results, strict=True):
            if isinstance(outcome, str):
                report.errors[symbol] = outcome
                continue
            for res in outcome:
                if res is None:
                    report.still_pending += 1
                    continue
                report.resolutions.append(res)
                if res.label == Outcome.UNRESOLVABLE.value:
                    report.unresolvable += 1
                else:
                    report.resolved += 1
                report.by_label[res.label] = report.by_label.get(res.label, 0) + 1

        report.duration_ms = int((time.perf_counter() - t0) * 1000)
        log.info(
            "outcomes_resolved",
            checked=report.checked,
            resolved=report.resolved,
            pending=report.still_pending,
            unresolvable=report.unresolvable,
            by_label=report.by_label,
            label=self.label.name,
        )
        return report

    def _unresolvable(self, sig: PendingSignal, note: str) -> Resolution:
        return Resolution(
            signal_id=sig.id, symbol=sig.symbol, label=Outcome.UNRESOLVABLE.value,
            label_config=self.label.name, horizon_bars=self.label.horizon_bars,
            return_pct=None, net_return_pct=None, mfe_atr=None, mae_atr=None,
            bars_held=None, entry_price=None, exit_price=None, note=note,
        )

    def _entry_index(self, sig: PendingSignal, series) -> tuple[int | None, str | None, str | None]:
        """Locate the signal's bar in `series`. Returns (index, note, failure).

        Two regimes, and the difference is deliberate:

        * **Same timeframe** — the signal's close time must be present exactly.
          A neighbouring bar would shift every barrier, so its absence is a feed
          gap and the signal is unresolvable, not approximated.
        * **Slower timeframe** — no daily bar closes at a 5-minute timestamp, so
          the signal is placed in the daily bar it fired inside. That costs up to
          one bar of lag, which is real and is written into the note rather than
          being quietly absorbed.
        """
        if series.close_time_ms.size == 0:
            return None, None, "no closed candles returned"

        oldest = int(series.close_time_ms[0])
        if sig.timestamp_ms < oldest:
            return None, None, (
                f"signal bar predates the deepest fetchable history ({MAX_FETCH_BARS} "
                f"{self.timeframe.value} bars)"
            )

        matches = np.flatnonzero(series.close_time_ms == sig.timestamp_ms)
        if matches.size:
            return int(matches[0]), None, None

        if sig.timeframe is self.timeframe:
            return None, None, "the signal's candle is missing from the provider's series (feed gap)"

        # Cross-timeframe: the first bar that closed at or after the signal is
        # the bar the signal happened inside.
        idx = int(np.searchsorted(series.close_time_ms, sig.timestamp_ms, side="left"))
        if idx >= series.close_time_ms.size:
            return None, None, "the signal is newer than the newest closed bar of the outcome timeframe"
        lag_ms = int(series.close_time_ms[idx]) - sig.timestamp_ms
        note = (
            f"graded on {self.timeframe.value} bars; the {sig.timeframe.value} signal fired "
            f"{lag_ms / 60000:.0f} min before that bar closed, and entry is the open of the next one"
        )
        return idx, note, None

    def _resolve_one(self, sig: PendingSignal, series) -> Resolution | None:
        """Grade one signal. Returns None when it is simply too early to say."""
        # ATR barriers need the ATR recorded at signal time. Multiple barriers do
        # not — they are a function of price — so a missing ATR is only fatal for
        # the ATR family.
        if self.label.needs_atr and (not sig.atr or sig.atr <= 0):
            return self._unresolvable(
                sig, "no ATR was recorded with the signal, so ATR-scaled barriers cannot be placed"
            )

        idx, note, failure = self._entry_index(sig, series)
        if failure is not None:
            return self._unresolvable(sig, failure)

        result = label_signal(
            series.high, series.low, series.close, series.open, idx, sig.atr or 0.0, self.label
        )

        if result.outcome is Outcome.UNRESOLVED:
            # Not enough future bars yet. Genuinely unknown — leave it pending.
            return None

        net = self.costs.apply(result.return_pct)
        return Resolution(
            signal_id=sig.id,
            symbol=sig.symbol,
            label=result.outcome.value,
            label_config=self.label.name,
            horizon_bars=self.label.horizon_bars,
            return_pct=result.return_pct,
            net_return_pct=net,
            mfe_atr=None if not np.isfinite(result.mfe_atr) else result.mfe_atr,
            mae_atr=None if not np.isfinite(result.mae_atr) else result.mae_atr,
            bars_held=result.bars_held,
            entry_price=result.entry_price,
            exit_price=result.exit_price,
            note=note,
            max_multiple=result.max_multiple,
        )
