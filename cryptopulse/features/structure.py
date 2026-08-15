"""Market structure: swings, support/resistance, breakout geometry.

Price structure is the backbone of this scanner. RSI and MACD are supporting
evidence; where price actually is relative to the levels people trade is the
primary signal.

**Confirmation lag is real and is respected here.** A fractal swing high at index
`i` is only knowable once `right` bars have printed after it. `find_swings`
therefore never returns a pivot in the last `right` bars. That costs a few bars of
latency and buys freedom from repainting: a level, once emitted, never moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from cryptopulse.features.indicators import atr, bollinger_width

__all__ = [
    "Swing",
    "SwingKind",
    "StructureTrend",
    "Level",
    "StructureReport",
    "find_swings",
    "cluster_levels",
    "analyse_structure",
    "compression_ratio",
]


class SwingKind(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


class StructureTrend(str, Enum):
    UPTREND = "UPTREND"  # higher highs and higher lows
    DOWNTREND = "DOWNTREND"  # lower highs and lower lows
    RANGE = "RANGE"
    UNCLEAR = "UNCLEAR"


@dataclass(frozen=True, slots=True)
class Swing:
    index: int
    kind: SwingKind
    price: float
    open_time_ms: int
    confirmed_at_index: int

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "kind": self.kind.value,
            "price": self.price,
            "time_ms": self.open_time_ms,
        }


@dataclass(frozen=True, slots=True)
class Level:
    """A horizontal level built from one or more swings that agree in price."""

    price: float
    touches: int
    kind: SwingKind
    last_touch_index: int
    strength: float  # 0..1, combines touch count and recency

    def to_dict(self) -> dict:
        return {
            "price": self.price,
            "touches": self.touches,
            "kind": self.kind.value,
            "strength": round(self.strength, 4),
        }


@dataclass(slots=True)
class StructureReport:
    trend: StructureTrend
    swings: list[Swing]
    resistances: list[Level]
    supports: list[Level]
    nearest_resistance: Level | None
    nearest_support: Level | None
    distance_to_resistance_atr: float | None
    distance_to_support_atr: float | None
    broke_out: bool
    fake_breakout: bool
    in_retest: bool
    range_position: float | None  # 0 at range low, 1 at range high
    atr_value: float | None
    notes: list[str]

    def to_dict(self) -> dict:
        return {
            "trend": self.trend.value,
            "resistances": [l.to_dict() for l in self.resistances],
            "supports": [l.to_dict() for l in self.supports],
            "nearest_resistance": self.nearest_resistance.to_dict() if self.nearest_resistance else None,
            "nearest_support": self.nearest_support.to_dict() if self.nearest_support else None,
            "distance_to_resistance_atr": _r(self.distance_to_resistance_atr),
            "distance_to_support_atr": _r(self.distance_to_support_atr),
            "broke_out": self.broke_out,
            "fake_breakout": self.fake_breakout,
            "in_retest": self.in_retest,
            "range_position": _r(self.range_position),
            "atr": _r(self.atr_value, 8),
            "notes": self.notes,
        }


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


# --------------------------------------------------------------------------- #
# Swings
# --------------------------------------------------------------------------- #


def find_swings(high: np.ndarray, low: np.ndarray, open_time_ms: np.ndarray, left: int = 3, right: int = 3) -> list[Swing]:
    """Fractal pivots confirmed by `right` subsequent bars.

    A bar is a swing high if its high is >= every high in the `left` bars before
    and strictly > every high in the `right` bars after. The asymmetry (>= before,
    > after) breaks ties deterministically toward the earlier bar.
    """
    h = np.asarray(high, dtype=np.float64)
    l = np.asarray(low, dtype=np.float64)
    t = np.asarray(open_time_ms, dtype=np.int64)
    n = h.size
    swings: list[Swing] = []
    if n < left + right + 1:
        return swings

    for i in range(left, n - right):
        window_left_h = h[i - left : i]
        window_right_h = h[i + 1 : i + 1 + right]
        if h[i] >= window_left_h.max() and h[i] > window_right_h.max():
            swings.append(
                Swing(index=i, kind=SwingKind.HIGH, price=float(h[i]), open_time_ms=int(t[i]), confirmed_at_index=i + right)
            )
        window_left_l = l[i - left : i]
        window_right_l = l[i + 1 : i + 1 + right]
        if l[i] <= window_left_l.min() and l[i] < window_right_l.min():
            swings.append(
                Swing(index=i, kind=SwingKind.LOW, price=float(l[i]), open_time_ms=int(t[i]), confirmed_at_index=i + right)
            )

    swings.sort(key=lambda s: s.index)
    return swings


def classify_trend(swings: list[Swing], lookback: int = 4) -> StructureTrend:
    """Label trend from the sequence of recent confirmed swings."""
    highs = [s for s in swings if s.kind is SwingKind.HIGH][-lookback:]
    lows = [s for s in swings if s.kind is SwingKind.LOW][-lookback:]
    if len(highs) < 2 or len(lows) < 2:
        return StructureTrend.UNCLEAR

    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price

    if hh and hl:
        return StructureTrend.UPTREND
    if lh and ll:
        return StructureTrend.DOWNTREND
    if (hh and ll) or (lh and hl):
        return StructureTrend.RANGE
    return StructureTrend.UNCLEAR


# --------------------------------------------------------------------------- #
# Levels
# --------------------------------------------------------------------------- #


def cluster_levels(
    swings: list[Swing], kind: SwingKind, tolerance: float, total_bars: int, max_levels: int = 5
) -> list[Level]:
    """Group swings of one kind into price clusters.

    `tolerance` is an absolute price distance (callers pass a fraction of ATR).
    A level touched three times matters more than one touched once, and a level
    touched recently matters more than one from the far left of the window.
    """
    picked = [s for s in swings if s.kind is kind]
    if not picked:
        return []

    picked.sort(key=lambda s: s.price)
    clusters: list[list[Swing]] = [[picked[0]]]
    for s in picked[1:]:
        if abs(s.price - clusters[-1][-1].price) <= tolerance:
            clusters[-1].append(s)
        else:
            clusters.append([s])

    levels: list[Level] = []
    for group in clusters:
        prices = np.array([g.price for g in group], dtype=np.float64)
        last_idx = max(g.index for g in group)
        recency = (last_idx + 1) / max(total_bars, 1)
        touch_score = min(len(group) / 3.0, 1.0)
        strength = float(np.clip(0.6 * touch_score + 0.4 * recency, 0.0, 1.0))
        levels.append(
            Level(
                price=float(prices.mean()),
                touches=len(group),
                kind=kind,
                last_touch_index=last_idx,
                strength=strength,
            )
        )

    levels.sort(key=lambda l: l.strength, reverse=True)
    return levels[:max_levels]


# --------------------------------------------------------------------------- #
# Compression
# --------------------------------------------------------------------------- #


def compression_ratio(close: np.ndarray, period: int = 20, lookback: int = 100) -> float | None:
    """Where current Bollinger width sits inside its own recent distribution.

    0.0 means the tightest the band has been over `lookback` bars (maximum
    compression); 1.0 means the widest. Self-referential by construction, so a
    permanently volatile micro-cap and a placid large-cap are judged on their own
    terms.
    """
    bw = bollinger_width(close, period)
    valid = bw[~np.isnan(bw)]
    if valid.size < 10:
        return None
    window = valid[-lookback:]
    current = float(valid[-1])
    return float(np.mean(window <= current))


# --------------------------------------------------------------------------- #
# Top-level analysis
# --------------------------------------------------------------------------- #


def analyse_structure(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_time_ms: np.ndarray,
    *,
    left: int = 3,
    right: int = 3,
    atr_period: int = 14,
    level_tolerance_atr: float = 0.35,
    breakout_confirm_atr: float = 0.15,
    retest_band_atr: float = 0.5,
) -> StructureReport:
    """Full structural read of one closed series.

    Everything is measured in ATR multiples so that thresholds transfer between a
    $60,000 asset and a $0.00003 asset without retuning.
    """
    h = np.asarray(high, dtype=np.float64)
    l = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    n = c.size
    notes: list[str] = []

    if n < max(atr_period + 2, left + right + 2):
        return StructureReport(
            trend=StructureTrend.UNCLEAR,
            swings=[],
            resistances=[],
            supports=[],
            nearest_resistance=None,
            nearest_support=None,
            distance_to_resistance_atr=None,
            distance_to_support_atr=None,
            broke_out=False,
            fake_breakout=False,
            in_retest=False,
            range_position=None,
            atr_value=None,
            notes=["INSUFFICIENT_HISTORY"],
        )

    atr_series = atr(h, l, c, atr_period)
    atr_now = float(atr_series[-1]) if np.isfinite(atr_series[-1]) else None
    if not atr_now or atr_now <= 0:
        notes.append("ATR_UNAVAILABLE")
        atr_now = None

    swings = find_swings(h, l, open_time_ms, left=left, right=right)
    trend = classify_trend(swings)

    price = float(c[-1])
    tol = (atr_now or price * 0.002) * level_tolerance_atr

    resistances = cluster_levels(swings, SwingKind.HIGH, tol, n)
    supports = cluster_levels(swings, SwingKind.LOW, tol, n)

    # Nearest *relevant* resistance = strongest level still above price.
    above = [lv for lv in resistances if lv.price > price]
    below = [lv for lv in supports if lv.price < price]
    nearest_res = min(above, key=lambda lv: lv.price - price) if above else None
    nearest_sup = max(below, key=lambda lv: lv.price) if below else None

    d_res = (nearest_res.price - price) / atr_now if (nearest_res and atr_now) else None
    d_sup = (price - nearest_sup.price) / atr_now if (nearest_sup and atr_now) else None

    # Breakout: price closed above a level that was resistance, by a margin that
    # exceeds noise (measured in ATR).
    broke_out = False
    fake_breakout = False
    in_retest = False

    recent_res = [lv for lv in resistances if lv.price <= price]
    if recent_res and atr_now:
        broken = max(recent_res, key=lambda lv: lv.price)
        margin = (price - broken.price) / atr_now
        if margin >= breakout_confirm_atr:
            broke_out = True
            notes.append(f"closed {margin:.2f} ATR above level {broken.price:.8g}")
        if abs(margin) <= retest_band_atr and broken.last_touch_index < n - right - 1:
            # Price is hovering on a level it previously broke: a retest.
            lookback = min(20, n)
            if float(np.max(h[-lookback:])) > broken.price + breakout_confirm_atr * atr_now:
                in_retest = True
                notes.append("price returned to a previously broken level")

    # Fake breakout: an intrabar poke above a level that did not hold on close.
    if atr_now and above:
        lookback = min(10, n)
        target = min(above, key=lambda lv: lv.price)
        poked = float(np.max(h[-lookback:])) > target.price + breakout_confirm_atr * atr_now
        if poked and price < target.price:
            fake_breakout = True
            notes.append("wick exceeded resistance but close did not hold")

    # Position inside the visible range.
    win = min(60, n)
    rng_hi = float(np.max(h[-win:]))
    rng_lo = float(np.min(l[-win:]))
    range_position = (price - rng_lo) / (rng_hi - rng_lo) if rng_hi > rng_lo else None

    return StructureReport(
        trend=trend,
        swings=swings[-24:],
        resistances=resistances,
        supports=supports,
        nearest_resistance=nearest_res,
        nearest_support=nearest_sup,
        distance_to_resistance_atr=d_res,
        distance_to_support_atr=d_sup,
        broke_out=broke_out,
        fake_breakout=fake_breakout,
        in_retest=in_retest,
        range_position=range_position,
        atr_value=atr_now,
        notes=notes,
    )
