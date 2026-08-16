"""Wide pre-scan: which tokens deserve an expensive look, out of the whole venue.

THE PROBLEM THIS SOLVES

The classic scanner builds its universe by sorting every pair on 24h volume and
keeping the top 120. That filter *structurally excludes* the assets this module
exists to find: a token nobody is watching yet is, by definition, not in the top
120 by volume. The scanner could never answer "is there something interesting
that I am not already following?", however good its scoring was.

It also could not be made to, by scaling: 1000 tokens across four timeframes is
4000 kline requests, roughly 8000 weight per minute against a documented ~6000
budget. Arithmetically impossible.

THE SHAPE OF THE ANSWER

One request — `/ticker/24hr` with no symbol filter — returns every pair on the
venue in a single response. From it, at zero marginal cost, each symbol yields
price, 24h change, quote volume, high/low, trade count and (on Binance) the best
bid and ask. That is enough to rank the whole venue and hand 30-50 candidates to
the expensive deep scan.

Better still, the scanner already makes that call every cycle. The hunter reads
the same dictionary, so **the wide search costs no additional requests at all**.

WHAT A ROLLING-COUNTER DELTA ACTUALLY MEASURES

This is the part that is easy to get wrong, and the reason for most of the care
below. `quoteVolume` and `count` are **24-hour rolling** figures. The difference
between two readings 60 seconds apart is therefore NOT "the volume of the last
60 seconds". It is:

    volume added in the last 60s  −  volume that just aged out of the window

In other words it compares this moment against *the same moment yesterday*. A
token trading steadily shows a delta near zero however large its volume. A token
whose activity has genuinely picked up shows a strongly positive delta.

That makes it a real anomaly detector, and arguably a better one than raw recent
volume — but it is not recent volume, and calling it "volume 1m" would be a
fabrication. Every field below is named for what it measures.

WHAT THIS MODULE IS NOT

It produces a **selection priority**, not an opinion about a token. It answers
"is this worth spending four kline requests on?" and nothing else. The judgement
belongs to the deep scan and the Opportunity Score, which see the price history
this stage deliberately never fetches. A high priority here means *look closer*,
never *this is good*.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import Ticker24h

log = get_logger("hunter.discovery")

__all__ = [
    "TokenSnapshot",
    "SnapshotMemory",
    "Candidate",
    "PrescanReport",
    "prescan",
    "MIN_SECONDS_BETWEEN_SNAPSHOTS",
]

# Two readings closer together than this produce a delta dominated by noise:
# a handful of trades against a 24-hour window.
MIN_SECONDS_BETWEEN_SNAPSHOTS = 30.0

# Below this a "pair" is not tradeable in any size, and its percentage moves are
# an artifact of a thin book rather than a signal.
ABSOLUTE_MIN_QUOTE_VOLUME = 50_000.0

# A spread this wide eats any move it might be signalling.
MAX_SPREAD_BPS = 250.0


@dataclass(frozen=True, slots=True)
class TokenSnapshot:
    """The cheap facts about one symbol at one instant."""

    symbol: str
    at_ms: int
    price: float
    quote_volume_24h: float
    trades_24h: int
    change_pct_24h: float
    range_position: float | None
    spread_bps: float | None
    avg_trade_size: float | None

    @classmethod
    def from_ticker(cls, t: Ticker24h, at_ms: int) -> TokenSnapshot:
        return cls(
            symbol=t.symbol,
            at_ms=at_ms,
            price=t.last_price,
            quote_volume_24h=t.quote_volume_24h,
            trades_24h=t.trades_24h,
            change_pct_24h=t.price_change_pct_24h,
            range_position=t.range_position_24h,
            spread_bps=t.spread_bps,
            avg_trade_size=t.avg_trade_size,
        )


@dataclass
class SnapshotMemory:
    """Keeps the previous reading per symbol so deltas can be computed.

    Deliberately in memory and deliberately small: one row per symbol, replaced
    each cycle. It is not a history and makes no claim to be one — after a
    restart there is no previous reading, and the report says so rather than
    reporting a delta of zero.
    """

    clock: Clock = SYSTEM_CLOCK
    _previous: dict[str, TokenSnapshot] = field(default_factory=dict)

    def previous(self, symbol: str) -> TokenSnapshot | None:
        return self._previous.get(symbol)

    def record(self, snapshots: list[TokenSnapshot]) -> None:
        for s in snapshots:
            self._previous[s.symbol] = s

    @property
    def size(self) -> int:
        return len(self._previous)


@dataclass(slots=True)
class Candidate:
    """One symbol worth considering, with every signal named for what it is."""

    symbol: str
    price: float
    quote_volume_24h: float
    change_pct_24h: float
    range_position: float | None
    spread_bps: float | None
    trades_24h: int
    avg_trade_size: float | None

    # Deltas against the previous reading. `None` on the first cycle, or when
    # the readings are too close together for the difference to mean anything.
    seconds_since_previous: float | None = None
    volume_excess_vs_yesterday: float | None = None
    trade_excess_vs_yesterday: float | None = None
    price_change_since_previous_pct: float | None = None

    priority: float = 0.0
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "quote_volume_24h": self.quote_volume_24h,
            "change_pct_24h": _r(self.change_pct_24h),
            "range_position_24h": _r(self.range_position),
            "spread_bps": _r(self.spread_bps),
            "trades_24h": self.trades_24h,
            "avg_trade_size": _r(self.avg_trade_size),
            "seconds_since_previous": _r(self.seconds_since_previous, 1),
            # Named for what it measures: excess over the same moment yesterday,
            # not volume in the last minute.
            "volume_excess_vs_yesterday": _r(self.volume_excess_vs_yesterday),
            "trade_excess_vs_yesterday": _r(self.trade_excess_vs_yesterday),
            "price_change_since_previous_pct": _r(self.price_change_since_previous_pct),
            "priority": _r(self.priority, 2),
            "reasons": self.reasons,
            "caveats": self.caveats,
        }


def _r(x, nd: int = 4):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd)


@dataclass(slots=True)
class PrescanReport:
    universe_size: int = 0          # everything the venue listed
    eligible: int = 0               # what survived the tradeability floors
    candidates: list[Candidate] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)
    has_previous_reading: bool = False
    notes: list[str] = field(default_factory=list)
    requests_used: int = 0          # zero: this reads the scan's own ticker call
    computed_at_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "universe_size": self.universe_size,
            "eligible": self.eligible,
            "returned": len(self.candidates),
            "rejected": self.rejected,
            "has_previous_reading": self.has_previous_reading,
            "requests_used": self.requests_used,
            "computed_at_ms": self.computed_at_ms,
            "notes": self.notes,
            "candidates": [c.to_dict() for c in self.candidates],
        }


def prescan(
    tickers: dict[str, Ticker24h],
    memory: SnapshotMemory,
    *,
    quote_asset: str = "USDT",
    exclude_bases: tuple[str, ...] = (),
    exclude_patterns: tuple[str, ...] = (),
    min_quote_volume: float = ABSOLUTE_MIN_QUOTE_VOLUME,
    max_spread_bps: float = MAX_SPREAD_BPS,
    limit: int = 40,
    now_ms: int | None = None,
    record: bool = True,
) -> PrescanReport:
    """Rank the whole venue on cheap data and return the top `limit` candidates.

    Ranking is by *anomaly*, never by size. A $3M-volume token whose activity has
    doubled against yesterday outranks a $20B one trading normally — which is the
    entire point, and the opposite of what a volume-sorted universe does.

    `record=False` computes without storing the reading. That distinction was
    found by running the thing: an on-demand search that recorded would overwrite
    the previous snapshot, so tapping "search again" a few seconds after a scan
    reset the very acceleration signal the search exists to surface. Only the
    scheduled cycle advances the memory; a read never disturbs it.
    """
    now_ms = now_ms if now_ms is not None else memory.clock.now_ms()
    report = PrescanReport(universe_size=len(tickers), requests_used=0)
    quote = quote_asset.upper()

    snapshots: list[TokenSnapshot] = []
    candidates: list[Candidate] = []

    for symbol, t in tickers.items():
        if not symbol.endswith(quote):
            report.rejected["wrong_quote"] = report.rejected.get("wrong_quote", 0) + 1
            continue
        base = symbol[: -len(quote)]
        if base in exclude_bases:
            report.rejected["stablecoin"] = report.rejected.get("stablecoin", 0) + 1
            continue
        if any(p in base for p in exclude_patterns):
            report.rejected["leveraged_token"] = report.rejected.get("leveraged_token", 0) + 1
            continue
        if t.quote_volume_24h < min_quote_volume:
            report.rejected["untradeable_volume"] = report.rejected.get("untradeable_volume", 0) + 1
            continue

        spread = t.spread_bps
        if spread is not None and spread > max_spread_bps:
            report.rejected["spread_too_wide"] = report.rejected.get("spread_too_wide", 0) + 1
            continue

        snap = TokenSnapshot.from_ticker(t, now_ms)
        snapshots.append(snap)
        candidates.append(_build_candidate(snap, memory.previous(symbol)))

    report.eligible = len(candidates)
    report.has_previous_reading = any(c.seconds_since_previous is not None for c in candidates)

    if not report.has_previous_reading:
        # The honest statement of a real limitation: without a second reading
        # there is no acceleration to measure, and the ranking falls back to
        # static facts only. Reporting a delta of zero would be a fabrication.
        report.notes.append(
            "First reading of this run: no previous snapshot to compare against, so no "
            "acceleration signal is available yet. Ranking uses static facts only. "
            "The next cycle produces deltas."
        )

    candidates.sort(key=lambda c: c.priority, reverse=True)
    report.candidates = candidates[:limit]
    report.computed_at_ms = now_ms
    if record:
        memory.record(snapshots)

    log.info(
        "prescan_complete",
        universe=report.universe_size,
        eligible=report.eligible,
        returned=len(report.candidates),
        has_previous=report.has_previous_reading,
    )
    return report


def _build_candidate(now: TokenSnapshot, before: TokenSnapshot | None) -> Candidate:
    c = Candidate(
        symbol=now.symbol,
        price=now.price,
        quote_volume_24h=now.quote_volume_24h,
        change_pct_24h=now.change_pct_24h,
        range_position=now.range_position,
        spread_bps=now.spread_bps,
        trades_24h=now.trades_24h,
        avg_trade_size=now.avg_trade_size,
    )

    if before is not None:
        elapsed = (now.at_ms - before.at_ms) / 1000.0
        if elapsed >= MIN_SECONDS_BETWEEN_SNAPSHOTS:
            c.seconds_since_previous = elapsed
            c.volume_excess_vs_yesterday = _pct_change(
                before.quote_volume_24h, now.quote_volume_24h
            )
            c.trade_excess_vs_yesterday = _pct_change(
                float(before.trades_24h), float(now.trades_24h)
            )
            c.price_change_since_previous_pct = _pct_change(before.price, now.price)

    _score_priority(c)
    return c


def _pct_change(before: float, after: float) -> float | None:
    if before <= 0:
        return None
    return (after - before) / before * 100.0


def _score_priority(c: Candidate) -> None:
    """A transparent selection heuristic — deliberately not a score.

    This decides which tokens are worth four kline requests. It is not versioned
    and carries no weights fingerprint, because it is not an opinion about the
    asset: `TOKEN_DISCOVERY_SCORE` (phase 05) is, and it will read the price
    history that this stage never fetches.

    Every term below adds a reason, so a candidate can always explain why it was
    picked. A term whose input is unavailable adds a caveat instead of a zero.
    """
    priority = 0.0

    # -- acceleration: the reason this module exists ------------------------- #
    if c.volume_excess_vs_yesterday is None:
        c.caveats.append("no previous reading yet — acceleration unknown")
    elif c.volume_excess_vs_yesterday > 0:
        # A rolling-window delta is small in percentage terms even for a large
        # surge, so the scale here is intentionally generous.
        gain = min(40.0, c.volume_excess_vs_yesterday * 40.0)
        if gain >= 1.0:
            priority += gain
            c.reasons.append(
                f"trading {c.volume_excess_vs_yesterday:+.2f}% above the same moment yesterday"
            )

    if c.trade_excess_vs_yesterday is not None and c.trade_excess_vs_yesterday > 0:
        gain = min(20.0, c.trade_excess_vs_yesterday * 25.0)
        if gain >= 1.0:
            priority += gain
            c.reasons.append(
                f"transaction count {c.trade_excess_vs_yesterday:+.2f}% above yesterday"
            )

    # -- volume leading price is the shape worth finding --------------------- #
    if (
        c.volume_excess_vs_yesterday is not None
        and c.price_change_since_previous_pct is not None
        and c.volume_excess_vs_yesterday > 0
        and abs(c.price_change_since_previous_pct) < 0.35
    ):
        priority += 12.0
        c.reasons.append("activity rising while price is still flat")

    # -- room left in the day's range ---------------------------------------- #
    if c.range_position is None:
        c.caveats.append("24h range unusable (no spread between high and low)")
    elif c.range_position <= 0.75:
        priority += (0.75 - c.range_position) * 14.0
        c.reasons.append(f"at {c.range_position:.0%} of the 24h range, not pinned at the high")
    else:
        c.caveats.append(f"already at {c.range_position:.0%} of the 24h range")

    # -- a move that has already happened is not an early one ---------------- #
    if c.change_pct_24h >= 25.0:
        priority -= 18.0
        c.caveats.append(f"already {c.change_pct_24h:+.1f}% over 24h — the move may be spent")
    elif c.change_pct_24h >= 12.0:
        priority -= 8.0
        c.caveats.append(f"already {c.change_pct_24h:+.1f}% over 24h")

    # -- tradeability, as a modest bonus rather than a ranking axis ----------- #
    if c.spread_bps is None:
        c.caveats.append("spread unknown — this venue does not publish bid/ask here")
    elif c.spread_bps <= 15.0:
        priority += 5.0
        c.reasons.append(f"tight spread ({c.spread_bps:.1f} bps)")
    elif c.spread_bps > 80.0:
        priority -= 6.0
        c.caveats.append(f"wide spread ({c.spread_bps:.0f} bps) will eat a small move")

    c.priority = round(priority, 3)
