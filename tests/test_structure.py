"""Market structure: swings, levels, breakout geometry, compression."""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.core.types import Timeframe
from cryptopulse.features.structure import (
    StructureTrend,
    SwingKind,
    analyse_structure,
    classify_trend,
    cluster_levels,
    compression_ratio,
    find_swings,
)

TF_MS = Timeframe.M5.ms


def _times(n: int) -> np.ndarray:
    return np.arange(n, dtype=np.int64) * TF_MS


def test_finds_an_obvious_swing_high():
    high = np.array([1, 2, 3, 10, 3, 2, 1], dtype=float)
    low = high - 1
    swings = find_swings(high, low, _times(7), left=3, right=3)
    highs = [s for s in swings if s.kind is SwingKind.HIGH]
    assert len(highs) == 1
    assert highs[0].index == 3 and highs[0].price == 10.0


def test_finds_an_obvious_swing_low():
    low = np.array([9, 8, 7, 1, 7, 8, 9], dtype=float)
    high = low + 1
    lows = [s for s in find_swings(high, low, _times(7), left=3, right=3) if s.kind is SwingKind.LOW]
    assert len(lows) == 1 and lows[0].index == 3 and lows[0].price == 1.0


def test_no_swing_on_a_monotonic_series():
    v = np.arange(30, dtype=float)
    assert find_swings(v + 1, v, _times(30), left=3, right=3) == []


def test_uptrend_classified_from_higher_highs_and_higher_lows():
    # A clean zig-zag with each peak and trough above the last.
    prices = []
    for i in range(6):
        prices += [10 + i * 2, 14 + i * 2, 11 + i * 2]
    close = np.array(prices, dtype=float)
    swings = find_swings(close + 0.5, close - 0.5, _times(close.size), left=1, right=1)
    assert classify_trend(swings) is StructureTrend.UPTREND


def test_downtrend_classified_from_lower_highs_and_lower_lows():
    prices = []
    for i in range(6):
        prices += [30 - i * 2, 34 - i * 2, 31 - i * 2]
    close = np.array(prices, dtype=float)
    swings = find_swings(close + 0.5, close - 0.5, _times(close.size), left=1, right=1)
    assert classify_trend(swings) is StructureTrend.DOWNTREND


def test_too_few_swings_is_unclear_not_a_guess():
    assert classify_trend([]) is StructureTrend.UNCLEAR


def test_levels_cluster_repeated_touches_of_the_same_price():
    high = np.zeros(60)
    low = np.zeros(60)
    high[:] = 90
    low[:] = 85
    # Three rejections from ~100, spaced apart.
    for i in (10, 25, 40):
        high[i] = 100.0 + (i % 3) * 0.1
    swings = find_swings(high, low, _times(60), left=3, right=3)
    levels = cluster_levels(swings, SwingKind.HIGH, tolerance=1.0, total_bars=60)
    assert levels
    top = levels[0]
    assert top.touches == 3, "three touches within tolerance should collapse into one level"
    assert top.price == pytest.approx(100.07, abs=0.2)


def test_more_touches_means_more_strength():
    high = np.full(60, 90.0)
    low = np.full(60, 85.0)
    for i in (10, 25, 40):
        high[i] = 100.0
    high[50] = 110.0  # a single, isolated touch
    swings = find_swings(high, low, _times(60), left=3, right=3)
    levels = cluster_levels(swings, SwingKind.HIGH, tolerance=0.5, total_bars=60)
    by_price = {round(l.price): l for l in levels}
    assert by_price[100].touches == 3 and by_price[110].touches == 1
    assert by_price[100].strength > by_price[110].strength


def test_breakout_detected_when_price_closes_above_a_tested_level():
    n = 80
    high = np.full(n, 99.0)
    low = np.full(n, 95.0)
    close = np.full(n, 97.0)
    for i in (10, 25, 40):  # build the resistance at 100
        high[i] = 100.0
    close[-6:] = np.linspace(100.5, 104.0, 6)  # then break it
    high[-6:] = close[-6:] + 0.5
    low[-6:] = close[-6:] - 0.5

    report = analyse_structure(high, low, close, _times(n))
    assert report.broke_out is True
    assert any("above level" in note for note in report.notes)


def test_fake_breakout_when_the_wick_exceeds_but_the_close_does_not():
    n = 80
    high = np.full(n, 99.0)
    low = np.full(n, 95.0)
    close = np.full(n, 97.0)
    for i in (10, 25, 40):
        high[i] = 100.0
    high[-3] = 103.0  # a wick well above resistance
    close[-3:] = [97.5, 97.2, 97.0]  # but every close fails to hold

    report = analyse_structure(high, low, close, _times(n))
    assert report.fake_breakout is True
    assert report.broke_out is False


def test_distance_to_resistance_is_measured_in_atr():
    n = 80
    rng = np.random.default_rng(41)
    close = 100 + rng.normal(0, 0.3, n)
    high = close + 0.5
    low = close - 0.5
    for i in (10, 25, 40):
        high[i] = 105.0

    report = analyse_structure(high, low, close, _times(n))
    assert report.nearest_resistance is not None
    assert report.distance_to_resistance_atr is not None
    assert report.atr_value is not None
    expected = (report.nearest_resistance.price - close[-1]) / report.atr_value
    assert report.distance_to_resistance_atr == pytest.approx(expected)
    assert report.distance_to_resistance_atr > 0, "resistance must be above the current price"


def test_insufficient_history_is_reported_not_guessed():
    report = analyse_structure(
        np.array([1.0, 2, 3]), np.array([0.5, 1, 2]), np.array([1.0, 1.5, 2.5]), _times(3)
    )
    assert report.notes == ["INSUFFICIENT_HISTORY"]
    assert report.trend is StructureTrend.UNCLEAR
    assert report.atr_value is None


def test_compression_ratio_lower_for_a_tightening_series():
    rng = np.random.default_rng(42)
    wild = 100 + np.cumsum(rng.normal(0, 1.5, 150))
    tightening = np.concatenate([100 + np.cumsum(rng.normal(0, 1.5, 110)), np.full(40, 100.0) + rng.normal(0, 0.02, 40)])
    assert compression_ratio(tightening, 20, 100) < compression_ratio(wild, 20, 100)


def test_compression_ratio_needs_data():
    assert compression_ratio(np.array([1.0, 2.0, 3.0]), 20) is None


def test_range_position_is_between_zero_and_one():
    rng = np.random.default_rng(43)
    close = 100 + np.cumsum(rng.normal(0, 0.4, 120))
    report = analyse_structure(close + 0.4, close - 0.4, close, _times(120))
    assert report.range_position is not None
    assert 0.0 <= report.range_position <= 1.0
