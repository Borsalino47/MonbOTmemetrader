"""Core domain types.

Design notes that matter for correctness:

* An `OHLCVSeries` stores columns as float64 numpy arrays. Index `i` is a single
  candle; `open_time[i]` is inclusive, `close_time[i]` is exclusive-end minus 1ms
  in the Binance convention we normalise to.
* A series knows whether its final candle is still forming. `closed()` returns a
  view containing only finished candles. Every feature computation in this
  project runs on `closed()` output, which is the single most important
  structural defence against look-ahead / repainting.
* Every value that reaches a score carries a `Provenance`: which source, which
  timestamp, how old. Nothing is anonymous.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

import numpy as np

# --------------------------------------------------------------------------- #
# Timeframes
# --------------------------------------------------------------------------- #


class Timeframe(str, Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def seconds(self) -> int:
        return _TF_SECONDS[self]

    @property
    def ms(self) -> int:
        return _TF_SECONDS[self] * 1000

    @classmethod
    def parse(cls, raw: str) -> Timeframe:
        try:
            return cls(raw)
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError(f"unsupported timeframe {raw!r}") from exc


_TF_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
}


# --------------------------------------------------------------------------- #
# Provenance / quality
# --------------------------------------------------------------------------- #


class DataQuality(str, Enum):
    OK = "OK"
    STALE = "STALE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a value came from and how old it is.

    `as_of_ms` is the timestamp of the newest observation contained in the value,
    not the time we fetched it. `fetched_at_ms` is when we called the provider.
    Age is measured against the observation, because that is what determines
    whether a signal is actionable.
    """

    source: str
    as_of_ms: int
    fetched_at_ms: int
    quality: DataQuality = DataQuality.OK
    note: str | None = None

    def age_seconds(self, now_ms: int) -> float:
        return max(0.0, (now_ms - self.as_of_ms) / 1000.0)

    def to_dict(self, now_ms: int | None = None) -> dict:
        out = {
            "source": self.source,
            "as_of": _iso(self.as_of_ms),
            "as_of_ms": self.as_of_ms,
            "fetched_at": _iso(self.fetched_at_ms),
            "quality": self.quality.value,
        }
        if now_ms is not None:
            out["age_seconds"] = round(self.age_seconds(now_ms), 3)
        if self.note:
            out["note"] = self.note
        return out


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


# --------------------------------------------------------------------------- #
# Candles
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int
    quote_volume: float = 0.0
    trades: int = 0
    is_closed: bool = True


@dataclass(slots=True)
class OHLCVSeries:
    """Column-oriented OHLCV block for one symbol / one timeframe.

    Sorted ascending by open_time. `last_is_open` marks the final candle as still
    forming; use `closed()` before computing anything that feeds a score.
    """

    symbol: str
    timeframe: Timeframe
    open_time_ms: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    close_time_ms: np.ndarray
    quote_volume: np.ndarray
    trades: np.ndarray
    provenance: Provenance
    last_is_open: bool = False

    # -- construction ------------------------------------------------------- #

    @classmethod
    def from_candles(
        cls,
        symbol: str,
        timeframe: Timeframe,
        candles: Sequence[Candle],
        provenance: Provenance,
    ) -> OHLCVSeries:
        if not candles:
            return cls.empty(symbol, timeframe, provenance)
        ordered = sorted(candles, key=lambda c: c.open_time_ms)
        arr = lambda key, dtype=np.float64: np.array(  # noqa: E731
            [getattr(c, key) for c in ordered], dtype=dtype
        )
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            open_time_ms=arr("open_time_ms", np.int64),
            open=arr("open"),
            high=arr("high"),
            low=arr("low"),
            close=arr("close"),
            volume=arr("volume"),
            close_time_ms=arr("close_time_ms", np.int64),
            quote_volume=arr("quote_volume"),
            trades=arr("trades", np.int64),
            provenance=provenance,
            last_is_open=not ordered[-1].is_closed,
        )

    @classmethod
    def empty(cls, symbol: str, timeframe: Timeframe, provenance: Provenance) -> OHLCVSeries:
        z = lambda dtype=np.float64: np.array([], dtype=dtype)  # noqa: E731
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            open_time_ms=z(np.int64),
            open=z(),
            high=z(),
            low=z(),
            close=z(),
            volume=z(),
            close_time_ms=z(np.int64),
            quote_volume=z(),
            trades=z(np.int64),
            provenance=provenance,
            last_is_open=False,
        )

    # -- access ------------------------------------------------------------- #

    def __len__(self) -> int:
        return int(self.open_time_ms.size)

    @property
    def last_close(self) -> float:
        if len(self) == 0:
            raise IndexError("empty series")
        return float(self.close[-1])

    @property
    def last_open_time_ms(self) -> int:
        return int(self.open_time_ms[-1])

    @property
    def last_close_time_ms(self) -> int:
        return int(self.close_time_ms[-1])

    def slice(self, start: int, stop: int | None = None) -> OHLCVSeries:
        s = slice(start, stop)
        return OHLCVSeries(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_time_ms=self.open_time_ms[s],
            open=self.open[s],
            high=self.high[s],
            low=self.low[s],
            close=self.close[s],
            volume=self.volume[s],
            close_time_ms=self.close_time_ms[s],
            quote_volume=self.quote_volume[s],
            trades=self.trades[s],
            provenance=self.provenance,
            last_is_open=self.last_is_open if (stop is None or stop >= len(self)) else False,
        )

    def closed(self) -> OHLCVSeries:
        """Drop the still-forming candle.

        This is the anti-look-ahead / anti-repaint boundary. Feature code must
        never see a candle whose high/low/close can still change.
        """
        if self.last_is_open and len(self) > 0:
            return self.slice(0, len(self) - 1)
        return self

    def upto(self, ts_ms: int) -> OHLCVSeries:
        """Candles fully closed at or before `ts_ms`. Used by the backtester.

        Strictly `close_time_ms <= ts_ms`, so a candle that has not finished by
        the decision timestamp is invisible.
        """
        if len(self) == 0:
            return self
        idx = int(np.searchsorted(self.close_time_ms, ts_ms, side="right"))
        out = self.slice(0, idx)
        out.last_is_open = False
        return out

    def has_gaps(self) -> bool:
        """True if consecutive open times are not exactly one timeframe apart."""
        if len(self) < 2:
            return False
        deltas = np.diff(self.open_time_ms)
        return bool(np.any(deltas != self.timeframe.ms))

    def gap_ratio(self) -> float:
        if len(self) < 2:
            return 0.0
        deltas = np.diff(self.open_time_ms)
        return float(np.mean(deltas != self.timeframe.ms))

    def iter_candles(self) -> Iterable[Candle]:
        for i in range(len(self)):
            yield Candle(
                open_time_ms=int(self.open_time_ms[i]),
                open=float(self.open[i]),
                high=float(self.high[i]),
                low=float(self.low[i]),
                close=float(self.close[i]),
                volume=float(self.volume[i]),
                close_time_ms=int(self.close_time_ms[i]),
                quote_volume=float(self.quote_volume[i]),
                trades=int(self.trades[i]),
                is_closed=not (self.last_is_open and i == len(self) - 1),
            )


