"""Finding the accelerations a token has actually had.

WHY EPISODES RATHER THAN A THRESHOLD

The obvious design fixes a threshold — "a pump is +5% in an hour" — and scans for
it. That forces the threshold to be chosen before anything is known, and a
different question ("what about +10%?") means another pass over the data.

Instead this finds *episodes*: a run from a local trough to a local peak, with
the size recorded rather than tested. One pass then answers every threshold, and
the statistics layer can bucket by size without re-reading a single candle.

WHICH TIMEFRAME, AND WHY IT IS 1h BY DEFAULT

Binance caps a klines request at 1000 rows, so the timeframe *is* the history
depth, and the choice is a real trade-off measured rather than guessed:

    5m   1000 bars =  3.5 days   — a handful of episodes, never a sample
    15m  1000 bars = 10.4 days   — 5-15 episodes, still under the n>=20 floor
    1h   1000 bars = 41.7 days   — 20-40 episodes on an active token
    4h   1000 bars = 166 days    — good sample, useless timing resolution

This project refuses to print a rate below 20 observations. On 5m or 15m the
statistics would therefore read "insufficient sample" essentially forever, which
is honest but useless. 1h is the shortest timeframe that produces a usable
sample, and it is already in the candle cache for any scanned symbol — 300 bars
(12.5 days) cost nothing at all, and the full 41 days cost one request.

The price of that choice is timing resolution: "time to peak" is known to the
hour, not the minute. Every episode therefore carries `resolution_minutes`, and
nothing downstream may render a precision it does not have.

NO LOOK-AHEAD, EVEN THOUGH THIS IS HISTORY

Detection reads closed candles only. More importantly, the *context* recorded at
an episode's start — the volume and volatility state a live scanner would have
seen — is computed strictly from bars at or before that start. That is what makes
the similarity comparison in `pumps/stats.py` meaningful rather than circular: a
setup is described by what was knowable then, never by what happened next.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import OHLCVSeries, Timeframe

log = get_logger("pumps.detect")

__all__ = ["PumpConfig", "PumpEpisode", "find_pumps", "DEFAULT_PUMP_CONFIG", "SIZE_BUCKETS"]

# Sizes an episode is classified into. Chosen so the smallest is still worth
# trading after the ~40 bps modelled round trip; below that a "pump" is noise.
SIZE_BUCKETS: tuple[float, ...] = (3.0, 5.0, 10.0, 20.0, 50.0, 100.0)


@dataclass(frozen=True, slots=True)
class PumpConfig:
    """What counts as an episode. Every value is a stated choice, not a constant."""

    # Below this the move does not clear costs and is not an event.
    min_gain_pct: float = 3.0
    # A rise spread over longer than this is a trend, not an acceleration.
    max_bars_to_peak: int = 24
    # A pullback of this fraction of the run ends the episode.
    end_retrace_fraction: float = 0.5
    # Two peaks closer than this belong to the same episode.
    min_bars_between_episodes: int = 6
    # How far above its window's low a bar may sit and still count as the start.
    # Requiring the *exact* minimum bar proved far too brittle: on ordinary noise
    # it accepted 29 bars in 400 and missed real runs whose low happened to be a
    # bar or two earlier. A run beginning just off the low is the same run.
    trough_tolerance_pct: float = 0.4

    def describe(self) -> str:
        return (
            f"a rise of at least {self.min_gain_pct:.0f}% reached within "
            f"{self.max_bars_to_peak} bars from a local low, ending when price gives back "
            f"{self.end_retrace_fraction:.0%} of the run"
        )


DEFAULT_PUMP_CONFIG = PumpConfig()


@dataclass(slots=True)
class PumpEpisode:
    """One acceleration that actually happened, with the state that preceded it."""

    symbol: str
    timeframe: str
    resolution_minutes: int

    start_ms: int
    start_price: float
    peak_ms: int
    peak_price: float

    gain_pct: float
    bars_to_peak: int
    minutes_to_peak: int

    # What happened after the peak, within the same lookahead window.
    drawdown_after_pct: float | None = None

    # The state a live scanner would have seen at the start. Computed strictly
    # from bars at or before `start_ms` — this is what makes similarity honest.
    rvol_at_start: float | None = None
    volume_change_before_pct: float | None = None
    range_position_at_start: float | None = None
    atr_pct_at_start: float | None = None

    @property
    def size_bucket(self) -> str:
        """The largest documented threshold this episode cleared."""
        cleared = [b for b in SIZE_BUCKETS if self.gain_pct >= b]
        return f"{cleared[-1]:.0f}%+" if cleared else "below floor"

    def reached(self, threshold_pct: float) -> bool:
        return self.gain_pct >= threshold_pct

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "resolution_minutes": self.resolution_minutes,
            "start_ms": self.start_ms,
            "start_price": self.start_price,
            "peak_ms": self.peak_ms,
            "peak_price": self.peak_price,
            "gain_pct": round(self.gain_pct, 3),
            "size_bucket": self.size_bucket,
            "bars_to_peak": self.bars_to_peak,
            "minutes_to_peak": self.minutes_to_peak,
            "drawdown_after_pct": _r(self.drawdown_after_pct),
            "rvol_at_start": _r(self.rvol_at_start),
            "volume_change_before_pct": _r(self.volume_change_before_pct),
            "range_position_at_start": _r(self.range_position_at_start),
            "atr_pct_at_start": _r(self.atr_pct_at_start),
        }


def _r(x, nd: int = 3):
    if x is None:
        return None
    v = float(x)
    return round(v, nd) if np.isfinite(v) else None


def find_pumps(
    series: OHLCVSeries,
    *,
    config: PumpConfig = DEFAULT_PUMP_CONFIG,
    warmup_bars: int = 50,
) -> list[PumpEpisode]:
    """Every episode in `series`, oldest first. Never invents one.

    `warmup_bars` is the history each episode's start context needs. Episodes
    beginning before it are still detected, but their context fields are `None`
    rather than computed from too little data.
    """
    closed = series.closed()
    n = len(closed)
    if n < 10:
        return []

    tf = closed.timeframe if isinstance(closed.timeframe, Timeframe) else Timeframe(closed.timeframe)
    minutes = max(1, tf.seconds // 60)

    high, low = closed.high, closed.low
    k = max(2, config.min_bars_between_episodes)

    # Two passes, because a greedy chronological walk gets this wrong: a small
    # early wiggle would be accepted first and then block the large run right
    # behind it. Collect every candidate run, then keep the biggest ones that do
    # not overlap — which is what a reader means by "this token's pumps".
    candidates: list[tuple[float, int, int]] = []   # (gain, trough, peak)
    for i in range(k, n - 1):
        start_price = float(low[i])
        if start_price <= 0:
            continue
        window_low = float(np.min(low[i - k : min(n, i + k + 1)]))
        if start_price > window_low * (1.0 + config.trough_tolerance_pct / 100.0):
            continue  # not near a local low

        window_end = min(n, i + config.max_bars_to_peak + 1)
        window_high = high[i + 1 : window_end]
        if window_high.size == 0:
            break

        rel = (window_high - start_price) / start_price * 100.0
        best = int(np.argmax(rel))
        gain = float(rel[best])
        if gain >= config.min_gain_pct:
            candidates.append((gain, i, i + 1 + best))

    taken: list[tuple[int, int]] = []
    chosen: list[tuple[int, int]] = []
    for _gain, trough, peak in sorted(candidates, key=lambda c: c[0], reverse=True):
        if any(trough <= p and peak >= t for t, p in taken):
            continue  # overlaps a run already recorded
        taken.append((trough, peak))
        chosen.append((trough, peak))

    episodes = [
        _build_episode(
            closed, t, p,
            float(low[t]), (float(high[p]) - float(low[t])) / float(low[t]) * 100.0,
            tf, minutes, config, warmup_bars,
        )
        for t, p in sorted(chosen)
    ]

    log.info("pumps_detected", symbol=closed.symbol, timeframe=tf.value, episodes=len(episodes))
    return episodes


def _build_episode(
    s: OHLCVSeries,
    start: int,
    peak: int,
    start_price: float,
    gain: float,
    tf: Timeframe,
    minutes: int,
    config: PumpConfig,
    warmup: int,
) -> PumpEpisode:
    n = len(s)
    peak_price = float(s.high[peak])

    # How far it gave back, inside a window of the same length as the run.
    after_end = min(n, peak + 1 + max(4, peak - start))
    drawdown = None
    if after_end > peak + 1 and peak_price > 0:
        trough_after = float(np.min(s.low[peak + 1 : after_end]))
        drawdown = (trough_after - peak_price) / peak_price * 100.0

    ep = PumpEpisode(
        symbol=s.symbol,
        timeframe=tf.value,
        resolution_minutes=minutes,
        start_ms=int(s.close_time_ms[start]),
        start_price=start_price,
        peak_ms=int(s.close_time_ms[peak]),
        peak_price=peak_price,
        gain_pct=gain,
        bars_to_peak=peak - start,
        minutes_to_peak=(peak - start) * minutes,
        drawdown_after_pct=drawdown,
    )
    _attach_start_context(ep, s, start, warmup)
    return ep


def _attach_start_context(ep: PumpEpisode, s: OHLCVSeries, start: int, warmup: int) -> None:
    """What a live scanner would have seen at the moment the run began.

    Strictly backward-looking. Using anything at or after the peak would make the
    similarity comparison circular — it would "discover" that pumps are preceded
    by pumps.
    """
    if start < warmup:
        return  # too little history; the fields stay None rather than being guessed

    vol = s.volume[:start + 1]
    if vol.size >= warmup:
        baseline = float(np.mean(vol[-warmup:-1])) if vol.size > 1 else 0.0
        if baseline > 0:
            ep.rvol_at_start = float(vol[-1]) / baseline
        recent = float(np.mean(vol[-6:]))
        earlier = float(np.mean(vol[-warmup:-6])) if vol.size > warmup else 0.0
        if earlier > 0:
            ep.volume_change_before_pct = (recent - earlier) / earlier * 100.0

    window_high = float(np.max(s.high[start + 1 - warmup : start + 1]))
    window_low = float(np.min(s.low[start + 1 - warmup : start + 1]))
    span = window_high - window_low
    if span > 0:
        ep.range_position_at_start = max(
            0.0, min(1.0, (float(s.close[start]) - window_low) / span)
        )

    # True range over the warm-up, as a percentage of price: the volatility a
    # scanner would have measured before the move.
    h = s.high[start + 1 - 14 : start + 1]
    lo = s.low[start + 1 - 14 : start + 1]
    if h.size >= 5:
        tr = float(np.mean(h - lo))
        price = float(s.close[start])
        if price > 0:
            ep.atr_pct_at_start = tr / price * 100.0


@dataclass(slots=True)
class PumpHistory:
    """Every episode found for one symbol, with what it took to find them."""

    symbol: str
    timeframe: str
    bars_examined: int
    days_covered: float
    config: PumpConfig
    episodes: list[PumpEpisode] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars_examined": self.bars_examined,
            "days_covered": round(self.days_covered, 2),
            "resolution_minutes": self.episodes[0].resolution_minutes if self.episodes else None,
            "definition": self.config.describe(),
            "episodes_found": len(self.episodes),
            "notes": self.notes,
            "episodes": [e.to_dict() for e in self.episodes],
        }


def build_history(
    series: OHLCVSeries, *, config: PumpConfig = DEFAULT_PUMP_CONFIG
) -> PumpHistory:
    """Detection plus the context needed to read it honestly."""
    closed = series.closed()
    tf = closed.timeframe if isinstance(closed.timeframe, Timeframe) else Timeframe(closed.timeframe)
    days = len(closed) * tf.seconds / 86_400.0

    episodes = find_pumps(closed, config=config)
    history = PumpHistory(
        symbol=closed.symbol,
        timeframe=tf.value,
        bars_examined=len(closed),
        days_covered=days,
        config=config,
        episodes=episodes,
    )
    if episodes:
        history.notes.append(
            f"Timing is known to the {tf.value} bar: 'time to peak' has "
            f"{episodes[0].resolution_minutes}-minute resolution, not minute precision."
        )
    else:
        history.notes.append(
            f"No episode matching the definition in {days:.1f} days of history. "
            "That is a finding, not a failure — this token has not accelerated."
        )
    return history