# --------------------------------------------------------------------------- #
# Order book
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: float
    qty: float


@dataclass(slots=True)
class OrderBook:
    symbol: str
    bids: list[OrderBookLevel]  # descending price
    asks: list[OrderBookLevel]  # ascending price
    provenance: Provenance

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        return (b + a) / 2 if (b is not None and a is not None) else None

    @property
    def spread_bps(self) -> float | None:
        b, a, m = self.best_bid, self.best_ask, self.mid
        if b is None or a is None or not m:
            return None
        return (a - b) / m * 10_000

    def depth_quote(self, side: str, pct: float) -> float:
        """Notional resting within `pct` of mid, in quote currency."""
        mid = self.mid
        if mid is None:
            return 0.0
        if side == "bid":
            floor = mid * (1 - pct)
            return sum(l.price * l.qty for l in self.bids if l.price >= floor)
        ceil = mid * (1 + pct)
        return sum(l.price * l.qty for l in self.asks if l.price <= ceil)

    def imbalance(self, pct: float = 0.005) -> float | None:
        """(bid - ask) / (bid + ask) notional within `pct` of mid. Range [-1, 1]."""
        b = self.depth_quote("bid", pct)
        a = self.depth_quote("ask", pct)
        total = a + b
        if total <= 0:
            return None
        return (b - a) / total


# --------------------------------------------------------------------------- #
# Market / ticker snapshots
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SymbolInfo:
    symbol: str
    base: str
    quote: str
    status: str
    tick_size: float | None = None
    step_size: float | None = None
    min_notional: float | None = None


@dataclass(slots=True)
class AssetValuation:
    """Supply-side facts about an asset, from a source that is not the exchange.

    `market_cap_usd` is what a ten-fold move has to be paid for, and it cannot be
    derived from candles at any price — a $0.000001 token can be worth more than
    a $60,000 one. When the figure is genuinely unknown the field stays `None`.

    `market_cap_upper_bound_usd` exists because "not in the top N by market cap"
    is a real, usable fact rather than a missing one: it bounds the cap from
    above by the smallest cap in the ranking that was searched. A bound is
    recorded as a bound and never promoted to a value.
    """

    symbol: str
    market_cap_usd: float | None = None
    market_cap_upper_bound_usd: float | None = None
    fully_diluted_valuation_usd: float | None = None
    circulating_supply: float | None = None
    total_supply: float | None = None
    ath_usd: float | None = None
    ath_change_pct: float | None = None
    rank: int | None = None
    ambiguous_symbol: bool = False
    provenance: Provenance | None = None

    @property
    def circulating_ratio(self) -> float | None:
        """Circulating / total supply. Low means heavy future dilution overhead."""
        if not self.circulating_supply or not self.total_supply or self.total_supply <= 0:
            return None
        return self.circulating_supply / self.total_supply

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "market_cap_usd": self.market_cap_usd,
            "market_cap_upper_bound_usd": self.market_cap_upper_bound_usd,
            "fully_diluted_valuation_usd": self.fully_diluted_valuation_usd,
            "circulating_supply": self.circulating_supply,
            "total_supply": self.total_supply,
            "circulating_ratio": self.circulating_ratio,
            "ath_usd": self.ath_usd,
            "ath_change_pct": self.ath_change_pct,
            "rank": self.rank,
            "ambiguous_symbol": self.ambiguous_symbol,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }


@dataclass(slots=True)
class Ticker24h:
    symbol: str
    last_price: float
    quote_volume_24h: float
    price_change_pct_24h: float
    high_24h: float
    low_24h: float
    trades_24h: int
    provenance: Provenance


# --------------------------------------------------------------------------- #
# DEX pair (Phase 2 surface, defined now so the interface is stable)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DexPair:
    pair_address: str
    chain: str
    dex: str
    base_symbol: str
    quote_symbol: str
    price_usd: float | None
    liquidity_usd: float | None
    volume_24h_usd: float | None
    fdv_usd: float | None
    market_cap_usd: float | None
    pair_created_at_ms: int | None
    buys_24h: int | None
    sells_24h: int | None
    price_change_pct: dict[str, float] = field(default_factory=dict)
    provenance: Provenance | None = None
